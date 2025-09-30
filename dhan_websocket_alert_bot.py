#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-detecting DhanHQ WebSocket Alert Bot (with version trial)
Tries multiple constructors and run signatures. If feed raises
ValueError("Unsupported version: ...") we automatically retry other versions.
"""

import os
import time
import signal
import logging
import requests
import traceback
import importlib
from types import ModuleType
from typing import Any, Optional

# -----------------
# Config / Env
# -----------------
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
HDFC_ID = os.environ.get("HDFC_ID", "1333")
SEND_INTERVAL_SECONDS = int(os.environ.get("SEND_INTERVAL_SECONDS", "60"))
INITIAL_BACKOFF = 1
MAX_BACKOFF = 60

# -----------------
# Logging
# -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dhan-autodetect")

# -----------------
# Telegram helpers
# -----------------
def esc_md(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text

_last_sent = {}
def send_telegram_message(security_id: str, ltp_price: float, friendly_name: Optional[str]=None):
    now = time.time()
    last = _last_sent.get(security_id, 0)
    if now - last < SEND_INTERVAL_SECONDS:
        logger.debug("Throttle skip %s", security_id)
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Missing telegram envs.")
        return
    timestamp = time.strftime("%H:%M:%S IST")
    name = friendly_name or f"Security {security_id}"
    message = (
        f"*{esc_md('HDFC BANK LTP ALERT!')}* 🔔\n"
        f"वेळ: {esc_md(timestamp)}\n\n"
        f"*{esc_md(name)}*\n"
        f"नवीनतम LTP: ₹ *{esc_md(f'{ltp_price:.2f}')}*\n\n"
        f"_हा ॲलर्ट दर {SEND_INTERVAL_SECONDS} सेकंदाने WebSocket Data वर आधारित आहे._"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=8)
        if resp.ok:
            logger.info("Telegram sent for %s: ₹%.2f", security_id, ltp_price)
            _last_sent[security_id] = time.time()
        else:
            logger.warning("Telegram API error %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("Telegram send failed: %s", e)

# -----------------
# Generic message handler
# -----------------
latest_ltp = {}
def market_feed_handler(message: Any):
    try:
        if isinstance(message, (bytes, str)):
            import json
            try:
                message = json.loads(message)
            except Exception:
                pass

        security_id = None
        ltp = None
        if isinstance(message, dict):
            security_id = message.get("securityId") or message.get("symbol") or message.get("security_id") or message.get("s")
            ltp = message.get("lastTradedPrice") or message.get("ltp") or message.get("last_price") or message.get("last")
            if not security_id:
                for k in ("data", "payload", "tick", "update"):
                    nested = message.get(k)
                    if isinstance(nested, dict):
                        security_id = security_id or nested.get("securityId") or nested.get("symbol")
                        ltp = ltp or nested.get("lastTradedPrice") or nested.get("ltp")
                        if security_id:
                            break
        else:
            for attr in ("securityId", "symbol", "security_id"):
                if hasattr(message, attr):
                    security_id = getattr(message, attr)
            for attr in ("lastTradedPrice", "ltp", "last_price", "last"):
                if hasattr(message, attr):
                    ltp = getattr(message, attr)

        if security_id:
            security_id = str(security_id)
        if ltp is not None:
            try:
                ltp = float(ltp)
            except Exception:
                ltp = None

        if security_id and ltp is not None:
            latest_ltp[security_id] = ltp
            logger.info("Real-time: %s LTP %.2f", security_id, ltp)
            send_telegram_message(security_id, ltp, friendly_name="HDFC BANK" if security_id == HDFC_ID else None)
        else:
            logger.debug("Ignored msg (no sec/ltp): %s", message)
    except Exception as e:
        logger.exception("Handler error: %s\n%s", e, traceback.format_exc())

# -----------------
# Utility to try calling a callable with common signatures
# -----------------
def try_call_callable(obj, func_name, handler):
    f = getattr(obj, func_name, None)
    if not callable(f):
        return False, None  # (invoked False, error None)

    logger.info("Attempting callable: %s on %s", func_name, type(obj).__name__)
    tries = [
        lambda: f(handler),
        lambda: f(on_message=handler),
        lambda: f(callback=handler),
        lambda: f(handler=handler),
        lambda: f(cb=handler),
        lambda: f()
    ]
    for t in tries:
        try:
            t()
            logger.info("Callable %s invoked successfully (may be blocking).", func_name)
            return True, None
        except TypeError as te:
            logger.debug("Signature mismatch for %s: %s", func_name, te)
            continue
        except ValueError as ve:
            # return the ValueError so caller can inspect (e.g., Unsupported version)
            logger.exception("Callable %s raised ValueError during call: %s", func_name, ve)
            return True, ve
        except Exception as e:
            logger.exception("Callable %s raised exception during call: %s", func_name, e)
            # treat as invoked (it ran but raised) so return True and the exception
            return True, e
    return False, None

# -----------------
# Start market feed with version trials
# -----------------
def start_market_feed():
    if not CLIENT_ID or not ACCESS_TOKEN:
        logger.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN.")
        return

    try:
        import dhanhq as dh
    except Exception as e:
        logger.exception("Failed to import dhanhq: %s", e)
        return

    logger.info("dhanhq module contents: %s", ", ".join(dir(dh)))
    module_obj = getattr(dh, "marketfeed", None) or dh

    # detect feed class
    feed_class = None
    for candidate in ("DhanFeed", "MarketFeed", "DhanMarketFeed"):
        if hasattr(module_obj, candidate):
            feed_class = getattr(module_obj, candidate)
            logger.info("Detected feed class: %s", candidate)
            break
    if not feed_class:
        # also check root
        for candidate in ("DhanFeed", "MarketFeed"):
            if hasattr(dh, candidate):
                feed_class = getattr(dh, candidate)
                module_obj = dh
                logger.info("Detected root-level feed class: %s", candidate)
                break

    # detect constants
    NSE = getattr(module_obj, "NSE", getattr(dh, "NSE", None))
    TICKER = getattr(module_obj, "Ticker", getattr(module_obj, "TICKER", getattr(dh, "Ticker", getattr(dh, "TICKER", None))))
    if NSE is None or TICKER is None:
        instruments = [("NSE", HDFC_ID, "TICKER")]
    else:
        instruments = [(NSE, HDFC_ID, TICKER)]

    logger.info("Instruments to subscribe: %s", instruments)

    # versions to try: prefer no-version, then '1'/'v1', then '2.0' as last
    version_candidates = [None, "1", "v1", "2.0", "v2"]

    backoff = INITIAL_BACKOFF
    while True:
        try:
            feed = None
            invoked_any = False
            # If we have a feed_class, try multiple version candidates
            for ver in version_candidates:
                try:
                    if ver is None:
                        try:
                            feed = feed_class(CLIENT_ID, ACCESS_TOKEN, instruments)
                            logger.info("Instantiated feed_class without version.")
                        except TypeError:
                            # some constructors require version param, handle below
                            feed = None
                    else:
                        try:
                            feed = feed_class(CLIENT_ID, ACCESS_TOKEN, instruments, version=ver)
                            logger.info("Instantiated feed_class with version=%s", ver)
                        except TypeError:
                            # maybe signature expects 'v' or other ordering; try positional fallback
                            try:
                                feed = feed_class(CLIENT_ID, ACCESS_TOKEN, instruments, ver)
                                logger.info("Instantiated feed_class with positional version=%s", ver)
                            except Exception:
                                feed = None
                    if feed:
                        # Inspect feed methods and attempt to run; capture ValueError for Unsupported version
                        feed_dir = dir(feed)
                        logger.info("feed dir: %s", ", ".join(feed_dir))

                        # try run methods
                        for name in ("run_forever", "run", "start", "listen", "listen_forever", "serve", "connect_and_listen"):
                            if name in feed_dir:
                                invoked, err = try_call_callable(feed, name, market_feed_handler)
                                if invoked:
                                    invoked_any = True
                                    # if err is ValueError and message contains Unsupported version -> break and try next version
                                    if isinstance(err, ValueError) and "Unsupported version" in str(err):
                                        logger.warning("Detected Unsupported version for version=%s -> trying next candidate", ver)
                                        invoked_any = False
                                        # break inner loop to try next ver
                                        break
                                    # else assume feed is running/blocked and keep monitoring; after it returns we'll reconnect
                                    time.sleep(1)
                                    break
                        if invoked_any:
                            break  # exit version loop; feed running
                        # If not invoked, try module-level market_feed_wss
                        if not invoked_any and hasattr(module_obj, "market_feed_wss"):
                            try:
                                logger.info("Trying module.market_feed_wss with version=%s", ver)
                                # try multiple signatures
                                try:
                                    module_obj.market_feed_wss(CLIENT_ID, ACCESS_TOKEN, instruments, market_feed_handler)
                                    invoked_any = True
                                except TypeError:
                                    try:
                                        module_obj.market_feed_wss(CLIENT_ID, ACCESS_TOKEN, instruments, callback=market_feed_handler)
                                        invoked_any = True
                                    except Exception as e:
                                        logger.exception("market_feed_wss call failed: %s", e)
                                if invoked_any:
                                    time.sleep(1)
                                    break
                            except Exception as e:
                                logger.exception("market_feed_wss raised: %s", e)
                        # if invoked_any triggered by Unsupported version earlier we continue to next version
                        if not invoked_any:
                            # if the feed object existed but no runnable method worked for this version, try next version
                            logger.info("No runnable method succeeded for version=%s; trying next version candidate.", ver)
                            # before trying next version, if feed provides disconnect/close, attempt cleanup
                            try:
                                if hasattr(feed, "disconnect"):
                                    feed.disconnect()
                                elif hasattr(feed, "close_connection"):
                                    feed.close_connection()
                            except Exception:
                                pass
                            feed = None
                            continue  # next version
                except Exception as top_e:
                    # If instantiation or invocation raised ValueError('Unsupported version') at module level, catch and continue
                    logger.exception("Error while trying version=%s : %s", ver, top_e)
                    # detect Unsupported version message
                    if isinstance(top_e, ValueError) and "Unsupported version" in str(top_e):
                        logger.warning("Unsupported version exception for candidate %s; trying next", ver)
                        continue
                    # otherwise continue trying other versions too
                    continue

            if not invoked_any:
                logger.error("Failed to find a working run/start for any tried versions. Dumping diagnostics and exiting.")
                try:
                    logger.info("dhanhq module dir: %s", ", ".join(dir(dh)))
                    if module_obj:
                        logger.info("marketfeed/module dir: %s", ", ".join(dir(module_obj)))
                except Exception:
                    pass
                return

            # If we invoked a run method that likely blocks, when it returns we'll loop and reconnect
            backoff = INITIAL_BACKOFF

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt; exiting.")
            return
        except Exception as e:
            logger.exception("Unexpected loop error: %s", e)
            sleep = min(MAX_BACKOFF, backoff)
            logger.info("Reconnecting in %.1f s", sleep)
            time.sleep(sleep)
            backoff = min(MAX_BACKOFF, backoff * 2 if backoff > 0 else INITIAL_BACKOFF)

# -----------------
# Signals
# -----------------
def _signal_handler(sig, frame):
    logger.info("Signal %s received; exiting.", sig)
    raise SystemExit()

import signal
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# -----------------
# Main
# -----------------
if __name__ == "__main__":
    logger.info("Starting auto-detect DhanHQ bot; HDFC_ID=%s", HDFC_ID)
    start_market_feed()
    logger.info("Bot finished.")
