import os
import time
import requests
import traceback
import logging

# dhanhq मॉड्यूल आणि क्लासेस इम्पोर्ट करा
# MarketFeed: मुख्य WebSocket क्लास
# NSE, Ticker: Exchange Segment आणि Subscription Type Constants (dhanhq च्या उदाहरणांनुसार)
try:
    from dhanhq.marketfeed import marketfeed
    MarketFeed = marketfeed
    # Constants: marketfeed.NSE, marketfeed.Ticker वापरा
    # जर MarketFeed क्लास marketfeed मॉड्यूलमध्ये लहान अक्षरात असेल, तर ते ॲक्सेस करण्याचा प्रयत्न करा
except ImportError:
    try:
        from dhanhq import MarketFeed, NSE, Ticker
    except ImportError:
        # Fallback to older import method if the above fails
        import dhanhq.marketfeed as marketfeed_module
        MarketFeed = marketfeed_module.MarketFeed
        NSE = marketfeed_module.NSE
        Ticker = marketfeed_module.Ticker

# DhanHQ च्या उदाहरणांनुसार NSE आणि Ticker Constants शोधणे (सुरक्षित पद्धत)
try:
    import dhanhq.marketfeed as marketfeed_module
    marketfeed_class = getattr(marketfeed_module, 'MarketFeed', None) or getattr(marketfeed_module, 'marketfeed', None)
    if not marketfeed_class:
        raise ImportError("Could not find MarketFeed class.")
    
    # Constants (NSE, Ticker, etc.) हे मॉड्यूलमध्ये थेट ॲट्रिब्यूट म्हणून असण्याची शक्यता आहे.
    NSE = getattr(marketfeed_module, 'NSE', None)
    Ticker = getattr(marketfeed_module, 'Ticker', None) or getattr(marketfeed_module, 'TICKER', None)
    
except ImportError as e:
    # जर dhanhq लायब्ररी खूप जुनी असेल किंवा रचना वेगळी असेल
    logging.error(f"Failed deep import from dhanhq.marketfeed: {e}")
    raise SystemExit(1)

# ----------------------------------------------------
# इथे आपण Constants आणि Class अचूकपणे ॲक्सेस केल्याची खात्री करू
# ----------------------------------------------------

# --- १. कॉन्फिगरेशन (Configuration) ---
CLIENT_ID = os.environ.get("DHAN_CLIENT_ID") 
ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN") 
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") 
HDFC_ID = '1333'
SEND_INTERVAL_SECONDS = 60

# इंस्ट्रुमेंट्स लिस्ट (Constants ची खात्री केल्यानंतर)
if NSE is None or Ticker is None:
    # जर Constants सापडले नाहीत, तर प्रोग्राम थांबेल
    logging.error("NSE or Ticker constants are not found in dhanhq module. Exiting.")
    raise SystemExit(1)

instruments = [
    # आता 'NSE' आणि 'Ticker' हे व्हेरिएबल्स म्हणून वापरले जातील
    (NSE, HDFC_ID, Ticker)
]

# डेटा साठवण्यासाठी ग्लोबल व्हेरिएबल्स
latest_ltp = {HDFC_ID: None}
last_telegram_send_time = time.time()

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
        # logging.info(f"[{timestamp}] Telegram alert sent: ₹{ltp_price:.2f}")
        last_telegram_send_time = time.time() # वेळ अपडेट करा

    except Exception as e:
        logging.error(f"Error sending Telegram message: {e}")

# --- ३. WebSocket डेटा हँडलर ---
def market_feed_handler(response):
    """WebSocket कडून डेटा मिळाल्यावर कॉल होते."""
    global latest_ltp

    # WebSocket response मध्ये lastTradedPrice (LTP) आहे का ते तपासा
    if response and response.get('securityId') == HDFC_ID and response.get('lastTradedPrice'):
        
        ltp = response['lastTradedPrice']
        latest_ltp[HDFC_ID] = ltp
        # logging.info(f"Real-time update: HDFCBANK LTP: {ltp}")

        # Telegram मेसेज पाठवण्याची वेळ झाली आहे का ते तपासा
        current_time = time.time()
        if current_time - last_telegram_send_time >= SEND_INTERVAL_SECONDS:
            send_telegram_message(ltp)

# --- ४. मुख्य WebSocket कनेक्शन ---
def start_market_feed():
    """Dhan Market Feed WebSocket कनेक्शन सुरू करते."""
    if not CLIENT_ID or not ACCESS_TOKEN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Error: Environment Variables (DHAN/TELEGRAM) missing. Please set them in Railway.")
        return

    logging.info("DhanHQ WebSocket Service सुरू होत आहे...")
    logging.info(f"HDFCBANK (ID: {HDFC_ID}) साठी डेटा ॲक्सेस करत आहे.")

    backoff = 1
    while True:
        try:
            # मार्केट फीड क्लासचा वापर करणे
            market_feed = marketfeed_class( 
                CLIENT_ID, 
                ACCESS_TOKEN, 
                instruments, 
                version='2.0'
            )
            
            # WebSocket कनेक्शन चालू करा आणि आलेल्या डेटासाठी handler सेट करा
            market_feed.run_forever(market_feed_handler)

        except Exception as e:
            # HTTP 400 मिळाल्यास reconnection logic
            logging.error(f"\n--- FATAL MARKET FEED ERROR: {e} ---")
            logging.warning(f"5 सेकंद थांबून परत कनेक्शनचा प्रयत्न करत आहे. (Backoff: {backoff}s)")
            
            # 400 त्रुटीसाठी, टोकन एक्सपायरीची शक्यता जास्त आहे.
            if "HTTP 400" in str(e) or "InvalidStatus" in str(e):
                 logging.error("Possible Access Token Expiry or Invalid Token. Please update DHAN_ACCESS_TOKEN in Railway variables.")
            
            time.sleep(backoff)
            backoff = min(60, backoff * 2)
            
        except KeyboardInterrupt:
            logging.info("Exiting bot.")
            break

if __name__ == "__main__":
    start_market_feed()
