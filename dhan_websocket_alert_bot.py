#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robust DhanHQ WebSocket Alert Bot
- Attempts several import styles for dhanhq (handles API differences across versions)
- Tries multiple run/start method signatures
- Sends throttled Telegram alerts
- Graceful shutdown and exponential backoff reconnects
"""

import os
import time
import signal
import sys
import logging
import requests
import traceback
from types import ModuleType
from typing import Any, Callable, Optional

# -----------------
# Config / Env
# -----------------
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Default instrument: HDFC Bank NSE security id 1333
HDFC_ID = os.environ.get("HDFC_ID", "1333")

# seconds between Telegram pushes per symbol
SEND_INTERVAL_SECONDS = int(os.environ.get("SEND_INTERVAL_SECONDS", "60"))

# backoff
INITIAL_BACKOFF = 1
MAX_BACKOFF = 60

# -----------------
# Logging
# -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dhan-bot")

# -----------------
# Globals
# -----------------
_running = True
_last_sent = {}  # per-security timestamp
latest_ltp = {}  # per-security latest ltp

# -----------------
# Telegram helper
# -----------------
def esc_md(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text

def send_telegram_message(security_id: str, ltp_price: float, friendly_name: Optional[str]=None):
    now = time.time()
    last = _last_sent.get(security_id, 0)
    if now - last < SEND_INTERVAL_SECONDS:
        logger.debug("Throttle: skipping send for %s (%.1fs left)", security_id, SEND_INTERVAL_SECONDS - (now - last))
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials missing; cannot send message")
        return

    timestamp = time.strftime("%H:%M:%S IST")
    symbol_name = friendly_name or f"Security {security_id}"

    message = (
        f"*{esc_md('HDFC BANK LTP ALERT!')}* 🔔\n"
        f"वेळ: {esc_md(timestamp)}\n\n"
        f"*{esc_md(symbol_name)}*\n"
        f"नवीनतम LTP: ₹ *{esc_md(f'{ltp_price:.2f}')}*\n\n"
        f"_हा ॲलर्ट दर {SEND_INTERVAL_SECONDS} सेकंदाने WebSocket Data वर आधारित आहे._"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}

    try:
        resp = requests.post(url, data=payload, timeout=8)
        if resp.ok:
            logger.info("Telegram alert sent for %s: ₹%.2f", security_id, ltp_price)
            _last_sent[security_id] = time.time()
        else:
            logger.warning("Telegram API error %s: %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        logger.exception("Failed to send Telegram message: %s", e)

# -----------------
# Dynamic dhanhq import & adapter
# -----------------
def debug_module(mod: ModuleType):
    try:
        names = dir(mod)
    except Exception:
        names = []
    logger.info("dhanhq module contents: %s", ", ".join(names))

def locate_feed_class():
    """
    Try multiple import patterns and return a tuple (module_object, FeedClass, helpers)
    helpers: dict with attributes like NSE, TICKER/Ticker names if present
    """
    # Try a few import styles
    try:
        # 1) try root-level MarketFeed
        import dhanhq as dh
        debug_module(dh)
        if hasattr(dh, "MarketFeed"):
            logger.info("Using dhanhq.MarketFeed (root)")
            return dh, getattr(dh, "MarketFeed"), {
                "NSE": getattr(dh, "NSE", None),
                "TICKER": getattr(dh, "TICKER", getattr(dh, "Ticker", None))
            }
        # 2) maybe dhanhq.marketfeed exists as submodule
        if hasattr(dh, "marketfeed"):
            mf = dh.marketfeed
            debug_module(mf)
            # common names: MarketFeed, DhanFeed, marketfeed (module)
            for name in ("MarketFeed", "DhanFeed", "DhanMarketFeed", "MarketSocket"):
                if hasattr(mf, name):
                    logger.info("Using dhanhq.marketfeed.%s", name)
                    return mf, getattr(mf, name), {
                        "NSE": getattr(mf, "NSE", None),
                        "TICKER": getattr(mf, "TICKER", getattr(mf, "Ticker", None))
                    }
            # if none found, but module exists return module as fallback
            logger.info("marketfeed module exists but no common class found; returning module fallback")
            return mf, None, {"NSE": getattr(mf, "NSE", None), "TICKER": getattr(mf, "TICKER", getattr(mf, "Ticker", None))}
    except Exception as e:
        logger.debug("Import dhanhq root failed: %s", e)

    # 3) try direct import from submodule
    try:
        import importlib
        mf = importlib.import_module("dhanhq.marketfeed")
        debug_module(mf)
        for name in ("MarketFeed", "DhanFeed", "DhanMarketFeed", "MarketSocket"):
            if hasattr(mf, name):
                logger.info("Using dhanhq.marketfeed.%s (importlib)", name)
                return mf, getattr(mf, name), {"NSE": getattr(mf, "NSE", None), "TICKER": getattr(mf, "TICKER", getattr(mf, "Ticker", None))}
        return mf, None, {"NSE": getattr(mf, "NSE", None), "TICKER": getattr(mf, "TICKER", getattr(mf, "Ticker", None))}
    except Exception as e:
        logger.debug("importlib dhanhq.marketfeed failed: %s", e)

    # 4) failed to find anything
    return None, None, {}

# -----------------
# Message handler (library-agnostic)
# -----------------
def market_feed_handler(message: Any):
    """
    Generic handler — many dhanhq libs pass dict-like messages.
    We'll attempt to extract securityId/ltp from common keys.
    """
    try:
        # If message is bytes/str try to parse JSON
        if isinstance(message, (bytes, str)):
            import json
            try:
                message = json.loads(message)
            except Exception:
                # keep as-is
                pass

        # message could be a dict or custom object
        security_id = None
        ltp = None

        if isinstance(message, dict):
            # common keys
            security_id = message.get("securityId") or message.get("symbol") or message.get("security_id") or message.get("s")
            ltp = message.get("lastTradedPrice") or message.get("ltp") or message.get("last_price") or message.get("last")
        else:
            # try attribute access
            if hasattr(message, "securityId"):
                security_id = getattr(message, "securityId", None)
            if hasattr(message, "lastTradedPrice"):
                ltp = getattr(message, "lastTradedPrice", None)

        if not security_id:
            # possibly nested structure: try common nested fields
            if isinstance(message, dict):
                for k in ("data", "payload", "tick", "update"):
                    nested = message.get(k)
                    if isinstance(nested, dict) and not security_id:
                        security_id = nested.get("securityId") or nested.get("symbol")
                        ltp = nested.get("lastTradedPrice") or nested.get("ltp") or nested.get("last_price")
                        if security_id:
                            break

        if security_id:
            security_id = str(security_id)

        if ltp is not None:
            try:
                ltp = float(ltp)
            except Exception:
                logger.debug("LTP not numeric: %s", ltp)
                ltp = None

        if security_id and ltp is not None:
            latest_ltp[security_id] = ltp
            logger.info("Real-time update: %s LTP: %.2f", security_id, ltp)
            send_telegram_message(security_id, ltp, friendly_name="HDFC BANK" if security_id == HDFC_ID else None)
        else:
            logger.debug("Ignored message (no securityId/ltp): %s", message)

    except Exception as ex:
        logger.exception("Error in market_feed_handler: %s\n%s", ex, traceback.format_exc())

# -----------------
# Start feed with flexible API signatures
# -----------------
def start_market_feed():
    if not CLIENT_ID or not ACCESS_TOKEN:
        logger.error("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN missing. Set env vars.")
        return

    module_obj, FeedClass, helpers = locate_feed_class()
    if module_obj is None and FeedClass is None:
        logger.error("Could not find dhanhq.marketfeed module or feed class. Inspect installed package.")
        logger.info("Ensure 'dhanhq' is installed and correct version. You can inspect contents by running a small script.")
        return

    # Decide NSE & TICKER names
    NSE = helpers.get("NSE")
    TICKER = helpers.get("TICKER")

    # Build instruments tuple: try to use names from module if available else fallback to constants
    # Typical instrument format: (market_segment_const, security_id, ticker_const)
    if NSE is None or TICKER is None:
        # fallback: use strings; many implementations accept simple tuples like ("NSE", "1333", "TICKER")
        instruments = [("NSE", HDFC_ID, "TICKER")]
    else:
        instruments = [(NSE, HDFC_ID, TICKER)]

    logger.info("Instruments: %s", instruments)

    backoff = INITIAL_BACKOFF
    while _running:
        try:
            logger.info("Creating feed instance using detected API...")
            feed = None
            # If FeedClass is a class or callable, try to instantiate with common signatures
            if FeedClass:
                try:
                    # Try signature: FeedClass(client_id, access_token, instruments, version=...)
                    try:
                        feed = FeedClass(CLIENT_ID, ACCESS_TOKEN, instruments, version="2.0")
                        logger.info("Instantiated FeedClass with signature (client, token, instruments, version)")
                    except TypeError:
                        # Try signature without version
                        feed = FeedClass(CLIENT_ID, ACCESS_TOKEN, instruments)
                        logger.info("Instantiated FeedClass with signature (client, token, instruments)")
                except Exception as e:
                    logger.exception("Failed to instantiate FeedClass: %s", e)
                    feed = None
            else:
                # If we only have module (no class), try to use module-level factory or function names
                try:
                    if hasattr(module_obj, "DhanFeed"):
                        feed = module_obj.DhanFeed(CLIENT_ID, ACCESS_TOKEN, instruments)
                    elif hasattr(module_obj, "MarketFeed"):
                        feed = module_obj.MarketFeed(CLIENT_ID, ACCESS_TOKEN, instruments)
                    else:
                        logger.warning("Module provided but no known feed constructor found.")
                except Exception as e:
                    logger.exception("Failed to create feed from module: %s", e)
                    feed = None

            if feed is None:
                logger.error("Could not create feed instance. Aborting this cycle. Inspect dhanhq package.")
                return

            # Now attempt to run the feed using common run/start method names.
            # We'll try the following common variants in order:
            # - feed.run_forever(handler) or feed.run_forever(on_message=handler)
            # - feed.run(on_message=handler) or feed.run(handler)
            # - feed.start(handler) or feed.start(on_message=handler)
            # - feed.connect(...) then feed.listen(...) (less common)
            logger.info("Attempting to start feed (trying multiple run signatures)...")
            started = False
            try:
                if hasattr(feed, "run_forever"):
                    try:
                        # many libraries accept handler as single positional
                        feed.run_forever(market_feed_handler)
                        started = True
                    except TypeError:
                        # maybe expects named arg
                        try:
                            feed.run_forever(on_message=market_feed_handler)
                            started = True
                        except TypeError:
                            try:
                                feed.run_forever(callback=market_feed_handler)
                                started = True
                            except Exception:
                                started = False
                if not started and hasattr(feed, "run"):
                    try:
                        feed.run(on_message=market_feed_handler)
                        started = True
                    except TypeError:
                        try:
                            feed.run(market_feed_handler)
                            started = True
                        except Exception:
                            started = False
                if not started and hasattr(feed, "start"):
                    try:
                        feed.start(market_feed_handler)
                        started = True
                    except TypeError:
                        try:
                            feed.start(on_message=market_feed_handler)
                            started = True
                        except Exception:
                            started = False
                # last resort: if feed exposes 'connect' and 'listen'
                if not started and hasattr(feed, "connect") and hasattr(feed, "listen"):
                    try:
                        feed.connect()
                        feed.listen(market_feed_handler)
                        started = True
                    except Exception:
                        started = False

                if started:
                    # If one of the run methods returns (with no exception), reset backoff and continue loop
                    backoff = INITIAL_BACKOFF
                    logger.info("Feed run method returned (exited). Reconnecting loop will continue.")
                    # small sleep to avoid tight loop if run returns immediately
                    time.sleep(1)
                else:
                    logger.error("Could not start feed: no known run/start signature worked.")
                    return

            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt during feed run; exiting.")
                break

        except Exception as ex:
            logger.exception("Unexpected error in market feed loop: %s", ex)
            sleep_time = min(MAX_BACKOFF, backoff)
            logger.info("Reconnect in %.1f seconds...", sleep_time)
            time.sleep(sleep_time)
            backoff = min(MAX_BACKOFF, backoff * 2 if backoff > 0 else INITIAL_BACKOFF)

    logger.info("Market feed loop terminated.")


# -----------------
# Signals
# -----------------
def _signal_handler(sig, frame):
    global _running
    logger.info("Signal %s received. Shutting down...", sig)
    _running = False

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# -----------------
# Main entry
# -----------------
if __name__ == "__main__":
    logger.info("Starting DhanHQ WebSocket Alert Bot")
    logger.info("HDFC_ID=%s SEND_INTERVAL_SECONDS=%s", HDFC_ID, SEND_INTERVAL_SECONDS)
    start_market_feed()
    logger.info("Bot exited.")
