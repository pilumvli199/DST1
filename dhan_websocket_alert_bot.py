#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Robust DhanHQ WebSocket Alert Bot (constructor-signature aware)
- Inspects feed constructor signature and calls with appropriate args/kwargs
- Tries multiple 'version' values and common kw names (avoids passing unsupported 'feed_type' etc.)
- Auto-detects and invokes feed run/start/listen methods
- Telegram alerts throttled, graceful shutdown, backoff
"""

import os
import time
import logging
import traceback
import requests
import inspect
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
# Constructor instantiation helper (signature-aware)
# -----------------
def instantiate_feed(feed_class, client, token, instruments, version_candidates):
    """
    Try to instantiate feed_class using signature introspection.
    Returns (instance, used_version) or (None, None).
    """
    sig = inspect.signature(feed_class)
    params = sig.parameters
    param_names = list(params.keys())
    logger.debug("Constructor parameters: %s", param_names)

    # common kw names mapping attempts
    common_kw_names = [
        ("client_id", "access_token", "instruments"),
        ("clientId", "access_token", "instruments"),
        ("client", "token", "instruments"),
        ("client_id", "token", "instruments"),
        ("client", "access_token", "instruments"),
        ("client_id", "api_key", "instruments"),
    ]

    # candidate version kw names
    version_kw_names = ["version", "v", "feed_type", "feedType"]

    # try no version first, then versions within version_candidates
    # We'll attempt permutations:
    # 1) use kwargs if constructor accepts those names
    # 2) fallback to positional if number of required params matches
    tried_exceptions = []
    for ver in version_candidates:
        # build kwargs based on param presence
        # attempt each common_kw_names mapping
        for mapping in common_kw_names:
            kw = {}
            # mapping is a tuple like (client_kw, token_kw, instruments_kw)
            client_kw, token_kw, instr_kw = mapping
            if client_kw in param_names:
                kw[client_kw] = client
            if token_kw in param_names:
                kw[token_kw] = token
            if instr_kw in param_names:
                kw[instr_kw] = instruments
            # add version if supported and ver not None
            if ver is not None:
                for vk in version_kw_names:
                    if vk in param_names:
                        kw[vk] = ver
                        break

            # if we have nothing to pass, skip mapping
            if not kw:
                continue

            try:
                logger.info("Trying constructor kwargs: %s", list(kw.keys()))
                inst = feed_class(**kw)
                logger.info("Instantiated feed via kwargs: %s", list(kw.keys()))
                return inst, ver
            except TypeError as te:
                # signature mismatch (unexpected kw) — record and try next mapping
                logger.debug("Constructor kwargs TypeError: %s", te)
                tried_exceptions.append(te)
                continue
            except ValueError as ve:
                # e.g., Unsupported version
                logger.warning("Constructor raised ValueError: %s", ve)
                tried_exceptions.append(ve)
                # If "Unsupported version" in message, break to next ver candidate
                if "Unsupported version" in str(ve):
                    logger.info("Detected unsupported version when using kwargs; will try next version candidate.")
                    break
                continue
            except Exception as e:
                logger.exception("Constructor raised exception with kwargs: %s", e)
                tried_exceptions.append(e)
                continue

        # fallback: try positional calling if appropriate
        # Count how many non-default positional-only / required params exist
        required_params = [p for p in params.values() if p.default is inspect._empty and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        # try forming positional list: [client, token, instruments, ver?] if lengths match
        for use_ver in (False, True) if ver is not None else (False,):
            pos_args = []
            # build pos args list up to required length
            # common expectation: (client, token, instruments) optionally + version
            if len(required_params) <= 0:
                continue
            # assemble a candidate
            pos_args = [client, token, instruments]
            if use_ver:
                pos_args.append(ver)
            try:
                logger.info("Trying constructor positional args (len=%d)", len(pos_args))
                inst = feed_class(*pos_args)
                logger.info("Instantiated feed via positional args (len=%d)", len(pos_args))
                return inst, ver if use_ver else None
            except TypeError as te:
                logger.debug("Positional constructor TypeError: %s", te)
                tried_exceptions.append(te)
                continue
            except ValueError as ve:
                logger.warning("Constructor positional ValueError: %s", ve)
                tried_exceptions.append(ve)
                if "Unsupported version" in str(ve):
                    logger.info("Unsupported version detected for positional attempt; will try next version candidate.")
                    break
                continue
            except Exception as e:
                logger.exception("Constructor positional raised exception: %s", e)
                tried_exceptions.append(e)
                continue

    # last resort: try very permissive positional with all candidates concatenated (safe attempt)
    try:
        logger.info("Final fallback: trying feed_class(client, token, instruments) bare positional.")
        inst = feed_class(client, token, instruments)
        return inst, None
    except Exception as e:
        logger.exception("Final fallback constructor attempt failed: %s", e)
        tried_exceptions.append(e)

    logger.error("instantiate_feed: all attempts failed; exceptions: %s", tried_exceptions)
    return None, None

# -----------------
# Utility to try calling a callable with common signatures
# -----------------
def try_call_callable(obj, func_name, handler):
    f = getattr(obj, func_name, None)
    if not callable
