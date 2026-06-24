
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as firebase_firestore
from google.cloud import firestore
from utils import send_text, send_buttons, send_image, send_template
import google.generativeai as genai
import os
import json
import time
import hmac
import hashlib
import threading
import re
from collections import defaultdict

app = Flask(__name__)
CORS(app)

# -------------------------
# EMOJI CONSTANTS
# All emojis are defined here as Unicode escapes — no emoji literals in code.
# -------------------------
E_BOX       = "\U0001F4E6"  # 📦
E_ID        = "\U0001F194"  # 🆔
E_BASKET    = "\U0001F9BA"  # 🧺
E_CHART     = "\U0001F4CA"  # 📊
E_CALENDAR  = "\U0001F4C5"  # 📅
E_TRUCK     = "\U0001F69A"  # 🚚
E_PHONE     = "\U0001F4DE"  # 📞
E_EMAIL     = "\U0001F4E7"  # 📧
E_CHAT      = "\U0001F4AC"  # 💬
E_CHECK     = "\u2705"      # ✅
E_CROSS     = "\u274C"      # ❌
E_WARN      = "\u26A0\uFE0F" # ⚠️
E_PIN       = "\U0001F4CD"  # 📍
E_NOMAIL    = "\U0001F4ED"  # 📭
E_SMILE     = "\U0001F60A"  # 😊
E_WAVE      = "\U0001F44B"  # 👋
E_PARTY     = "\U0001F389"  # 🎉
E_TIMER     = "\u23F1"      # ⏱
E_MONEY     = "\U0001F4B0"  # 💰
E_ROBOT     = "\U0001F916"  # 🤖
E_HOT       = "\u2668\uFE0F" # ♨️
E_SHIRT     = "\U0001F455"  # 👕
E_FORMAL    = "\U0001F454"  # 👔
E_DRESS     = "\U0001F457"  # 👗
E_KIMONO    = "\U0001F458"  # 👘
E_SARI      = "\U0001F97B"  # 🥻
E_COAT      = "\U0001F9E5"  # 🧥
E_SCARF     = "\U0001F9E3"  # 🧣
E_GLOVE     = "\U0001F9E4"  # 🧤
E_YARN      = "\U0001F9F6"  # 🧶
E_TSHIRT    = "\U0001F45A"  # 👚
E_PANTS     = "\U0001F456"  # 👖
E_SHOE      = "\U0001F45F"  # 👟
E_TOPHAT    = "\U0001F3A9"  # 🎩
E_SUIT      = "\U0001F935"  # 🤵
E_BED       = "\U0001F6CF"  # 🛏
E_WINDOW    = "\U0001FA9F"  # 🪟

# -------------------------
# FIREBASE CONNECTIONS
# -------------------------
firebase_key = json.loads(os.environ["FIREBASE_KEY"])
cred = credentials.Certificate(firebase_key)
firebase_admin.initialize_app(cred)

# 1. Dashboard Database (andesdb)
db_andes = firestore.Client(project=firebase_key["project_id"], database="andesdb", credentials=cred.get_credential())
# 2. Rider App Database (Default)
db_default = firebase_firestore.client()

print("\nConnected to Both Databases (andesdb & default)\n")

# -------------------------
# STARTUP VALIDATION
# -------------------------
REQUIRED_ENV_VARS = ["FIREBASE_KEY", "WHATSAPP_TOKEN", "PHONE_NUMBER_ID", "VERIFY_TOKEN"]
missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing:
    raise EnvironmentError(f"STARTUP FAILED: Missing required environment variables: {missing}")
print("All required environment variables verified")

# -------------------------
# SECURITY LAYER
# -------------------------

# 1. Per-user rate limiter (in-memory)
_rate_limit_store = defaultdict(list)
RATE_LIMIT_MAX = 10       # max messages per user
RATE_LIMIT_WINDOW = 60    # per 60 seconds

# Injection attempt tracker -- block repeat offenders
_injection_strikes = defaultdict(int)
INJECTION_BLOCK_THRESHOLD = 3  # block after 3 attempts

# -------------------------
# DUPLICATE MESSAGE GUARD
# -------------------------
# Meta retries the webhook if it doesn't get a fast 200 OK.
# We deduplicate on the WhatsApp message ID to prevent the bot
# from sending the same reply 2-3 times.
_processed_msg_ids = {}          # { msg_id: timestamp }
DUPLICATE_TTL = 60               # seconds -- discard cache entries older than this

def is_duplicate_message(msg_id):
    """Returns True if this message ID was already processed recently."""
    now = time.time()
    # Evict stale entries to prevent unbounded memory growth
    expired = [k for k, t in _processed_msg_ids.items() if now - t > DUPLICATE_TTL]
    for k in expired:
        del _processed_msg_ids[k]
    if msg_id in _processed_msg_ids:
        return True  # Already handled -- this is a retry
    _processed_msg_ids[msg_id] = now
    return False

def is_rate_limited(phone):
    """Returns True if user is sending too many messages."""
    now = time.time()
    _rate_limit_store[phone] = [t for t in _rate_limit_store[phone] if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[phone]) >= RATE_LIMIT_MAX:
        return True
    _rate_limit_store[phone].append(now)
    return False

def mask_phone(phone):
    """Masks phone number in logs for privacy: 919918XXXXXX"""
    return phone[:4] + "X" * (len(phone) - 8) + phone[-4:] if len(phone) > 8 else "****"

