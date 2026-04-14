from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
from utils import send_text, send_buttons, send_image
from config import VERIFY_TOKEN
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
db_andes = firestore.client(database_id="andesdb")
# 2. Rider App Database (Default)
db_default = firestore.client() 

print("\nConnected to Both Databases (andesdb & default)\n")

# In-memory user state
user_state = {}

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
    # Create unique IDs
    orders_count = sum(1 for _ in db_default.collection("cartdetails").stream())
    order_num = 251000 + orders_count + 1
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
    cancelled = False
    for o in q1:
        db_andes.collection("orders").document(o.id).update({"status": "CANCELLED"})
        cancelled = True
    
    # Cancel in Rider App
    q2 = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).where("status", "==", "pending").stream()
    for c in q2:
        db_default.collection("cartdetails").document(c.id).update({"status": "cancelled"})
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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" in val:
            msg = val["messages"][0]
            phone = msg["from"]

            # Log Incoming & Verify Bot Status
            txt_body = msg["text"]["body"] if msg["type"] == "text" else f"[{msg['interactive']['button_reply']['title']}]"
            log_chat(phone, txt_body, "user")
            
            if is_bot_paused(phone): 
                return "ok"

            # CASE A: TEXT MESSAGE (GREETINGS / INPUTS)
            if msg["type"] == "text":
                body = txt_body.lower().strip()
                
                # Main Menu
                if any(word in body for word in ["hi", "hello", "start", "menu", "hey"]):
                    buttons = [
                        {"id": "schedule_order", "title": "Schedule Order"},
                        {"id": "cancel_order", "title": "Cancel Order"},
                        {"id": "customer_support", "title": "Support"}
                    ]
                    reply_buttons(phone, "Welcome to Andes Laundry\n\nHow can we help you today?", buttons)
                    return "ok"

                # Step 1: Handling Name Typed
                if phone in user_state and user_state[phone].get("step") == "awaiting_name":
                    user_state[phone]["name"] = txt_body
                    user_state[phone]["step"] = "awaiting_service"
                    
                    services = get_services()
                    buttons = [{"id": s["id"], "title": s["name"]} for s in services]
                    reply_buttons(phone, f"Thanks {txt_body}!\n\nPlease select the service you need:", buttons)
                    return "ok"

                # Step 2: Handling Address Typed
                if phone in user_state and user_state[phone].get("step") == "awaiting_address":
                    user_state[phone]["address"] = txt_body
                    user_state[phone]["step"] = "awaiting_pickup"
                    
                    buttons = [
                        {"id": "today_evening", "title": "Today Evening"},
                        {"id": "tomorrow_morning", "title": "Tomorrow Morning"},
                        {"id": "tomorrow_evening", "title": "Tomorrow Evening"}
                    ]
                    reply_buttons(phone, "Nice! When should we come for the pickup?", buttons)
                    return "ok"

            # CASE B: BUTTON CLICK (INTERACTIVE)
            elif msg["type"] == "interactive":
                bid = msg["interactive"]["button_reply"]["id"]
                
                # Start Booking
                if bid == "schedule_order":
                    user_state[phone] = {"step": "awaiting_name"}
                    reply_text(phone, "Great! First, please enter your Full Name:")
                    return "ok"

                # Trigger Cancellation
                elif bid == "cancel_order":
                    if cancel_latest_order(phone):
                        reply_text(phone, "✅ Success! Your latest pending order has been cancelled.")
                    else:
                        reply_text(phone, "❌ Sorry, I couldn't find any pending orders for this number.")
                    return "ok"

                # Service Selection Clicked
                services = get_services()
                if bid in [s["id"] for s in services]:
                    if phone not in user_state: user_state[phone] = {}
                    user_state[phone]["service"] = bid
                    user_state[phone]["step"] = "awaiting_address"
                    reply_text(phone, "Got it. Now, please enter your full pickup address:")
                    return "ok"

                # Pickup Time Clicked (Final Step)
                if bid in ["today_evening", "tomorrow_morning", "tomorrow_evening"]:
                    if phone in user_state and "service" in user_state[phone]:
                        user_state[phone]["pickup"] = bid
                        order_id = save_order(phone, user_state[phone])
                        reply_text(phone, f"✅ Order Confirmed!\n\n🆔 Order ID: {order_id}\n\nOur rider will contact you soon. Thank you!")
                        del user_state[phone]
                    return "ok"

                # Support
                if bid == "customer_support":
                    reply_text(phone, "Our support team has been notified and will contact you shortly.")
                    return "ok"

    except Exception as e:
        print("Bot Error:", e)
    
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
