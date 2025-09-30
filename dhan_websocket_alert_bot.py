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

HDFC_ID = '1333' 
SEND_INTERVAL_SECONDS = 60

instruments = [
    (NSE, HDFC_ID)
]

last_telegram_send_time = time.time()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- २. Telegram फंक्शन ---
def send_telegram_message(ltp_price):
    global last_telegram_send_time
    
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S IST")
    
    message = (
        f"🔔 *HDFC BANK LTP ALERT!* 🔔\n\n"
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
        logging.info(f"Telegram alert sent: HDFCBANK LTP @ ₹{ltp_price:.2f}")
        last_telegram_send_time = time.time()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending Telegram message: {e}")

# --- ३. WebSocket डेटा हँडलर ---
def on_message(message):
    try:
        if message.get('type') == 'Ticker' and message.get('security_id') == HDFC_ID:
            ltp = message.get('ltp')
            if ltp is not None:
                current_time = time.time()
                if current_time - last_telegram_send_time >= SEND_INTERVAL_SECONDS:
                    send_telegram_message(ltp)
    except Exception as e:
        logging.error(f"Error in on_message handler: {e}")

def on_connect():
    logging.info("DhanHQ Market Feed Connected Successfully!")

def on_error(error):
    logging.error(f"WebSocket Error: {error}")

# --- ४. मुख्य WebSocket कनेक्शन (येथे बदल केला आहे) ---
def start_market_feed():
    """Dhan Market Feed WebSocket कनेक्शन सुरू करते."""
    if not all([CLIENT_ID, ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Environment variables missing. Please set all required variables.")
        return

    logging.info("Starting DhanHQ WebSocket Service for HDFCBANK...")

    # 1. फक्त आवश्यक पॅरामीटर्स देऊन ऑब्जेक्ट तयार करा
    feed = DhanFeed(
        client_id=CLIENT_ID,
        access_token=ACCESS_TOKEN,
        instruments=instruments
    )

    # 2. आता कॉलबॅक फंक्शन्स जोडा
    feed.on_connect = on_connect
    feed.on_message = on_message
    feed.on_error = on_error
    
    # 3. WebSocket कनेक्शन सुरू करा
    feed.run_forever()

if __name__ == "__main__":
    try:
        start_market_feed()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception as e:
        logging.critical(f"A critical error occurred: {e}")