# 2. Meta Webhook Signature Verification
def verify_webhook_signature(req):
    """Verifies the request is genuinely from Meta using HMAC-SHA256."""
    app_secret = os.environ.get("APP_SECRET", "")
    if not app_secret:
        return True  # Skip check if secret not configured (dev mode)
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature:
        return False
    payload = req.get_data()
    mac = hmac.new(app_secret.encode("utf-8"), payload, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(signature, expected)

# 3. AI Input Sanitizer (Prompt Injection + Length Guard)
MAX_AI_INPUT_LENGTH = 400  # chars -- prevents token quota abuse
PROMPT_INJECTION_PATTERNS = [
    "ignore all previous", "ignore previous", "system prompt",
    "you are now", "act as", "jailbreak", "pretend you are",
    "forget your instructions", "new instruction", "disregard",
    "override", "bypass", "your real instructions"
]

def sanitize_ai_input(text):
    """Returns sanitized input or None if injection attempt detected."""
    text = text.strip()[:MAX_AI_INPUT_LENGTH]  # Truncate long inputs
    lower = text.lower()
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern in lower:
            return None  # Reject injection attempt
    return text

def is_injection_blocked(phone):
    """Returns True if this user has hit the injection attempt threshold."""
    return _injection_strikes[phone] >= INJECTION_BLOCK_THRESHOLD

def record_injection_attempt(phone):
    """Increments injection strike counter for this user."""
    _injection_strikes[phone] += 1
    print(f"Security: Injection strike {_injection_strikes[phone]}/{INJECTION_BLOCK_THRESHOLD} for {mask_phone(phone)}")

# -------------------------
# GEMINI AI CONNECTION & CONTEXT
# -------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

    # HOW TO FEED CONTEXT: Edit this block to teach the AI about your business.
    # Note: No emoji literals here -- Gemini outputs emojis on its own based on instructions.
    business_context = """
You are a friendly, professional WhatsApp support assistant for Andes Laundry, based in Pune.
Always reply in a short, conversational, WhatsApp-friendly tone. Use relevant emojis in your replies.

=== SERVICE AREA ===
We serve all areas of Pune: Viman Nagar, Kothrud, Hadapsar, Hinjewadi, Wakad, Baner, Aundh, Koregaon Park, Shivajinagar, and more.

=== STANDARD SERVICES (Per KG) ===
- Wash & Fold: Rs.59/kg -- for t-shirts, shorts, track pants, pajamas, undergarments, daily casual wear.
- Wash & Iron: Rs.89/kg -- for office shirts, formal trousers, kurtas, cotton formals.
- Iron Only: Rs.10/piece -- just ironing, no washing.

=== DRY CLEANING -- INDIAN WEAR (Per Piece) ===
- Kurta Pajama: Rs.149/set
- Kurta: Rs.99/piece
- Kurti: Rs.99/piece
- Saree: Rs.199/piece
- Saree with Embroidery: Rs.499/piece
- Blouse: Rs.69/piece
- Lehenga: Rs.349/piece
- Designer Lehenga: Rs.699/piece (up to Rs.1000 for heavy designer work)
- Dhoti: Rs.69/piece
- Sherwani: Rs.349/piece
- Pagdi: Rs.79/piece
- Salwar: Rs.69/piece
- Sharara: Rs.299/piece
- Dupatta: Rs.49/piece

=== DRY CLEANING -- WINTER & OUTERWEAR ===
- Sweater: Rs.149/piece
- Hoodie: Rs.149/piece
- Muffler: Rs.99/piece
- Shawl: Rs.199/piece
- Winter Coat: Rs.299/piece
- Leather Jacket: Rs.699/piece
- Puffer Jacket: Rs.249/piece
- Normal Jacket: Rs.149/piece
- Woolen Gloves: Rs.49/pair
- Leather Gloves: Rs.329/pair

=== DRY CLEANING -- WESTERN & FORMAL WEAR ===
- Suit (full set): Rs.449/set
- Blazer: Rs.249/piece
- Trouser: Rs.49/piece
- Shirt & Pant Combo: Rs.49/set
- Jeans: Rs.59/piece
- Top: Rs.49/piece
- Joggers: Rs.149/piece
- Skirt: Rs.49/piece

=== DRY CLEANING -- HOME FURNISHINGS ===
- Window Curtain: Rs.149/piece
- Door Curtain: Rs.199/piece
- Single Bedsheet / Blanket: Rs.149/piece
- Double Bedsheet: Rs.289/piece
- Pillow Cover: Rs.29/piece

=== SHOE CLEANING ===
- Sports Shoes: Rs.199/pair
- Loafers / Sneakers: Rs.249/pair

=== TURNAROUND TIMES ===
- Andes Regular: 24 to 48 hours guaranteed.
- Andes Instant: 3-hour express clean and delivery.

=== KEY FEATURES ===
- Free Pickup & Delivery on all orders.
- Track orders via the Andes App on Google Play Store.

=== SUPPORT ===
- Phone: +91 86260 76578
- Email: care@andes.co.in
- Or type 'support' to raise a ticket.

=== RULES YOU MUST FOLLOW ===
1. When a user asks about the price of a specific item (e.g. "shirt price", "saree dry cleaning cost", "jacket rate"), give the EXACT price from the list above. Do not say 'pricing varies'.
2. If a user asks which service is right for their item, recommend the correct service AND state the price.
3. Keep answers short (2-5 lines max). Use bullet points for multiple items.
4. NEVER invent prices or services not in this list.
5. If a user wants to book/schedule a pickup, tell them to type 'menu' or 'start'.
6. Always be warm, helpful, and professional.

=== EXAMPLE Q&A (use these as response templates) ===
Q: What is the price for dry cleaning a shirt?
A: Shirt dry cleaning is not listed separately -- for shirts we recommend Wash & Iron at Rs.89/kg, or Shirt & Pant Combo dry cleaning at Rs.49/set. Type *menu* to book!

Q: Saree dry cleaning price?
A: Saree dry cleaning: Rs.199/piece. If it has embroidery, it is Rs.499/piece. Type *menu* to schedule a pickup!

Q: How much for a leather jacket?
A: Leather Jacket dry cleaning: Rs.699/piece. Type *menu* to schedule a pickup!

Q: What is the price for a suit?
A: Suit dry cleaning: Rs.449/set (full suit). Blazer alone: Rs.249. Type *menu* to book!

Q: Shoe cleaning price?
A: Sports Shoes: Rs.199/pair | Loafers/Sneakers: Rs.249/pair. Type *menu* to schedule!

Q: What is the price for a lehenga?
A: Lehenga dry cleaning: Rs.349/piece. Designer Lehenga: Rs.699 to Rs.1000/piece. Type *menu* to book!
    """

    # Valid models with confirmed free-tier quota (as of 2025).
    # Ordered by preference -- fastest/highest RPD first.
    GEMINI_MODELS = [
        "gemini-2.0-flash-lite",          # Best free tier: 1500 RPD, 1M TPD
        "gemini-2.0-flash",               # 200 RPD free
        "gemini-1.5-flash",               # 1500 RPD free -- reliable fallback
        "gemini-1.5-flash-8b",            # 1500 RPD free -- lightest model
        "gemini-2.5-flash-preview-05-20", # Preview -- limited free quota
    ]
    gemini_model = None

    for model_name in GEMINI_MODELS:
        try:
            candidate = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=business_context
            )
            # No test call here - saves free quota on every server restart
            gemini_model = candidate
            print(f"Gemini AI Ready: {model_name}")
            break
        except Exception as e:
            print(f"Model '{model_name}' init failed: {e}. Trying next...")

    if not gemini_model:
        print("WARNING: All Gemini models failed. AI Chatbot disabled.")
