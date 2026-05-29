
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
# GEMINI AI CONNECTION & CONTEXT
# -------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    
    # HOW TO FEED CONTEXT: Edit this block to teach the AI about your business!
    business_context = """
    You are a friendly customer support AI for Andes Laundry.
    
    Here is your Knowledge Base to answer user questions:
    - Service Area: We currently only serve customers in Pune.
    - Pricing: Wash & Fold is ₹80/kg, Dry Cleaning starts at ₹150/piece, Ironing is ₹20/piece.
    - Turnaround time: Standard delivery takes 48 hours. Express delivery is 24 hours.
    - Booking: If a user wants to schedule an order, tell them to type 'menu' or 'start'.
    - Tracking: If a user asks where their clothes are, tell them to type 'track'.
    - Support: If they have a complaint, tell them to type 'support'.
    
    Rules for you:
    1. Keep answers extremely short, friendly, and use WhatsApp emojis.
    2. Never make up prices or services that are not in your Knowledge Base.
    3. Do not place the order for them, just tell them to type 'menu' to start the booking process.
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
    data = request.get_json()
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
    data = request.get_json()
    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" in val:
            msg = val["messages"][0]
            phone = msg["from"]

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
                        reply_text(phone, f"📦 *Order Status*\n\n🆔 Order ID: {order_num}\n📊 Status: *{status_text}*")
                    else:
                        reply_text(phone, "You don't have any recent orders to track.")
                    return "ok"

                if any(word in body for word in ["help", "support", "agent", "human", "call"]):
                    db_andes.collection("support_requests").add({
                        "phone": phone,
                        "status": "OPEN",
                        "timestamp": firestore.SERVER_TIMESTAMP
                    })
                    reply_text(phone, "Our support team has been notified and will contact you shortly.")
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
                    state["step"] = "awaiting_service"
                    update_user_state(phone, state)
                    
                    services = get_services()
                    buttons = [{"id": s["id"], "title": s["name"]} for s in services]
                    reply_buttons(phone, f"Thanks {txt_body.strip()}!\n\nPlease select the service you need:", buttons)
                    return "ok"

                if state and state.get("step") == "awaiting_address":
                    addr = txt_body.strip()
                    if len(addr) < 10:
                        reply_text(phone, "Please enter a more detailed address (at least 10 characters):")
                        return "ok"
                    if "pune" not in addr.lower():
                        reply_text(phone, "We currently serve in Pune only. Please include 'Pune' in your address, or type 'cancel' to exit.")
                        return "ok"
                        
                    state["address"] = addr
                    state["step"] = "awaiting_pickup"
                    update_user_state(phone, state)
                    
                    buttons = [
                        {"id": "today_evening", "title": "Today Evening"},
                        {"id": "tomorrow_morning", "title": "Tomorrow Morning"},
                        {"id": "tomorrow_evening", "title": "Tomorrow Evening"}
                    ]
                    reply_buttons(phone, "Nice! When should we come for the pickup?", buttons)
                    return "ok"
                    
                # 3. AI CHAT & FALLBACK
                FALLBACK_MSG = "I didn't quite catch that! 🤖\n\nType *menu* to see your options, or *help* to contact our human support team."
                if gemini_model:
                    try:
                        # Simple in-memory cache to avoid duplicate API calls
                        # for identical questions (saves free quota)
                        if not hasattr(app, '_ai_cache'):
                            app._ai_cache = {}
                        cache_key = txt_body.lower().strip()
                        if cache_key in app._ai_cache:
                            reply_text(phone, app._ai_cache[cache_key])
                        else:
                            chat_response = gemini_model.generate_content(txt_body)
                            answer = chat_response.text.strip()
                            # Cache up to 50 unique questions
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
                        reply_buttons(phone, f"Welcome back, {profile['name']}!\n\nDo you want us to pick up from your saved address?\n📍 {profile['address']}", buttons)
                    else:
                        update_user_state(phone, {"step": "awaiting_name"})
                        reply_text(phone, "Great! First, please enter your Full Name:")
                    return "ok"

                if bid == "use_saved_address":
                    if state and state.get("step") == "confirm_saved_address":
                        profile = state.get("profile")
                        state["name"] = profile["name"]
                        state["address"] = profile["address"]
                        state["step"] = "awaiting_service"
                        update_user_state(phone, state)
                        
                        services = get_services()
                        buttons = [{"id": s["id"], "title": s["name"]} for s in services]
                        reply_buttons(phone, "Perfect! Please select the service you need:", buttons)
                    return "ok"

                if bid == "enter_new_details":
                    if state and state.get("step") == "confirm_saved_address":
                        update_user_state(phone, {"step": "awaiting_name"})
                        reply_text(phone, "No problem. Let's start fresh.\n\nPlease enter your Full Name:")
                    return "ok"

                elif bid == "cancel_order":
                    if cancel_latest_order(phone):
                        reply_text(phone, "✅ Success! Your latest pending order has been cancelled.")
                    else:
                        reply_text(phone, "❌ Sorry, I couldn't find any pending orders for this number.")
                    return "ok"
                    
                if bid == "track_order":
                    q = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).stream()
                    orders = list(q)
                    if orders:
                        latest_doc = sorted(orders, key=lambda x: x.create_time, reverse=True)[0]
                        latest = latest_doc.to_dict()
                        status_text = str(latest.get('status', 'PENDING')).upper()
                        order_num = latest.get('orderNumber', 'Unknown')
                        reply_text(phone, f"📦 *Order Status*\n\n🆔 Order ID: {order_num}\n📊 Status: *{status_text}*")
                    else:
                        reply_text(phone, "You don't have any recent orders to track.")
                    return "ok"

                services = get_services()
                if bid in [s["id"] for s in services]:
                    if not state: state = {}
                    state["service"] = bid
                    
                    if state.get("address"):
                        state["step"] = "awaiting_pickup"
                        update_user_state(phone, state)
                        buttons = [
                            {"id": "today_evening", "title": "Today Evening"},
                            {"id": "tomorrow_morning", "title": "Tomorrow Morning"},
                            {"id": "tomorrow_evening", "title": "Tomorrow Evening"}
                        ]
                        reply_buttons(phone, "Got it. When should we come for the pickup?", buttons)
                    else:
                        state["step"] = "awaiting_address"
                        update_user_state(phone, state)
                        reply_text(phone, "Got it. Now, please enter your full pickup address:")
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
                    reply_text(phone, "Our support team has been notified and will contact you shortly.")
                    return "ok"

    except Exception as e:
        print("Bot Error:", e)
    
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

