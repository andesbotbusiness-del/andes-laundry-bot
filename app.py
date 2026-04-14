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

# 1. Dashboard Database
db_andes = firestore.client(database_id="andesdb")
# 2. Rider App Database (Default)
db_default = firestore.client() 

print("\nConnected to Both Databases (andesdb & default)\n")

user_state = {}

# -------------------------
# HELPERS
# -------------------------

def is_bot_paused(phone):
    try:
        doc = db_andes.collection("bot_settings").document(phone).get()
        return doc.to_dict().get("paused", False) if doc.exists else False
    except: return False

def log_chat(phone, message_text, sender):
    try:
        db_andes.collection("chat_history").add({
            "phone": phone, "message": message_text, "sender": sender,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except: pass

def reply_text(phone, text):
    send_text(phone, text)
    log_chat(phone, text, "bot")

def reply_buttons(phone, text, buttons):
    send_buttons(phone, text, buttons)
    log_chat(phone, text, "bot")

# -------------------------
# DUAL SYNC LOGIC
# -------------------------

def save_order(phone, state):
    # 1. Calculate Order Number
    orders_count = sum(1 for _ in db_default.collection("cartdetails").stream())
    order_num = 251000 + orders_count + 1
    now = firestore.SERVER_TIMESTAMP
    ts_ms = int(time.time() * 1000)

    # 2. Map to Rider App Schema (cartdetails)
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
    
    # 3. Save to Dashboard Database
    order_id = f"ANDES-{order_num}"
    db_andes.collection("orders").add({
        "order_id": order_id, "phone": phone, "service": state["service"],
        "address": state["address"], "pickup": state["pickup"],
        "status": "PENDING", "created_at": now
    })

    # 4. Save to Rider App Database
    db_default.collection("cartdetails").add(cart_data)
    return order_id

def cancel_latest_order(phone):
    """Finds latest pending order in both DBs and cancels it."""
    # Find in AndesDB
    orders = db_andes.collection("orders").where("phone", "==", phone).where("status", "==", "PENDING").stream()
    found = False
    for o in orders:
        db_andes.collection("orders").document(o.id).update({"status": "CANCELLED"})
        found = True
    
    # Find in Default DB (CartDetails)
    carts = db_default.collection("cartdetails").where("userMobile", "in", [phone, f"+{phone}"]).where("status", "==", "pending").stream()
    for c in carts:
        db_default.collection("cartdetails").document(c.id).update({"status": "cancelled"})
        found = True
    
    return found

# -------------------------
# WEBHOOK
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

            # Log & Check Pause
            txt = msg["text"]["body"] if msg["type"] == "text" else f"[{msg['interactive']['button_reply']['title']}]"
            log_chat(phone, txt, "user")
            if is_bot_paused(phone): return "ok"

            if msg["type"] == "text":
                body = txt.lower()
                
                # GREETING / MENU
                if body in ["hi", "hello", "start", "menu"]:
                    buttons = [
                        {"id": "schedule_order", "title": "Schedule Order"},
                        {"id": "cancel_order", "title": "Cancel Order"},
                        {"id": "customer_support", "title": "Support"}
                    ]
                    reply_buttons(phone, "Welcome to Andes Laundry\n\nHow can we help you?", buttons)

                # STEP 1: NAME COLLECTION
                elif phone in user_state and user_state[phone].get("step") == "awaiting_name":
                    user_state[phone] = {"name": txt, "step": "awaiting_service"}
                    services = [s["name"] for s in db_andes.collection("services").stream()] # optional fetch
                    reply_text(phone, f"Thanks {txt}! Now, choose a service from the menu below.")
                    # Trigger manual service button list here or next interaction

                # STEP 2: ADDRESS COLLECTION
                elif phone in user_state and user_state[phone].get("step") == "awaiting_address":
                    user_state[phone]["address"] = txt
                    user_state[phone]["step"] = "awaiting_pickup"
                    btns = [{"id": "today_evening", "title": "Today Evening"}, {"id": "tomorrow_morning", "title": "Tomorrow Morning"}]
                    reply_buttons(phone, "Select Pickup Time:", btns)

            elif msg["type"] == "interactive":
                bid = msg["interactive"]["button_reply"]["id"]
                
                if bid == "schedule_order":
                    user_state[phone] = {"step": "awaiting_name"}
                    reply_text(phone, "Please enter your Full Name:")

                elif bid == "cancel_order":
                    if cancel_latest_order(phone):
                        reply_text(phone, "✅ Your latest pending order has been cancelled.")
                    else:
                        reply_text(phone, "❌ No pending orders found to cancel.")

                elif phone in user_state and user_state[phone].get("step") == "awaiting_service":
                    # This triggered after name; we should ideally show service buttons
                    pass # Handled below by matching service IDs

                # SERVICE CLICKED
                services_ids = [s.id for s in db_andes.collection("services").stream()]
                if bid in services_ids:
                    user_state[phone]["service"] = bid
                    user_state[phone]["step"] = "awaiting_address"
                    reply_text(phone, "Please enter your pickup address:")

                # PICKUP CLICKED
                elif bid in ["today_evening", "tomorrow_morning", "tomorrow_evening"]:
                    user_state[phone]["pickup"] = bid
                    oid = save_order(phone, user_state[phone])
                    reply_text(phone, f"✅ Order Confirmed!\n\n🆔 Order ID: {oid}\n\nOur rider will be notified.")
                    del user_state[phone]

    except Exception as e: print("Error:", e)
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
