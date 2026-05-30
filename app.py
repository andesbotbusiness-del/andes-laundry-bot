
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as firebase_firestore
from google.cloud import firestore
from utils import send_text, send_buttons, send_image, send_template
import google.generativeai as genai # <-- Added Gemini Import
import os
import json
import time
import hmac
import hashlib
from collections import defaultdict

app = Flask(__name__)
CORS(app)

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
print("All required environment variables verified ✓")

# -------------------------
# SECURITY LAYER
# -------------------------

# 1. Per-user rate limiter (in-memory)
_rate_limit_store = defaultdict(list)
RATE_LIMIT_MAX = 10       # max messages per user
RATE_LIMIT_WINDOW = 60    # per 60 seconds

# Injection attempt tracker — block repeat offenders
_injection_strikes = defaultdict(int)
INJECTION_BLOCK_THRESHOLD = 3  # block after 3 attempts

# -------------------------
# DUPLICATE MESSAGE GUARD
# -------------------------
# Meta retries the webhook if it doesn't get a fast 200 OK.
# We deduplicate on the WhatsApp message ID to prevent the bot
# from sending the same reply 2-3 times.
_processed_msg_ids = {}          # { msg_id: timestamp }
DUPLICATE_TTL = 60               # seconds — discard cache entries older than this

