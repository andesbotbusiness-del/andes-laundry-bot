from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as firebase_firestore
from google.cloud import firestore
from utils import send_text, send_buttons, send_image, send_template
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

# State management in Firestore
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
    # Create unique IDs based on timestamp
    order_num = int(time.time())
    now = firestore.SERVER_TIMESTAMP
    ts_ms = int(time.time() * 1000)

    # 1. Map to Rider App Schema (cartdetails)
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
    
    # 2. Save to Dashboard (andesdb)
    order_id = f"ANDES-{order_num}"
    db_andes.collection("orders").add({
        "order_id": order_id, "phone": phone, "service": state["service"],
        "address": state["address"], "pickup": state["pickup"],
        "status": "PENDING", "created_at": now
    })

    # 3. Save to Rider App (default)
    db_default.collection("cartdetails").add(cart_data)
    return order_id

def cancel_latest_order(phone):
    """Cancels latest PENDING order in both databases."""
    # Cancel in Dashboard
    q1 = db_andes.collection("orders").where("phone", "==", phone).where("status", "==", "PENDING").stream()
    orders_andes = list(q1)
    cancelled = False
    
    if orders_andes:
        latest = sorted(orders_andes, key=lambda x: x.create_time, reverse=True)[0]
        db_andes.collection("orders").document(latest.id).update({"status": "CANCELLED"})
        cancelled = True
    
    # Cancel in Rider App
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
                return "ok" # Silently ignore unsupported types
                
            log_chat(phone, txt_body, "user")
            
            if is_bot_paused(phone): 
                return "ok"

            # CASE A: TEXT MESSAGE (GREETINGS / INPUTS)
            if msg["type"] == "text":
                body = txt_body.lower().strip()
                
                # Main Menu & Restart
                if any(word in body for word in ["hi", "hello", "start", "menu", "hey", "cancel", "restart", "back"]):
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

                state = get_user_state(phone)

                # Step 1: Handling Name Typed
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

                # Step 2: Handling Address Typed
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

            # CASE B: BUTTON CLICK (INTERACTIVE)
            elif msg["type"] == "interactive":
                if "button_reply" in msg["interactive"]:
                    bid = msg["interactive"]["button_reply"]["id"]
                elif "list_reply" in msg["interactive"]:
                    bid = msg["interactive"]["list_reply"]["id"]
                else:
                    return "ok"
                
                state = get_user_state(phone)
                
                # Start Booking
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

                # Trigger Cancellation
                elif bid == "cancel_order":
                    if cancel_latest_order(phone):
                        reply_text(phone, "✅ Success! Your latest pending order has been cancelled.")
                    else:
                        reply_text(phone, "❌ Sorry, I couldn't find any pending orders for this number.")
                    return "ok"
                    
                # Track Order
                if bid == "track_order":
                    q = db_andes.collection("orders").where("phone", "==", phone).stream()
                    orders = list(q)
                    if orders:
                        latest_doc = sorted(orders, key=lambda x: x.create_time, reverse=True)[0]
                        latest = latest_doc.to_dict()
                        reply_text(phone, f"📦 *Order Status*\n\n🆔 Order ID: {latest.get('order_id')}\n📊 Status: *{latest.get('status', 'PENDING')}*")
                    else:
                        reply_text(phone, "You don't have any recent orders to track.")
                    return "ok"

                # Service Selection Clicked
                services = get_services()
                if bid in [s["id"] for s in services]:
                    if not state: state = {}
                    state["service"] = bid
                    
                    if state.get("address"):
                        # Returning user using saved address
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

                # Pickup Time Clicked (Final Step)
                if bid in ["today_evening", "tomorrow_morning", "tomorrow_evening"]:
                    if state and "service" in state:
                        state["pickup"] = bid
                        order_id = save_order(phone, state)
                        
                        # Save to profile for next time
                        update_user_profile(phone, {"name": state["name"], "address": state["address"]})
                        
                        # Use Meta WhatsApp Template 'order_placed'
                        pickup_str = bid.replace('_', ' ').title()
                        send_template(phone, "order_placed", variables=[state['name'], pickup_str])
                        log_chat(phone, f"[Template Sent: order_placed]", "bot")
                        
                        clear_user_state(phone)
                    return "ok"

                # Support
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
