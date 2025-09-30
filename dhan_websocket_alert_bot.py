import os
import time
import requests
from dhanhq.marketfeed import MarketFeed # <--- आता थेट 'dhanhq.marketfeed' मधून क्लास इम्पोर्ट केला

# --- १. कॉन्फिगरेशन (Configuration) ---
# Environment Variables मधून Secrets ॲक्सेस केले जातील.
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID") 
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN") 
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") 

# HDFC Bank साठी Exchange Segment आणि Security ID.
# Segment: NSE (Equity), Security ID: 1333
HDFC_ID = '1333'
instruments = [
    # Ticker Subscription साठी 'MarketFeed.Ticker' वापरले आहे. (आता ते MarketFeed क्लासमधून ॲक्सेस केले जाईल)
    (MarketFeed.NSE, HDFC_ID, MarketFeed.Ticker) # Ticker (LTP) साठी 1
]

# डेटा साठवण्यासाठी ग्लोबल व्हेरिएबल्स
latest_ltp = {HDFC_ID: None}
last_telegram_send_time = time.time()
SEND_INTERVAL_SECONDS = 60 # 60 सेकंद (1 मिनिट)

# --- २. Telegram फंक्शन ---
def send_telegram_message(ltp_price):
    """LTP घेऊन Telegram Bot API चा वापर करून मेसेज पाठवते."""
    global last_telegram_send_time
    
    timestamp = time.strftime("%H:%M:%S IST")
    
    # ॲलर्ट फॉर्मॅटमध्ये मेसेज तयार करा
    message = f"""*HDFC BANK LTP ALERT!* 🔔
वेळ: {timestamp}
        
*HDFC BANK*
नवीनतम LTP: ₹ *{ltp_price:.2f}*

_हा ॲलर्ट दर 1 मिनिटाने WebSocket Data वर आधारित आहे._"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown' 
    }
    try:
        requests.post(url, data=payload)
        print(f"[{timestamp}] Telegram alert sent: ₹{ltp_price:.2f}")
        last_telegram_send_time = time.time() # वेळ अपडेट करा

    except Exception as e:
        print(f"Error sending Telegram message: {e}")

# --- ३. WebSocket डेटा हँडलर ---
def market_feed_handler(response):
    """WebSocket कडून डेटा मिळाल्यावर कॉल होते."""
    global latest_ltp

    # WebSocket response मध्ये lastTradedPrice (LTP) आहे का ते तपासा
    if response and response.get('securityId') == HDFC_ID and response.get('lastTradedPrice'):
        
        ltp = response['lastTradedPrice']
        latest_ltp[HDFC_ID] = ltp
        print(f"Real-time update: HDFCBANK LTP: {ltp}")

        # Telegram मेसेज पाठवण्याची वेळ झाली आहे का ते तपासा
        current_time = time.time()
        if current_time - last_telegram_send_time >= SEND_INTERVAL_SECONDS:
            send_telegram_message(ltp)

# --- ४. मुख्य WebSocket कनेक्शन ---
def start_market_feed():
    """Dhan Market Feed WebSocket कनेक्शन सुरू करते."""
    if not CLIENT_ID or not ACCESS_TOKEN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Error: Environment Variables (DHAN/TELEGRAM) missing. Please set them in Railway.")
        return

    print("DhanHQ WebSocket Service सुरू होत आहे...")
    print(f"HDFCBANK (ID: {HDFC_ID}) साठी डेटा ॲक्सेस करत आहे.")

    try:
        # ** MarketFeed क्लास योग्यरित्या उपलब्ध आहे **
        market_feed = MarketFeed( 
            CLIENT_ID, 
            ACCESS_TOKEN, 
            instruments, 
            version='2.0'
        )
        
        # WebSocket कनेक्शन चालू करा आणि आलेल्या डेटासाठी handler सेट करा
        market_feed.run_forever(market_feed_handler)

    except Exception as e:
        print(f"\n--- FATAL MARKET FEED ERROR ---")
        print(f"Error: {e}")
        print("5 सेकंद थांबून परत कनेक्शनचा प्रयत्न करत आहे...")
        time.sleep(5)
        start_market_feed() # reconnection logic

if __name__ == "__main__":
    start_market_feed()