def is_duplicate_message(msg_id):
    """Returns True if this message ID was already processed recently."""
    now = time.time()
    # Evict stale entries to prevent unbounded memory growth
    expired = [k for k, t in _processed_msg_ids.items() if now - t > DUPLICATE_TTL]
    for k in expired:
        del _processed_msg_ids[k]
    if msg_id in _processed_msg_ids:
        return True  # Already handled — this is a retry
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
MAX_AI_INPUT_LENGTH = 400  # chars — prevents token quota abuse
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
    
    # HOW TO FEED CONTEXT: Edit this block to teach the AI about your business!
    business_context = """
    You are a friendly, professional, and helpful customer support AI for Andes Laundry.
    
    Here is your Knowledge Base. Use this to answer user questions accurately:
    
    1. SERVICE AREA:
    - We currently serve customers across Pune (including Viman Nagar, Kothrud, Hadapsar, Hinjewadi, etc.).
    
    2. SERVICES, PRICING, & CLOTHING TYPES:
    - Wash & Fold (Starting from ₹59/Kg): Best for daily wear like t-shirts, shorts, track pants, pajamas, and undergarments.
    - Wash (Wash, Tumble-Dry, Fold) (Starting from ₹69/Kg): Great for regular casual wear.
    - Wash & Iron (Starting from ₹89/Kg): Ideal for office wear, cotton shirts, formal trousers, and kurtas.
    - Dry Cleaning (Pricing varies): Required for winter wear (jackets, sweaters, blankets, quilts) and delicate fabrics.
    - Andes Premium (Specialized Dry Cleaning): The perfect choice for expensive or designer wear including Suits, Blazers, Sherwanis, Silk Sarees, and Lehengas.
    - Shoe Cleaning (Starting from ₹125/Pair): We clean sneakers, sports shoes, canvas, and leather footwear.
    
    3. TURNAROUND TIMES:
    - Andes Regular: 24 to 48 hours guaranteed turnaround.
    - Andes Instant: 3-hour express clean and delivery for urgent needs.
    
    4. OFFERS & FEATURES:
    - We offer Free Pickup & Delivery right to the customer's doorstep.
    - Customers can track their order status, ETA, and access history by downloading the 'Andes' app on the Google Play Store.
    
    5. SUPPORT:
    - If a user needs human assistance or has a complex complaint, provide them with our support number: +91 86260 76578, or our support email: care@andes.co.in. You can also tell them to type 'support' to open a ticket.
    
    RULES FOR THE AI:
    - Keep answers concise, conversational, and friendly. Use WhatsApp appropriate emojis (👕, 👔, 👗, ✨).
    - If a user asks what service they need for a specific item (e.g., "What should I do with a silk saree?"), confidently recommend the correct service from the list above.
    - NEVER make up prices, locations, or services not listed in this knowledge base.
    - If a user wants to place an order or schedule a pickup, ALWAYS instruct them to type the word 'menu' or 'start' to trigger the automated booking system. Do not try to take their order manually.
    """
    
    # FREE PLAN OPTIMIZED — based on actual API quota dashboard
    # Only models with confirmed free quota. Ordered by RPD (highest first).
    GEMINI_MODELS = [
        "gemini-3.1-flash-lite",    # 500 RPD free — BEST option for free plan
        "gemini-2.5-flash",         # 20 RPD free — confirmed working
        "gemini-3.5-flash",         # 20 RPD free
        "gemini-2.5-flash-lite",    # 20 RPD free
        "gemini-3-flash",           # 20 RPD free
        # DO NOT USE: gemini-2.0-flash, gemini-2.0-flash-lite, gemini-2.5-pro = 0 RPD
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
def is_bot_paused(phone):
    """Checks if the human operator has paused the bot for this phone."""
    try:
        doc = db_andes.collection("bot_settings").document(phone).get()
        return doc.to_dict().get("paused", False) if doc.exists else False
    except: return False

def log_chat(phone, message_text, sender):
    """Logs conversation to andesdb for the dashboard."""
    try:
        db_andes.collection("chat_history").add({
            "phone": phone, 
            "message": message_text, 
            "sender": sender,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except: pass

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
# BOT CONTROLLER
# -------------------------
@app.route("/send", methods=["POST"])
def send_manual_message():
    """Protected manual send endpoint — requires API_SECRET header."""
    api_secret = os.environ.get("API_SECRET", "")
    if api_secret and request.headers.get("X-API-Secret") != api_secret:
        print("Security: Unauthorized access attempt on /send endpoint.")
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or not data.get("phone") or not data.get("message"):
        return jsonify({"error": "Missing phone or message"}), 400
    reply_text(data.get("phone"), data.get("message"))
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
    # SECURITY CHECK 1: Verify request is from Meta
    if not verify_webhook_signature(request):
        print("Security: Rejected webhook with invalid signature.")
        return "Forbidden", 403

    data = request.get_json()
    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" in val:
            msg = val["messages"][0]
            phone = msg["from"]
            msg_id = msg.get("id", "")

            # DEDUPLICATION GUARD: Meta retries webhooks on slow/failed responses.
            # If we've already processed this exact message ID, drop the retry silently.
            if is_duplicate_message(msg_id):
                print(f"Dedup: Skipping already-processed message {msg_id} for {mask_phone(phone)}")
                return "ok"

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
                return "ok" 
                
            log_chat(phone, txt_body, "user")
            
            # SECURITY CHECK 2: Per-user rate limiting
            if is_rate_limited(phone):
                print(f"Security: Rate limit hit for {mask_phone(phone)}")
                return "ok"  # Silently ignore — don't tell attacker they're blocked

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
                if any(word in body for word in ["cancel", "restart", "back", "reset", "abort"]):
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

                if any(word in body for word in ["track", "status", "where"]):
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
                        expected_delivery = drop_time if (drop_time and drop_time != 'standard') else "Within 24–48 hours of pickup"
                        reply_text(phone,
                            f"📦 *Order Status*\n\n"
                            f"🆔 Order ID: *{order_num}*\n"
                            f"🧺 Service: *{service_display}*\n"
                            f"📊 Status: *{status_text}*\n"
                            f"📅 Pickup Time: *{pickup_time}*\n"
                            f"🚚 Expected Delivery: *{expected_delivery}*\n\n"
                            f"For assistance, contact Andes Support: 📞 *+91 86260 76578*"
                        )
                    else:
                        reply_text(phone, "📭 You don't have any recent orders to track.\n\nType *menu* to schedule a new pickup!")
                    return "ok"

                if any(word in body for word in ["help", "support", "agent", "human", "call"]):
                    db_andes.collection("support_requests").add({
                        "phone": phone,
                        "status": "OPEN",
                        "timestamp": firestore.SERVER_TIMESTAMP
                    })
                    reply_text(phone,
                        "💬 *Support Request Received*\n\n"
                        "Our support team has been notified and will contact you shortly.\n\n"
                        "For immediate assistance, you can also reach us at:\n"
                        "📞 *+91 86260 76578*\n"
                        "📧 care@andes.co.in"
                    )
                    return "ok"

                if any(word in body for word in ["hi", "hello", "start", "menu", "hey"]):
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
                    state["step"] = "awaiting_address"   # NEW ORDER: ask address next
                    update_user_state(phone, state)
                    reply_text(phone, f"Thanks, {txt_body.strip()}! 😊\n\nNow please share your full *Pickup Address* (include Pune):")
                    return "ok"

                if state and state.get("step") == "awaiting_address":
                    addr = txt_body.strip()
                    if len(addr) < 10:
                        reply_text(phone, "Please enter a more detailed address (at least 10 characters):")
                        return "ok"
                    if "pune" not in addr.lower():
                        reply_text(phone, "⚠️ We currently serve in Pune only. Please include 'Pune' in your address, or type *cancel* to exit.")
                        return "ok"
                        
                    state["address"] = addr
                    state["step"] = "awaiting_service"   # NEW ORDER: service comes after address
                    update_user_state(phone, state)
                    
                    services = get_services()
                    buttons = [{"id": s["id"], "title": s["name"]} for s in services]
                    reply_buttons(phone, "Got your address! 📍\n\nPlease select the *service* you need:", buttons)
                    return "ok"

                # Cancel Order — awaiting YES confirmation
                if state and state.get("step") == "awaiting_cancel_confirm":
                    if txt_body.strip().lower() == "yes":
                        if cancel_latest_order(phone):
                            order_id = state.get("pending_order_id", "your order")
                            reply_text(phone, f"✅ Your order *{order_id}* has been successfully cancelled.\n\nType *menu* if you need anything else.")
                        else:
                            reply_text(phone, "❌ Sorry, we couldn't find a pending order to cancel. It may have already been processed.")
                        clear_user_state(phone)
                    else:
                        reply_text(phone, "Cancellation not confirmed. Your order is *still active*. ✅\n\nType *menu* to go back to the main menu.")
                        clear_user_state(phone)
                    return "ok"
                    
                # 3. AI CHAT & FALLBACK
                FALLBACK_MSG = "I didn't quite catch that! 🤖\n\nType *menu* to see your options, or *help* to contact our human support team."
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
                                reply_text(phone, "⚠️ I can't process that message. Type *menu* to continue.")
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
                        reply_buttons(phone, f"Welcome back, {profile['name']}! 👋\n\nShall we pick up from your saved address?\n📍 {profile['address']}", buttons)
                    else:
                        update_user_state(phone, {"step": "awaiting_name"})
                        reply_text(phone, "📦 *Schedule a Pickup*\n\nPlease share your *Full Name* to get started:")
                    return "ok"

                if bid == "use_saved_address":
                    if state and state.get("step") == "confirm_saved_address":
                        profile = state.get("profile")
                        state["name"] = profile["name"]
                        state["address"] = profile["address"]
                        state["step"] = "awaiting_service"   # address already known, go straight to service
                        update_user_state(phone, state)
                        
                        services = get_services()
                        buttons = [{"id": s["id"], "title": s["name"]} for s in services]
                        reply_buttons(phone, "Perfect! 🎉\n\nPlease select the *service* you need:", buttons)
                    return "ok"

                if bid == "enter_new_details":
                    if state and state.get("step") == "confirm_saved_address":
                        update_user_state(phone, {"step": "awaiting_name"})
                        reply_text(phone, "No problem! Let's start fresh. 😊\n\nPlease share your *Full Name*:")
                    return "ok"

                if bid == "cancel_order":
                    # NEW FLOW: Show order details first, ask for YES confirmation
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
                            f"❌ *Cancel Order*\n\n"
                            f"🆔 Order ID: *{order_num}*\n"
                            f"🧺 Service: *{service_display}*\n"
                            f"📅 Pickup: *{pickup_time}*\n"
                            f"📊 Status: *Pending*\n\n"
                            f"To cancel this order, please reply *YES*.\n"
                            f"Reply anything else to keep your order."
                        )
                    else:
                        reply_text(phone, "❌ You don't have any pending orders to cancel.")
                    return "ok"
                    
                if bid == "track_order":
                    q = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).stream()
                    orders = list(q)
                    if orders:
                        latest_doc = sorted(orders, key=lambda x: x.create_time, reverse=True)[0]
                        latest = latest_doc.to_dict()
                        status_text = str(latest.get('status', 'PENDING')).upper()
                        order_num = latest.get('orderNumber', 'Unknown')
                        # Service name
                        service_raw = list(latest.get('services', {}).keys())
                        service_display = service_raw[0].replace('_', ' ').title() if service_raw else 'N/A'
                        # Pickup time
                        pickup_time = latest.get('paymentData', {}).get('pickupTime', 'N/A').replace('_', ' ').title()
                        # Expected delivery (drop_time field or estimated from status)
                        drop_time = latest.get('dropTime', '')
                        if drop_time and drop_time != 'standard':
                            expected_delivery = drop_time
                        else:
                            expected_delivery = "Within 24–48 hours of pickup"
                        reply_text(phone,
                            f"📦 *Order Status*\n\n"
                            f"🆔 Order ID: *{order_num}*\n"
                            f"🧺 Service: *{service_display}*\n"
                            f"📊 Status: *{status_text}*\n"
                            f"📅 Pickup Time: *{pickup_time}*\n"
                            f"🚚 Expected Delivery: *{expected_delivery}*\n\n"
                            f"For assistance, contact Andes Support: 📞 *+91 86260 76578*"
                        )
                    else:
                        reply_text(phone, "📭 You don't have any recent orders to track.\n\nType *menu* to schedule a new pickup!")
                    return "ok"

                services = get_services()
                if bid in [s["id"] for s in services]:
                    if not state: state = {}
                    state["service"] = bid
                    # Address is always collected before service in the new flow,
                    # so it should already be in state. Go straight to pickup time.
                    state["step"] = "awaiting_pickup"
                    update_user_state(phone, state)
                    buttons = [
                        {"id": "today_evening", "title": "Today Evening"},
                        {"id": "tomorrow_morning", "title": "Tomorrow Morning"},
                        {"id": "tomorrow_evening", "title": "Tomorrow Evening"}
                    ]
                    reply_buttons(phone, "Got it! 🧺\n\nWhen should we come for the *pickup*?", buttons)
                    return "ok"

                if bid in ["today_evening", "tomorrow_morning", "tomorrow_evening"]:
                    if state and "service" in state:
                        state["pickup"] = bid
                        order_id = save_order(phone, state)
                        
                        update_user_profile(phone, {"name": state["name"], "address": state["address"]})
                        
                        pickup_str = bid.replace('_', ' ').title()
                        send_template(phone, "order_placed", variables=[state['name'], pickup_str])
                        log_chat(phone, f"[Template Sent: order_placed]", "bot")
                        
                        clear_user_state(phone)
                    return "ok"

                if bid == "customer_support":
                    db_andes.collection("support_requests").add({
                        "phone": phone,
                        "status": "OPEN",
                        "timestamp": firestore.SERVER_TIMESTAMP
                    })
                    reply_text(phone,
                        "💬 *Support Request Received*\n\n"
                        "Our support team has been notified and will contact you shortly.\n\n"
                        "For immediate assistance, you can also reach us at:\n"
                        "📞 *+91 86260 76578*\n"
                        "📧 care@andes.co.in"
                    )
                    return "ok"

    except Exception as e:
        print("Bot Error:", e)
    
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