else:
    gemini_model = None
    print("WARNING: Gemini API Key not found. AI Chatbot disabled.")

# -------------------------
# STATE MANAGEMENT
# -------------------------
def get_user_state(phone):
    try:
        doc = db_andes.collection("bot_sessions").document(phone).get()
        return doc.to_dict() if doc.exists else {}
    except: return {}

def update_user_state(phone, state):
    try:
        db_andes.collection("bot_sessions").document(phone).set(state)
    except: pass

def clear_user_state(phone):
    try:
        db_andes.collection("bot_sessions").document(phone).delete()
    except: pass

def get_user_profile(phone):
    try:
        doc = db_andes.collection("users").document(phone).get()
        return doc.to_dict() if doc.exists else None
    except: return None

def update_user_profile(phone, data):
    try:
        db_andes.collection("users").document(phone).set(data, merge=True)
    except: pass

# -------------------------
# HELPERS
# -------------------------

# Cache for is_bot_paused() -- avoids a Firestore read on every single message.
# Refreshes every 30 seconds. If an operator pauses the bot, it takes effect within 30s.
_bot_paused_cache = {}       # { phone: (paused_bool, timestamp) }
BOT_PAUSED_CACHE_TTL = 30   # seconds

def is_bot_paused(phone):
    """Checks if the human operator has paused the bot for this phone.
    Result is cached for BOT_PAUSED_CACHE_TTL seconds to avoid a Firestore
    round-trip on every message.
    """
    now = time.time()
    if phone in _bot_paused_cache:
        val, ts = _bot_paused_cache[phone]
        if now - ts < BOT_PAUSED_CACHE_TTL:
            return val   # Serve from cache -- no network call
    try:
        doc = db_andes.collection("bot_settings").document(phone).get()
        result = doc.to_dict().get("paused", False) if doc.exists else False
    except:
        result = False
    _bot_paused_cache[phone] = (result, now)
    return result

