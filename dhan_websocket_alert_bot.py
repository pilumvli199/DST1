import os
import time
import requests
import logging

# योग्य dhanhq क्लास आणि कॉन्स्टंट्स इम्पोर्ट करा (येथे बदल केला आहे)
from dhanhq import DhanFeed
from dhanhq.marketfeed import NSE

# --- १. कॉन्फिगरेशन (Configuration) ---
# हे व्हेरिएबल्स तुमच्या Railway किंवा लोकल एनवायरमेंटमध्ये सेट करा
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# HDFC Bank चा NSE मधील Security ID
HDFC_ID = '1333' 
# किती सेकंदांनी टेलिग्राम मेसेज पाठवायचा
SEND_INTERVAL_SECONDS = 60

# ज्या स्क्रिप्ट्सचा डेटा हवा आहे, त्यांची लिस्ट
instruments = [
    (NSE, HDFC_ID)
]

# डेटा साठवण्यासाठी ग्लोबल व्हेरिएबल्स
last_telegram_send_time = time.time()

# लॉगिंग सेटअप
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- २. Telegram फंक्शन ---
def send_telegram_message(ltp_price):
    """LTP घेऊन Telegram Bot API चा वापर करून मेसेज पाठवते."""
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
    """WebSocket कडून डेटा मिळाल्यावर हा फंक्शन आपोआप कॉल होतो."""
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
    """WebSocket कनेक्ट झाल्यावर हा फंक्शन कॉल होतो."""
    logging.info("DhanHQ Market Feed Connected Successfully!")

def on_error(error):
    """WebSocket मध्ये एरर आल्यास हा फंक्शन कॉल होतो."""
    logging.error(f"WebSocket Error: {error}")

# --- ४. मुख्य WebSocket कनेक्शन ---
def start_market_feed():
    """Dhan Market Feed WebSocket कनेक्शन सुरू करते."""
    if not all([CLIENT_ID, ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.error("Environment variables missing. Please set all required variables.")
        return

    logging.info("Starting DhanHQ WebSocket Service for HDFCBANK...")

    feed = DhanFeed(
        client_id=CLIENT_ID,
        access_token=ACCESS_TOKEN,
        instruments=instruments,
        on_connect=on_connect,
        on_message=on_message,
        on_error=on_error
    )
    
    feed.run_forever()

if __name__ == "__main__":
    try:
        start_market_feed()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception as e:
        logging.critical(f"A critical error occurred: {e}")
