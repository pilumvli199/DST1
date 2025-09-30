#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-detecting DhanHQ WebSocket Alert Bot
Attempts to:
 - instantiate DhanFeed (or other feed classes)
 - inspect feed & module for runnable callables
 - try multiple signatures including module.market_feed_wss(...)
 - fallback: log dir() for debugging
"""

import os
import time
import signal
import logging
import requests
import traceback
import importlib
from types import ModuleType
from typing import Any, Callable, Optional

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
        # handle JSON-like dicts or string/bytes
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
            # nested checks
            if not security_id:
                for k in ("data", "payload", "tick", "update"):
                    nested = message.get(k)
                    if isinstance(nested, dict):
                        security_id = security_id or nested.get("securityId") or nested.get("symbol")
                        ltp = ltp or nested.get("lastTradedPrice") or nested.get("ltp")
                        if security_id:
                            break
        else:
            # attribute-style
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
# Auto-detect & run logic
# -----------------
def try_call_callable(obj, func_name, handler):
    """
    Try to call obj.func_name with various common signatures.
    Returns True if call was made (no guaranteed persistence).
    """
    f = getattr(obj, func_name, None)
    if not callable(f):
        return False

    logger.info("Attempting callable: %s on %s", func_name, type(obj).__name__)
    # try many signatures
    variants = [
        (handler,),  # positional
        ((), {"on_message": handler}),
        ((), {"callback": handler}),
        ((), {"handler": handler}),
        ((), {"cb": handler}),
        ((), {"on_message_callback": handler}),
    ]
    for pos_args, kw in [(v if isinstance(v, tuple) and len(v)==2 else (v, {})) for v in variants]:
        # The above expression yields wrong shape; simpler iterate explicitly below
        pass

    # simpler explicit tries:
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
            return True
        except TypeError as te:
            logger.debug("Signature mismatch for %s: %s", func_name, te)
        except Exception as e:
            # If it runs and then raises inside, we surface but consider that we invoked it.
            logger.exception("Callable %s raised exception during call: %s", func_name, e)
            return True
    return False

def start_market_feed():
    if not CLIENT_ID or not ACCESS_TOKEN:
        logger.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN.")
        return

    # import and inspect
    try:
        import dhanhq as dh
    except Exception as e:
        logger.exception("Failed to import dhanhq: %s", e)
        return

    # print module contents
    try:
        module_contents = ", ".join(dir(dh))
        logger.info("dhanhq module contents: %s", module_contents)
    except Exception:
        logger.exception("Could not list dhanhq contents")

    # Prefer dh.marketfeed if present
    module_obj = None
    feed_class = None
    helpers = {}
    if hasattr(dh, "marketfeed"):
        module_obj = dh.marketfeed
        logger.info("Using dh.marketfeed module")
        # check common feed classes
        for candidate in ("DhanFeed", "MarketFeed", "DhanMarketFeed"):
            if hasattr(module_obj, candidate):
                feed_class = getattr(module_obj, candidate)
                logger.info("Detected feed class: %s", candidate)
                break

    # also check root-level classes
    if feed_class is None:
        for candidate in ("DhanFeed", "MarketFeed"):
            if hasattr(dh, candidate):
                feed_class = getattr(dh, candidate)
                module_obj = dh
                logger.info("Detected root-level feed class: %s", candidate)
                break

    # prepare instruments using detected constants if possible
    NSE = getattr(module_obj, "NSE", getattr(dh, "NSE", None))
    TICKER = getattr(module_obj, "Ticker", getattr(module_obj, "TICKER", getattr(dh, "Ticker", getattr(dh, "TICKER", None))))
    if NSE is None or TICKER is None:
        instruments = [("NSE", HDFC_ID, "TICKER")]
    else:
        instruments = [(NSE, HDFC_ID, TICKER)]
    logger.info("Instruments to subscribe: %s", instruments)

    backoff = INITIAL_BACKOFF
    while True:
        try:
            feed = None
            if feed_class:
                try:
                    feed = feed_class(CLIENT_ID, ACCESS_TOKEN, instruments, version="2.0")
                    logger.info("Instantiated feed_class with (client, token, instruments, version).")
                except TypeError:
                    try:
                        feed = feed_class(CLIENT_ID, ACCESS_TOKEN, instruments)
                        logger.info("Instantiated feed_class with (client, token, instruments).")
                    except Exception as e:
                        logger.exception("Failed to instantiate feed_class: %s", e)
                        feed = None
                except Exception as e:
                    logger.exception("Error instantiating feed_class: %s", e)
                    feed = None
            else:
                # fallback: use module-level constructors or functions
                if hasattr(module_obj, "DhanFeed"):
                    try:
                        feed = getattr(module_obj, "DhanFeed")(CLIENT_ID, ACCESS_TOKEN, instruments)
                        logger.info("Instantiated module_obj.DhanFeed")
                    except Exception as e:
                        logger.exception("module_obj.DhanFeed failed: %s", e)
                elif hasattr(module_obj, "MarketFeed"):
                    try:
                        feed = getattr(module_obj, "MarketFeed")(CLIENT_ID, ACCESS_TOKEN, instruments)
                        logger.info("Instantiated module_obj.MarketFeed")
                    except Exception as e:
                        logger.exception("module_obj.MarketFeed failed: %s", e)

            if feed is None:
                logger.error("Could not create feed instance. Module/class inspection follows.")
                # log feed_class and module_obj dir for debugging
                try:
                    if module_obj:
                        logger.info("module_obj dir: %s", ", ".join(dir(module_obj)))
                    logger.info("root dh dir: %s", ", ".join(dir(dh)))
                except Exception:
                    pass
                return

            # Inspect feed for runnable methods
            feed_dir = dir(feed)
            logger.info("feed dir: %s", ", ".join(feed_dir))

            # Try common names first
            tried = False
            for name in ("run_forever", "run", "start", "listen", "listen_forever", "serve", "connect_and_listen"):
                if name in feed_dir:
                    invoked = try_call_callable(feed, name, market_feed_handler)
                    tried = tried or invoked
                    if invoked:
                        # if invoked, assume it blocks; when it returns we'll reconnect
                        logger.info("Invoked %s on feed; if this is blocking then handler is active.", name)
                        # sleep a bit to avoid tight loop
                        time.sleep(1)
                        break

            if not tried:
                # try module-level helper market_feed_wss if available
                if hasattr(module_obj, "market_feed_wss"):
                    logger.info("Trying module_obj.market_feed_wss(...) as fallback")
                    try:
                        # many implementations: market_feed_wss(client, token, instruments, callback)
                        module_obj.market_feed_wss(CLIENT_ID, ACCESS_TOKEN, instruments, market_feed_handler)
                        logger.info("Called market_feed_wss; assuming it blocks and is delivering callbacks.")
                        tried = True
                        time.sleep(1)
                    except TypeError:
                        # try named param
                        try:
                            module_obj.market_feed_wss(CLIENT_ID, ACCESS_TOKEN, instruments, callback=market_feed_handler)
                            logger.info("Called market_feed_wss with callback=...")
                            tried = True
                            time.sleep(1)
                        except Exception as e:
                            logger.exception("market_feed_wss invocation failed: %s", e)
                    except Exception as e:
                        logger.exception("market_feed_wss raised: %s", e)

            if not tried:
                # Last resort: attempt to detect any callable that looks promising
                for attr in feed_dir:
                    low = attr.lower()
                    if any(token in low for token in ("listen", "run", "start", "connect", "market", "ws", "subscribe")):
                        invoked = try_call_callable(feed, attr, market_feed_handler)
                        if invoked:
                            tried = True
                            break

            if not tried:
                # Nothing worked — dump diagnostic info and exit (so user can inspect logs)
                logger.error("No runnable entrypoint found on feed or module. Dumping diagnostics:")
                try:
                    logger.info("dhanhq module dir: %s", ", ".join(dir(dh)))
                except Exception:
                    pass
                logger.info("feed object type: %s", type(feed))
                logger.info("feed dir: %s", ", ".join(feed_dir))
                return

            # if we get here, we've invoked a callable and it likely is blocking delivering callbacks.
            # After it returns, we'll try reconnecting (loop).
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
