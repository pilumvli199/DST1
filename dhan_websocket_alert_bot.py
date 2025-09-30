import os
import time
import requests
import logging

from dhanhq import DhanFeed
from dhanhq.marketfeed import NSE

# --- १. कॉन्फिगरेशन (Configuration) ---
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- Reliance ID सह बदल (Change to Reliance ID) ---
STOCK_ID = '2885'  # Reliance चा Security ID
STOCK_NAME = "RELIANCE"
# ----------------------------------------------------

SEND_INTERVAL_SECONDS = 60

# ज्या स्क्रिप्टचा डेटा हवा आहे, त्याची लिस्ट
instruments = [
    (NSE, STOCK_ID)
]

last_telegram_send_time = time.time()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- २. Telegram फंक्शन ---
def send_telegram_message(ltp_price):
    global last_telegram_send_time
    
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S IST")
    
    # मेसेजमध्ये स्टॉकचे नाव ديناميك करण्यासाठी बदल
    message = (
        f"🔔 *{STOCK_NAME} LTP ALERT!* 🔔\n\n"
        f"**वेळ:** {timestamp}\n"
        f"**नवीनतम LTP:** ₹ *{ltp_price:.2f}*\n\n"
        f"_हा ॲलर्ट दर {SEND_INTERVAL_SECONDS} सेकंदांनी WebSocket डेटावर आधारित आहे._"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        logging.info(f"Telegram alert sent: {STOCK_NAME} LTP @ ₹{ltp_price:.2f}")
        last_telegram_send_time = time.time()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending Telegram message: {e}")

# --- ३. WebSocket डेटा हँडलर ---
def on_message(message):
    try:
        if message.get('type') == 'Ticker' and message.get('security_id') == STOCK_ID:
            ltp = message.get('ltp')
            if ltp is not None:
                current_time = time.time()
                if current_time - last_telegram_send_time >= SEND_INTERVAL_SECONDS:
                    send_telegram_message(ltp)
    except Exception as e:
        logging.error(f"Error in on_message handler: {e}")

def on_connect():
    logging.info(f"DhanHQ Market Feed Connected Successfully! Subscribed to {STOCK_NAME} ({STOCK_ID}).")

def on_error(error):
    logging.error(f"WebSocket Error: {error}")

# --- ४. मुख्य WebSocket कनेक्शन ---
def start_market_feed():
    if not all([CLIENT_ID, ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Environment variables missing. Please set all required variables.")
        return

    logging.info(f"Starting DhanHQ WebSocket Service for {STOCK_NAME}...")

    feed = DhanFeed(
        client_id=CLIENT_ID,
        access_token=ACCESS_TOKEN,
        instruments=instruments
    )

    feed.on_connect = on_connect
    feed.on_message = on_message
    feed.on_error = on_error
    
    feed.run_forever()

if __name__ == "__main__":
    try:
        start_market_feed()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception as e:
        logging.critical(f"A critical error occurred: {e}")