def _log_chat_worker(phone, message_text, sender):
    """Background worker that writes a chat log entry to Firestore."""
    try:
        db_andes.collection("chat_history").add({
            "phone": phone,
            "message": message_text,
            "sender": sender,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except: pass

def log_chat(phone, message_text, sender):
    """Logs conversation to andesdb for the dashboard.
    Runs in a daemon thread -- never blocks the reply path.
    """
    t = threading.Thread(target=_log_chat_worker, args=(phone, message_text, sender), daemon=True)
    t.start()

def reply_text(phone, text):
    send_text(phone, text)
    log_chat(phone, text, "bot")

def reply_buttons(phone, text, buttons):
    send_buttons(phone, text, buttons)
    log_chat(phone, text, "bot")

def get_services():
    """Fetches laundry services from andesdb."""
    services_ref = db_andes.collection("services").stream()
    return [{"id": s.id, "name": s.to_dict()["name"]} for s in services_ref]

# -------------------------
# ORDER & SYNC LOGIC
# -------------------------
def save_order(phone, state):
    """Saves to andesdb (Dashboard) and default (Rider App)."""
    order_num = int(time.time())
    now = firestore.SERVER_TIMESTAMP
    ts_ms = int(time.time() * 1000)

    cart_data = {
        "address": state["address"],
        "convenienceFee": 0,
        "createdAt": now,
        "deliveryCharge": 0,
        "dropTime": "standard",
        "freeDeliveryApplied": False,
        "location": {
            "address": state["address"],
            "isManual": True,
            "pincode": "000000",
            "selectionSource": "whatsapp",
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            "userEnteredAddress": state["address"]
        },
        "orderNumber": order_num,
        "orderTimestamp": ts_ms,
        "originalTotalCost": 0,
        "paymentData": {
            "convenienceFee": 0, "originalAmount": 0, "totalWithFee": 0,
            "paymentMethod": "cod", "paymentStatus": "pending", "pickupTime": state["pickup"]
        },
        "services": {f"{state['service']}_regular": 1},
        "serviceUnits": {f"{state['service']}_regular": "regular"},
        "status": "pending",
        "totalCost": 0,
        "totalItems": 1,
        "updatedAt": now,
        "userId": f"whatsapp_{phone}",
        "userMobile": f"+{phone}" if not phone.startswith("+") else phone,
        "userName": state["name"]
    }

    order_id = f"ANDES-{order_num}"
    db_andes.collection("orders").add({
        "order_id": order_id, "phone": phone, "service": state["service"],
        "address": state["address"], "pickup": state["pickup"],
        "status": "PENDING", "created_at": now
    })

    db_default.collection("cartdetails").add(cart_data)
    return order_id

def cancel_latest_order(phone):
    """Cancels latest PENDING order in both databases."""
    q1 = db_andes.collection("orders").where("phone", "==", phone).where("status", "==", "PENDING").stream()
    orders_andes = list(q1)
    cancelled = False

    if orders_andes:
        latest = sorted(orders_andes, key=lambda x: x.create_time, reverse=True)[0]
        db_andes.collection("orders").document(latest.id).update({"status": "CANCELLED"})
        cancelled = True

    q2 = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).where("status", "==", "pending").stream()
    orders_rider = list(q2)

    if orders_rider:
        latest_rider = sorted(orders_rider, key=lambda x: x.create_time, reverse=True)[0]
        db_default.collection("cartdetails").document(latest_rider.id).update({"status": "cancelled"})
        cancelled = True

    return cancelled

# -------------------------
# SMART PRICE HANDLER DATA
# -------------------------
# All item -> price mappings. Checked before AI to guarantee accurate pricing.
# Format: ([keyword list], display label, price string, emoji constant)
ITEM_PRICE_MAP = [
    # Indian Wear
    (["kurta pajama", "kurta-pajama"],              "Kurta Pajama (Dry Cleaning)",           "Rs.149/set",        E_KIMONO),
    (["kurta"],                                      "Kurta (Dry Cleaning)",                  "Rs.99/piece",       E_KIMONO),
    (["kurti"],                                      "Kurti (Dry Cleaning)",                  "Rs.99/piece",       E_KIMONO),
    (["saree embroidery", "embroidery saree"],       "Saree with Embroidery (Dry Cleaning)",  "Rs.499/piece",      E_SARI),
    (["saree", "sari"],                             "Saree (Dry Cleaning)",                  "Rs.199/piece",      E_SARI),
    (["blouse"],                                     "Blouse (Dry Cleaning)",                 "Rs.69/piece",       E_DRESS),
    (["designer lehenga"],                          "Designer Lehenga (Dry Cleaning)",        "Rs.699 - Rs.1000/piece", E_DRESS),
    (["lehenga"],                                    "Lehenga (Dry Cleaning)",                "Rs.349/piece",      E_DRESS),
    (["dhoti"],                                      "Dhoti (Dry Cleaning)",                  "Rs.69/piece",       E_SCARF),
    (["sherwani"],                                   "Sherwani (Dry Cleaning)",               "Rs.349/piece",      E_TOPHAT),
    (["pagdi", "pagri", "turban"],                  "Pagdi (Dry Cleaning)",                  "Rs.79/piece",       E_TOPHAT),
    (["salwar"],                                     "Salwar (Dry Cleaning)",                 "Rs.69/piece",       E_DRESS),
    (["sharara"],                                    "Sharara (Dry Cleaning)",                "Rs.299/piece",      E_DRESS),
    (["dupatta"],                                    "Dupatta (Dry Cleaning)",                "Rs.49/piece",       E_SCARF),
    # Winter & Outerwear
    (["leather jacket"],                             "Leather Jacket (Dry Cleaning)",         "Rs.699/piece",      E_COAT),
    (["puffer jacket", "puffer"],                   "Puffer Jacket (Dry Cleaning)",           "Rs.249/piece",      E_COAT),
    (["jacket"],                                     "Jacket (Dry Cleaning)",                 "Rs.149/piece",      E_COAT),
    (["winter coat", "coat"],                       "Winter Coat (Dry Cleaning)",             "Rs.299/piece",      E_COAT),
    (["sweater"],                                    "Sweater (Dry Cleaning)",                "Rs.149/piece",      E_YARN),
    (["hoodie"],                                     "Hoodie (Dry Cleaning)",                 "Rs.149/piece",      E_SHIRT),
    (["muffler"],                                    "Muffler (Dry Cleaning)",                "Rs.99/piece",       E_SCARF),
    (["shawl"],                                      "Shawl (Dry Cleaning)",                  "Rs.199/piece",      E_SCARF),
    (["leather gloves", "leather glove"],           "Leather Gloves (Dry Cleaning)",          "Rs.329/pair",       E_GLOVE),
    (["woolen gloves", "woollen gloves", "gloves", "glove"], "Woolen Gloves (Dry Cleaning)", "Rs.49/pair",        E_GLOVE),
    # Western & Formal
    (["suit"],                                       "Suit (Dry Cleaning)",                   "Rs.449/set",        E_SUIT),
    (["blazer"],                                     "Blazer (Dry Cleaning)",                 "Rs.249/piece",      E_SUIT),
    (["trouser", "trousers"],                       "Trouser (Dry Cleaning)",                 "Rs.49/piece",       E_PANTS),
    (["shirt pant", "shirt & pant", "shirt and pant"], "Shirt & Pant Combo (Dry Cleaning)",  "Rs.49/set",         E_FORMAL),
    (["jeans", "denim"],                            "Jeans (Dry Cleaning)",                   "Rs.59/piece",       E_PANTS),
    (["top"],                                        "Top (Dry Cleaning)",                    "Rs.49/piece",       E_TSHIRT),
    (["shirt", "tshirt", "t-shirt"],                 "Shirt",                                 "Wash & Iron (Rs.89/kg) or Combo (Rs.49/set)", E_SHIRT),
    (["pant", "pants"],                              "Pant",                                  "Trouser (Rs.49) or Combo (Rs.49/set)", E_PANTS),
    (["joggers", "jogger"],                         "Joggers (Dry Cleaning)",                 "Rs.149/piece",      E_SHOE),
    (["skirt"],                                      "Skirt (Dry Cleaning)",                  "Rs.49/piece",       E_DRESS),
    # Home Furnishings
    (["window curtain"],                             "Window Curtain (Dry Cleaning)",         "Rs.149/piece",      E_WINDOW),
    (["door curtain", "curtain"],                   "Door/Window Curtain (Dry Cleaning)",     "Rs.149 - Rs.199/piece", E_WINDOW),
    (["double bedsheet", "double bed sheet"],       "Double Bedsheet (Dry Cleaning)",         "Rs.289/piece",      E_BED),
    (["bedsheet", "bed sheet", "blanket"],          "Single Bedsheet/Blanket (Dry Cleaning)", "Rs.149/piece",      E_BED),
    (["pillow cover", "pillow"],                    "Pillow Cover (Dry Cleaning)",            "Rs.29/piece",       E_BED),
    # Shoe Cleaning
    (["loafer", "sneaker", "sneakers", "loafers"],  "Loafers / Sneakers (Shoe Cleaning)",    "Rs.249/pair",       E_SHOE),
    (["sports shoe", "sports shoes", "sports"],     "Sports Shoes (Shoe Cleaning)",           "Rs.199/pair",       E_SHOE),
    (["shoe", "shoes"],                              "Shoe Cleaning",                         "Rs.199 - Rs.249/pair", E_SHOE),
    # Standard services
    (["iron only", "only iron", "just iron", "ironing"], "Iron Only",                        "Rs.10/piece",       E_HOT),
    (["wash iron", "wash and iron", "wash & iron"], "Wash & Iron",                           "Rs.89/kg",          E_FORMAL),
    (["wash fold", "wash and fold", "wash & fold"], "Wash & Fold",                           "Rs.59/kg",          E_SHIRT),
]

PRICE_INTENT_KEYWORDS = [
    "price", "cost", "rate", "charge", "fee", "rupee", "rs", "\u20b9",
    "how much", "kitna", "amount", "pricing", "tariff", "kitne"
]

# -------------------------
# BOT CONTROLLER
# -------------------------
@app.route("/send", methods=["POST"])
def send_manual_message():
    """Protected manual send endpoint -- requires API_SECRET header."""
    api_secret = os.environ.get("API_SECRET", "")
    if api_secret and request.headers.get("X-API-Secret") != api_secret:
        print("Security: Unauthorized access attempt on /send endpoint.")
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.get_json()
    if not data or not data.get("phone") or not data.get("message"):
        return jsonify({"error": "Missing phone or message"}), 400
        
    phone = data.get("phone")
    message = data.get("message")
    image_url = data.get("imageUrl") # Extract the new optional image URL
    if image_url:
        # Send the image via WhatsApp with the message as its caption
        send_image(phone, image_url, caption=message)
        # Log the image message to the chat history
        log_chat(phone, f"[Image Attached]\n{message}", "bot")
    else:
        # Standard text message
        reply_text(phone, message)
        
    return jsonify({"status": "ok"})

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Verify webhook with Meta"""
    from config import VERIFY_TOKEN
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if token == VERIFY_TOKEN:
        return challenge
    return "Invalid token", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    # SECURITY CHECK 1: Verify request is from Meta (fast -- no network, just HMAC)
    if not verify_webhook_signature(request):
        print("Security: Rejected webhook with invalid signature.")
        return "Forbidden", 403

    data = request.get_json()

    # SPEED FIX: Acknowledge Meta immediately with 200 OK.
    # Meta requires a response within 5 seconds or it retries (causing duplicate messages).
    # We do a quick dedup check here (in-memory, instant), then hand off to a background
    # thread so Meta gets its 200 OK before any Firestore or WhatsApp API calls are made.
    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" in val:
            msg = val["messages"][0]
            msg_id = msg.get("id", "")
            if is_duplicate_message(msg_id):
                phone = msg["from"]
                print(f"Dedup: Skipping already-processed message {msg_id} for {mask_phone(phone)}")
                return "ok"  # Drop retry instantly
            # Spawn background thread -- returns 200 to Meta immediately after this
            t = threading.Thread(target=process_message, args=(data,), daemon=True)
            t.start()
    except Exception as e:
        print(f"Webhook parse error: {e}")

    return "ok"  # Meta gets this instantly -- no Firestore delays

def process_message(data):
    """Handles all bot logic in a background thread.
    Runs after 200 OK has already been returned to Meta.
    """
    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" in val:
            msg = val["messages"][0]
            phone = msg["from"]
            msg_id = msg.get("id", "")
            # Log Incoming & Verify Bot Status
            if msg["type"] == "text":
                txt_body = msg["text"]["body"]
            elif msg["type"] == "interactive":
                if "button_reply" in msg["interactive"]:
                    txt_body = f"[{msg['interactive']['button_reply']['title']}]"
                elif "list_reply" in msg["interactive"]:
                    txt_body = f"[{msg['interactive']['list_reply']['title']}]"
                else:
                    txt_body = "[Interactive]"
            else:
                txt_body = f"[{msg['type'].upper()} MESSAGE]"
                log_chat(phone, txt_body, "user")
                reply_text(phone, f"{E_WARN} Sorry, I am a bot and I can only read *text messages* right now. Please type out your request!")
                return "ok"

            log_chat(phone, txt_body, "user")

            # SECURITY CHECK 2: Per-user rate limiting
            if is_rate_limited(phone):
                # Only send the warning once when they exactly hit the limit, stay silent if they keep spamming.
                if len(_rate_limit_store[phone]) == RATE_LIMIT_MAX:
                    reply_text(phone, f"{E_WARN} You're sending messages too fast! Please wait a minute before messaging again.")
                print(f"Security: Rate limit hit for {mask_phone(phone)}")
                return "ok"

            # SECURITY CHECK 2b: Block repeat injection offenders entirely
            if is_injection_blocked(phone):
                print(f"Security: Blocked repeat offender {mask_phone(phone)}")
                return "ok"

            if is_bot_paused(phone):
                return "ok"

            # CASE A: TEXT MESSAGE (GREETINGS / INPUTS)
            if msg["type"] == "text":
                body = txt_body.lower().strip()

                # 1. SMART INTENTS (Overrides State)
                if re.search(r'\b(cancel|restart|back|reset|abort)\b', body):
                    clear_user_state(phone)
                    profile = get_user_profile(phone)
                    greeting = f"Welcome back, {profile['name']}!" if profile and profile.get("name") else "Welcome to Andes Laundry!"
                    buttons = [
                        {"id": "schedule_order", "title": "Schedule Order"},
                        {"id": "track_order", "title": "Track Order"},
                        {"id": "cancel_order", "title": "Cancel Order"},
                        {"id": "customer_support", "title": "Support"}
                    ]
                    reply_buttons(phone, f"Action cancelled. {greeting}\n\nHow can we help you today?", buttons)
                    return "ok"

                if re.search(r'\b(track|status|where)\b', body):
                    q = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).stream()
                    orders = list(q)
                    if orders:
                        latest_doc = sorted(orders, key=lambda x: x.create_time, reverse=True)[0]
                        latest = latest_doc.to_dict()
                        status_text = str(latest.get('status', 'PENDING')).upper()
                        order_num = latest.get('orderNumber', 'Unknown')
                        service_raw = list(latest.get('services', {}).keys())
                        service_display = service_raw[0].replace('_', ' ').title() if service_raw else 'N/A'
                        pickup_time = latest.get('paymentData', {}).get('pickupTime', 'N/A').replace('_', ' ').title()
                        drop_time = latest.get('dropTime', '')
                        expected_delivery = drop_time if (drop_time and drop_time != 'standard') else "Within 24-48 hours of pickup"
                        reply_text(phone,
                            f"{E_BOX} *Order Status*\n\n"
                            f"{E_ID} Order ID: *{order_num}*\n"
                            f"{E_BASKET} Service: *{service_display}*\n"
                            f"{E_CHART} Status: *{status_text}*\n"
                            f"{E_CALENDAR} Pickup Time: *{pickup_time}*\n"
                            f"{E_TRUCK} Expected Delivery: *{expected_delivery}*\n\n"
                            f"For assistance, contact Andes Support: {E_PHONE} *+91 86260 76578*"
                        )
                    else:
                        reply_text(phone, f"{E_NOMAIL} You don't have any recent orders to track.\n\nType *menu* to schedule a new pickup!")
                    return "ok"

                if re.search(r'\b(help|support|agent|human|call)\b', body):
                    db_andes.collection("support_requests").add({
                        "phone": phone,
                        "status": "OPEN",
                        "timestamp": firestore.SERVER_TIMESTAMP
                    })
                    reply_text(phone,
                        f"{E_CHAT} *Support Request Received*\n\n"
                        "Our support team has been notified and will contact you shortly.\n\n"
                        "For immediate assistance, you can also reach us at:\n"
                        f"{E_PHONE} *+91 86260 76578*\n"
                        f"{E_EMAIL} care@andes.co.in"
                    )
                    return "ok"

                if re.search(r'\b(thanks|thank you|thx|tysm|ok|okay|cool|awesome|great)\b', body):
                    reply_text(phone, f"You're welcome! {E_SMILE} Let us know if you need anything else.\n\nType *menu* anytime to schedule a pickup.")
                    return "ok"

                if re.search(r'\b(hi|hello|start|menu|hey|order|book|schedule)\b', body):
                    clear_user_state(phone)
                    profile = get_user_profile(phone)
                    greeting = f"Welcome back, {profile['name']}!" if profile and profile.get("name") else "Welcome to Andes Laundry!"

                    buttons = [
                        {"id": "schedule_order", "title": "Schedule Order"},
                        {"id": "track_order", "title": "Track Order"},
                        {"id": "cancel_order", "title": "Cancel Order"},
                        {"id": "customer_support", "title": "Support"}
                    ]
                    reply_buttons(phone, f"{greeting}\n\nHow can we help you today?", buttons)
                    return "ok"

                # 2. STATE HANDLING
                state = get_user_state(phone)

                if state and state.get("step") == "awaiting_name":
                    if len(txt_body.strip()) < 2:
                        reply_text(phone, "Please enter a valid name (at least 2 characters):")
                        return "ok"

                    state["name"] = txt_body.strip()
                    state["step"] = "awaiting_address"
                    update_user_state(phone, state)
                    reply_text(phone, f"Thanks, {txt_body.strip()}! {E_SMILE}\n\nNow please share your full *Pickup Address* (include Pune):")
                    return "ok"

                if state and state.get("step") == "awaiting_address":
                    addr = txt_body.strip()
                    if len(addr) < 10:
                        reply_text(phone, "Please enter a more detailed address (at least 10 characters):")
                        return "ok"
                    if "pune" not in addr.lower():
                        reply_text(phone, f"{E_WARN} We currently serve in Pune only. Please include 'Pune' in your address, or type *cancel* to exit.")
                        return "ok"

                    state["address"] = addr
                    state["step"] = "awaiting_service"
                    update_user_state(phone, state)

                    reply_text(phone, f"Got your address! {E_PIN}\n\nPlease type the *service* you need (e.g., Wash & Fold, Dry Cleaning, Shoe Cleaning):")
                    return "ok"

                if state and state.get("step") == "awaiting_service":
                    srv = txt_body.strip().lower()
                    
                    matched_service = None
                    if any(w in srv for w in ["premium"]): matched_service = "andes_premium"
                    elif any(w in srv for w in ["shoe", "sneaker"]): matched_service = "shoe_cleaning"
                    elif any(w in srv for w in ["dry", "clean"]): matched_service = "dry_cleaning"
                    elif "iron" in srv and "wash" not in srv: matched_service = "iron_only"
                    elif "iron" in srv and "wash" in srv: matched_service = "wash_iron"
                    elif "fold" in srv or "wash" in srv: matched_service = "wash_fold"
                    
                    if not matched_service:
                        reply_text(phone, f"{E_WARN} We couldn't recognize that service. Please type one of:\n- Wash & Fold\n- Wash & Iron\n- Iron Only\n- Dry Cleaning\n- Shoe Cleaning\n- Andes Premium")
                        return "ok"
                        
                    state["service"] = matched_service
                    state["step"] = "awaiting_pickup"
                    update_user_state(phone, state)
                    reply_text(phone, f"Got it! {E_BASKET}\n\nWhen should we come for the *pickup*? You can type anything (e.g., 'tomorrow 10am', 'Monday evening', etc.)")
                    return "ok"

                if state and state.get("step") == "awaiting_pickup":
                    state["pickup"] = txt_body.strip()
                    order_id = save_order(phone, state)
                    
                    update_user_profile(phone, {"name": state["name"], "address": state["address"]})
                    
                    send_template(phone, "order_placed", variables=[state['name'], state["pickup"]])
                    log_chat(phone, "[Template Sent: order_placed]", "bot")
                    
                    clear_user_state(phone)
                    return "ok"

                # Cancel Order -- awaiting YES confirmation
                if state and state.get("step") == "awaiting_cancel_confirm":
                    if txt_body.strip().lower() == "yes":
                        if cancel_latest_order(phone):
                            order_id = state.get("pending_order_id", "your order")
                            reply_text(phone, f"{E_CHECK} Your order *{order_id}* has been successfully cancelled.\n\nType *menu* if you need anything else.")
                        else:
                            reply_text(phone, f"{E_CROSS} Sorry, we couldn't find a pending order to cancel. It may have already been processed.")
                        clear_user_state(phone)
                    else:
                        reply_text(phone, f"Cancellation not confirmed. Your order is *still active*. {E_CHECK}\n\nType *menu* to go back to the main menu.")
                        clear_user_state(phone)
                    return "ok"

                # 3. SMART PRICE HANDLER
                # Item-specific lookup first, then full menu for general pricing queries.
                matched_item = None
                for keywords, label, price, emoji in ITEM_PRICE_MAP:
                    if any(re.search(rf'\b{re.escape(kw)}s?\b', body) for kw in keywords):
                        matched_item = (label, price, emoji)
                        break

                if matched_item:
                    label, price, emoji = matched_item
                    reply_text(phone,
                        f"{emoji} *{label}*\n"
                        f"Price: *{price}*\n\n"
                        f"{E_TRUCK} Free pickup & delivery included!\n"
                        "Type *menu* to schedule a pickup."
                    )
                    return "ok"

                elif any(kw in body.split() for kw in PRICE_INTENT_KEYWORDS) and len(body.split()) <= 3:
                    # General price inquiry (short queries like "price list" or "rates") -- show full menu
                    reply_text(phone,
                        f"{E_MONEY} *Andes Laundry -- Pricing*\n\n"
                        "*Standard Services*\n"
                        f"{E_SHIRT} Wash & Fold -- Rs.59/kg\n"
                        f"{E_FORMAL} Wash & Iron -- Rs.89/kg\n"
                        f"{E_HOT} Iron Only -- Rs.10/piece\n\n"
                        "*Dry Cleaning (select items)*\n"
                        f"{E_KIMONO} Kurta -- Rs.99 | Kurta Pajama -- Rs.149/set\n"
                        f"{E_SARI} Saree -- Rs.199 | Embroidery Saree -- Rs.499\n"
                        f"{E_DRESS} Lehenga -- Rs.349 | Designer -- Rs.699-Rs.1000\n"
                        f"{E_SUIT} Suit -- Rs.449/set | Blazer -- Rs.249\n"
                        f"{E_COAT} Jacket -- Rs.149 | Leather Jacket -- Rs.699\n"
                        f"{E_BED} Bedsheet -- Rs.149 | Curtain -- Rs.149-Rs.199\n\n"
                        "*Shoe Cleaning*\n"
                        f"{E_SHOE} Sports Shoes -- Rs.199/pair\n"
                        f"{E_SHOE} Loafers/Sneakers -- Rs.249/pair\n\n"
                        f"{E_TRUCK} Free Pickup & Delivery on all orders!\n"
                        f"{E_TIMER} Regular: 24-48 hrs | Instant: 3 hrs\n\n"
                        "Ask me the price of any specific item or type *menu* to book!"
                    )
                    return "ok"
                # --- END SMART PRICE HANDLER ---

                # 4. AI CHAT & FALLBACK
                FALLBACK_MSG = f"I didn't quite catch that! {E_ROBOT}\n\nType *menu* to see your options, or *help* to contact our human support team."
                if gemini_model:
                    try:
                        # SECURITY CHECK 3: Sanitize input (injection + length)
                        safe_input = sanitize_ai_input(txt_body)
                        if not safe_input:
                            record_injection_attempt(phone)
                            if is_injection_blocked(phone):
                                # Permanently silenced after 3 strikes
                                print(f"Security: User {mask_phone(phone)} permanently blocked for injection.")
                            else:
                                reply_text(phone, f"{E_WARN} I can't process that message. Type *menu* to continue.")
                            return "ok"

                        # Simple in-memory cache to avoid duplicate API calls
                        if not hasattr(app, '_ai_cache'):
                            app._ai_cache = {}
                        cache_key = safe_input.lower()
                        if cache_key in app._ai_cache:
                            reply_text(phone, app._ai_cache[cache_key])
                        else:
                            chat_response = gemini_model.generate_content(safe_input)
                            # SECURITY CHECK 4: Limit AI response length
                            answer = chat_response.text.strip()[:1000]
                            if len(app._ai_cache) < 50:
                                app._ai_cache[cache_key] = answer
                            reply_text(phone, answer)
                    except Exception as e:
                        print(f"Gemini API Error: {e}")
                        reply_text(phone, FALLBACK_MSG)
                else:
                    reply_text(phone, FALLBACK_MSG)

                return "ok"

            # CASE B: BUTTON CLICK (INTERACTIVE)
            elif msg["type"] == "interactive":
                if "button_reply" in msg["interactive"]:
                    bid = msg["interactive"]["button_reply"]["id"]
                elif "list_reply" in msg["interactive"]:
                    bid = msg["interactive"]["list_reply"]["id"]
                else:
                    return "ok"

                state = get_user_state(phone)

                if bid == "schedule_order":
                    profile = get_user_profile(phone)
                    if profile and profile.get("name") and profile.get("address"):
                        update_user_state(phone, {"step": "confirm_saved_address", "profile": profile})
                        buttons = [
                            {"id": "use_saved_address", "title": "Yes, use saved"},
                            {"id": "enter_new_details", "title": "No, enter new"}
                        ]
                        reply_buttons(phone, f"Welcome back, {profile['name']}! {E_WAVE}\n\nShall we pick up from your saved address?\n{E_PIN} {profile['address']}", buttons)
                    else:
                        update_user_state(phone, {"step": "awaiting_name"})
                        reply_text(phone, f"{E_BOX} *Schedule a Pickup*\n\nPlease share your *Full Name* to get started:")
                    return "ok"

                if bid == "use_saved_address":
                    if state and state.get("step") == "confirm_saved_address":
                        profile = state.get("profile")
                        state["name"] = profile["name"]
                        state["address"] = profile["address"]
                        state["step"] = "awaiting_service"
                        update_user_state(phone, state)

                        reply_text(phone, f"Perfect! {E_PARTY}\n\nPlease type the *service* you need (e.g., Wash & Fold, Dry Cleaning, Shoe Cleaning):")
                    return "ok"

                if bid == "enter_new_details":
                    if state and state.get("step") == "confirm_saved_address":
                        update_user_state(phone, {"step": "awaiting_name"})
                        reply_text(phone, f"No problem! Let's start fresh. {E_SMILE}\n\nPlease share your *Full Name*:")
                    return "ok"

                if bid == "cancel_order":
                    # Show order details first, ask for YES confirmation
                    q = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).where("status", "==", "pending").stream()
                    pending_orders = list(q)
                    if pending_orders:
                        latest_doc = sorted(pending_orders, key=lambda x: x.create_time, reverse=True)[0]
                        latest = latest_doc.to_dict()
                        order_num = latest.get('orderNumber', 'Unknown')
                        service_raw = list(latest.get('services', {}).keys())
                        service_display = service_raw[0].replace('_', ' ').title() if service_raw else 'N/A'
                        pickup_time = latest.get('paymentData', {}).get('pickupTime', 'N/A').replace('_', ' ').title()

                        update_user_state(phone, {"step": "awaiting_cancel_confirm", "pending_order_id": str(order_num)})
                        reply_text(phone,
                            f"{E_CROSS} *Cancel Order*\n\n"
                            f"{E_ID} Order ID: *{order_num}*\n"
                            f"{E_BASKET} Service: *{service_display}*\n"
                            f"{E_CALENDAR} Pickup: *{pickup_time}*\n"
                            f"{E_CHART} Status: *Pending*\n\n"
                            "To cancel this order, please reply *YES*.\n"
                            "Reply anything else to keep your order."
                        )
                    else:
                        reply_text(phone, f"{E_CROSS} You don't have any pending orders to cancel.")
                    return "ok"

                if bid == "track_order":
                    q = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).stream()
                    orders = list(q)
                    if orders:
                        latest_doc = sorted(orders, key=lambda x: x.create_time, reverse=True)[0]
                        latest = latest_doc.to_dict()
                        status_text = str(latest.get('status', 'PENDING')).upper()
                        order_num = latest.get('orderNumber', 'Unknown')
                        service_raw = list(latest.get('services', {}).keys())
                        service_display = service_raw[0].replace('_', ' ').title() if service_raw else 'N/A'
                        pickup_time = latest.get('paymentData', {}).get('pickupTime', 'N/A').replace('_', ' ').title()
                        drop_time = latest.get('dropTime', '')
                        if drop_time and drop_time != 'standard':
                            expected_delivery = drop_time
                        else:
                            expected_delivery = "Within 24-48 hours of pickup"
                        reply_text(phone,
                            f"{E_BOX} *Order Status*\n\n"
                            f"{E_ID} Order ID: *{order_num}*\n"
                            f"{E_BASKET} Service: *{service_display}*\n"
                            f"{E_CHART} Status: *{status_text}*\n"
                            f"{E_CALENDAR} Pickup Time: *{pickup_time}*\n"
                            f"{E_TRUCK} Expected Delivery: *{expected_delivery}*\n\n"
                            f"For assistance, contact Andes Support: {E_PHONE} *+91 86260 76578*"
                        )
                    else:
                        reply_text(phone, f"{E_NOMAIL} You don't have any recent orders to track.\n\nType *menu* to schedule a new pickup!")
                    return "ok"

                if bid == "customer_support":
                    db_andes.collection("support_requests").add({
                        "phone": phone,
                        "status": "OPEN",
                        "timestamp": firestore.SERVER_TIMESTAMP
                    })
                    reply_text(phone,
                        f"{E_CHAT} *Support Request Received*\n\n"
                        "Our support team has been notified and will contact you shortly.\n\n"
                        "For immediate assistance, you can also reach us at:\n"
                        f"{E_PHONE} *+91 86260 76578*\n"
                        f"{E_EMAIL} care@andes.co.in"
                    )
                    return "ok"

    except Exception as e:
        print("Bot Error:", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
