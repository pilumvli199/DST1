#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robust DhanHQ WebSocket Alert Bot (fixed syntax)
- Inspects dhanhq module for feed class
- Tries multiple constructor signatures and version candidates
- Tries common run/start signatures
- Throttled Telegram alerts, graceful shutdown, backoff
"""

import os
import time
import logging
import traceback
import inspect
from typing import Any, Optional

import requests

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
# Constructor helper (simpler, robust)
# -----------------
def instantiate_feed_simple(feed_class, client, token, instruments, version_candidates):
    """
    Simpler attempt to instantiate feed_class:
     - try kwargs with common names
     - try positional (client, token, instruments)
     - try adding version kw if supported
    Returns (instance, used_version) or (None, None)
    """
    param_names = []
    try:
        sig = inspect.signature(feed_class)
        param_names = list(sig.parameters.keys())
        logger.debug("Constructor params: %s", param_names)
    except Exception:
        # cannot introspect; we'll try safe positional too
        param_names = []

    # common mappings
    mappings = [
        {"client_id": client, "access_token": token, "instruments": instruments},
        {"clientId": client, "access_token": token, "instruments": instruments},
        {"client": client, "token": token, "instruments": instruments},
        {"client_id": client, "token": token, "instruments": instruments},
        {"client": client, "access_token": token, "instruments": instruments},
    ]

    version_keys = ["version", "v", "feed_type", "feedType"]

    for ver in version_candidates:
        # try mappings with kw if possible
        for kw in mappings:
            kwargs = {}
            for k, v in kw.items():
                if not param_names or k in param_names:
                    kwargs[k] = v
            # include version if supported
            if ver is not None:
                for vk in version_keys:
                    if not param_names or vk in param_names:
                        kwargs[vk] = ver
                        break
            if not kwargs:
                continue
            try:
                logger.info("Trying constructor kwargs: %s", list(kwargs.keys()))
                inst = feed_class(**kwargs)
                logger.info("Instantiated feed via kwargs (version=%s)", ver)
                return inst, ver
            except TypeError:
                logger.debug("Constructor kwargs TypeError; will try other forms.")
            except ValueError as ve:
                logger.warning("Constructor ValueError: %s", ve)
                if "Unsupported version" in str(ve):
                    # try next version candidate
                    break
            except Exception as e:
                logger.exception("Constructor raised exception: %s", e)

        # try simple positional
        try:
            if ver is None:
                logger.info("Trying constructor positional (client, token, instruments)")
                inst = feed_class(client, token, instruments)
                logger.info("Instantiated feed via positional (no version).")
                return inst, None
            else:
                logger.info("Trying constructor positional with version: %s", ver)
                inst = feed_class(client, token, instruments, ver)
                logger.info("Instantiated feed via positional with version=%s", ver)
                return inst, ver
        except TypeError:
            logger.debug("Positional instantiation TypeError for version %s", ver)
        except ValueError as ve:
            logger.warning("Constructor ValueError positional: %s", ve)
            if "Unsupported version" in str(ve):
                continue
        except Exception as e:
            logger.exception("Positional constructor exception: %s", e)

    # final fallback: try (client, token) only
    try:
        inst = feed_class(client, token)
        logger.info("Instantiated feed via fallback (client, token).")
        return inst, None
    except Exception as e:
        logger.exception("Fallback instantiation failed: %s", e)

    return None, None

# -----------------
# try calling run-like methods
# -----------------
def try_start_feed_instance(feed):
    """
    Try to start/feed-run the feed instance using common methods.
    Returns (started_bool, error_if_any)
    """
    feed_dir = dir(feed)
    logger.info("feed dir: %s", ", ".join(feed_dir))

    candidates = ["run_forever", "run", "start", "listen", "listen_forever", "serve", "connect_and_listen"]
    for name in candidates:
        if name in feed_dir:
            f = getattr(feed, name)
            if not callable(f):
                continue
            logger.info("Attempting feed.%s()", name)
            # try common signatures
            try:
                # try passing our handler
                try:
                    f(market_feed_handler)
                except TypeError:
                    try:
                        f(on_message=market_feed_handler)
                    except TypeError:
                        try:
                            f(callback=market_feed_handler)
                        except TypeError:
                            f()
                logger.info("Invoked %s (may block).", name)
                return True, None
            except ValueError as ve:
                logger.exception("ValueError invoking %s: %s", name, ve)
                return False, ve
            except Exception as e:
                logger.exception("Exception invoking %s: %s", name, e)
                return True, e
    return False, None

# -----------------
# Main start logic
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

    version_candidates = [None, "1", "v1", "v2", "2.0"]

    backoff = INITIAL_BACKOFF
    while True:
        try:
            feed = None
            used_version = None
            if feed_class:
                feed, used_version = instantiate_feed_simple(feed_class, CLIENT_ID, ACCESS_TOKEN, instruments, version_candidates)
                if not feed:
                    logger.error("Could not instantiate feed_class for any version candidate.")
                    logger.info("module_obj dir: %s", ", ".join(dir(module_obj)))
                    return
                logger.info("Feed instance created (used_version=%s). type=%s", used_version, type(feed))
            else:
                # fallback: module-level function
                if hasattr(module_obj, "market_feed_wss"):
                    try:
                        logger.info("Trying module_obj.market_feed_wss(...) fallback")
                        # many variants: try with callback kw or positional
                        try:
                            module_obj.market_feed_wss(CLIENT_ID, ACCESS_TOKEN, instruments, market_feed_handler)
                        except TypeError:
                            module_obj.market_feed_wss(CLIENT_ID, ACCESS_TOKEN, instruments, callback=market_feed_handler)
                        logger.info("module.market_feed_wss invoked (may block).")
                        # if it blocks, we assume handler active; sleep briefly and continue
                        time.sleep(1)
                        continue
                    except Exception as e:
                        logger.exception("market_feed_wss invocation failed: %s", e)
                logger.error("No feed_class and no working module-level fallback.")
                return

            # try to start the feed instance
            started, err = try_start_feed_instance(feed)
            if started:
                if isinstance(err, ValueError) and "Unsupported version" in str(err):
                    # try next version candidate by continuing loop
                    logger.warning("Unsupported version detected after starting attempt; will retry with other versions.")
                    # try to cleanup
                    try:
                        if hasattr(feed, "disconnect"):
                            feed.disconnect()
                        elif hasattr(feed, "close_connection"):
                            feed.close_connection()
                    except Exception:
                        pass
                    time.sleep(0.5)
                    continue
                # else: feed invoked (may block). when it returns we'll reconnect (loop)
                backoff = INITIAL_BACKOFF
                # small sleep to avoid tight loop if run returned quickly
                time.sleep(1)
                continue
            else:
                logger.error("Could not start feed instance (no run/start method worked). Dumping diagnostic and exiting.")
                logger.info("feed dir: %s", ", ".join(dir(feed)))
                return

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
import signal
def _signal_handler(sig, frame):
    logger.info("Signal %s received; exiting.", sig)
    raise SystemExit()

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# -----------------
# Main
# -----------------
if __name__ == "__main__":
    logger.info("Starting auto-detect DhanHQ bot; HDFC_ID=%s", HDFC_ID)
    start_market_feed()
    logger.info("Bot finished.")
