from flask import Flask, request, jsonify  # type: ignore
import firebase_admin  # type: ignore
from firebase_admin import credentials, firestore  # type: ignore
from utils import send_text, send_buttons, send_image
from config import VERIFY_TOKEN
import os
import json

app = Flask(__name__)

# -------------------------
# FIREBASE CONNECTION
# -------------------------

firebase_key = json.loads(os.environ["FIREBASE_KEY"])
cred = credentials.Certificate(firebase_key)

firebase_admin.initialize_app(cred)

# Connect to the specific 'andesdb' instance
db = firestore.client(database_id="andesdb")

print("\nConnected to Firebase (andesdb)\n")

# -------------------------
# TEMP USER STATE
# -------------------------

user_state = {}

# -------------------------
# NEW: BOT CONTROL HELPER
# -------------------------

def is_bot_paused(phone):
    """Checks if the human operator has paused the bot for this specific phone."""
    try:
        doc = db.collection("bot_settings").document(phone).get()
        if doc.exists:
            return doc.to_dict().get("paused", False)
        return False
    except Exception as e:
        print(f"Error checking bot status for {phone}: {e}")
        return False

# -------------------------
# CHAT LOGGING TO FIREBASE
# -------------------------

def log_chat(phone, message_text, sender):
    """Saves a single message to the chat_history collection for the dashboard."""
    try:
        doc_data = {
            "phone": phone,
            "message": message_text,
            "sender": sender, # "user" or "bot"
            "timestamp": firestore.SERVER_TIMESTAMP
        }
        db.collection("chat_history").add(doc_data)
    except Exception as e:
        print(f"Failed to log chat to Firebase: {e}")

# --- WRAPPER FUNCTIONS ---

def reply_text(phone, text):
    send_text(phone, text)
    log_chat(phone, text, "bot")

def reply_buttons(phone, text, buttons):
    send_buttons(phone, text, buttons)
    log_chat(phone, text, "bot")

def reply_image(phone, image_url, caption=""):
    send_image(phone, image_url, caption)
    log_chat(phone, f"[Sent Image] {caption}", "bot")


# -------------------------
# BUSINESS LOGIC
# -------------------------

def get_services():
    services_ref = db.collection("services").stream()
    services = []
    for service in services_ref:
        data = service.to_dict()
        services.append({"id": service.id, "name": data["name"]})
    return services

def generate_order_id():
    orders = db.collection("orders").stream()
    count = sum(1 for _ in orders)
    return f"ANDES-{1000 + count + 1}"

def save_order(phone, order):
    order_id = generate_order_id()
    order_data = {
        "order_id": order_id,
        "phone": phone,
        "service": order["service"],
        "address": order["address"],
        "pickup": order["pickup"],
        "status": "PENDING",
        "created_at": firestore.SERVER_TIMESTAMP
    }
    db.collection("orders").add(order_data)
    return order_id

def save_complaint(phone, complaint):
    db.collection("complaints").add({
        "phone": phone,
        "complaint": complaint,
        "status": "OPEN",
        "created_at": firestore.SERVER_TIMESTAMP
    })

def save_support_request(phone):
    db.collection("support_requests").add({
        "phone": phone,
        "status": "PENDING",
        "created_at": firestore.SERVER_TIMESTAMP
    })

def get_order_status(phone):
    orders_ref = db.collection("orders").where("phone", "==", phone).stream()
    latest_order = None
    highest_id = 0

    for order in orders_ref:
        data = order.to_dict()
        order_id = data.get("order_id", "")
        if order_id.startswith("ANDES-"):
            number = int(order_id.split("-")[1])
            if number > highest_id:
                highest_id = number
                latest_order = data

    if latest_order:
        service_map = {"wash_fold": "Wash & Fold", "wash_iron": "Wash & Iron", "dry_clean": "Dry Clean"}
        pickup_map = {"today_evening": "Today Evening", "tomorrow_morning": "Tomorrow Morning", "tomorrow_evening": "Tomorrow Evening"}
        service_name = service_map.get(latest_order["service"], latest_order["service"])
        pickup_time = pickup_map.get(latest_order["pickup"], latest_order["pickup"])
        return f"📦 Your Latest Order\n\n🆔 Order ID : {latest_order['order_id']}\n🧺 Service : {service_name}\n📍 Pickup : {pickup_time}\n⏳ Status : {latest_order['status']}"
    return "❌ No orders found."

# -------------------------
# ENDPOINTS
# -------------------------

@app.route("/")
def home():
    return "Andes Laundry Bot is running"

@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        return challenge
    return "Verification failed"

# NEW: MANUAL SEND ENDPOINT (Called by Dashboard)
@app.route("/send", methods=["POST"])
def send_manual_message():
    data = request.get_json()
    phone = data.get("phone")
    message = data.get("message")
    
    if not phone or not message:
        return jsonify({"status": "error", "message": "Missing phone or message"}), 400
        
    print(f"Admin sending manual message to {phone}: {message}")
    reply_text(phone, message) # This sends and logs it to Firebase
    return jsonify({"status": "ok"})

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    try:
        if "entry" in data and "changes" in data["entry"][0] and "value" in data["entry"][0]["changes"][0]:
            value = data["entry"][0]["changes"][0]["value"]

            if "messages" in value:
                message = value["messages"][0]
                phone = message["from"]

                # 1. LOG INCOMING MESSAGE FIRST (Always visible in dashboard)
                if message["type"] == "text":
                    text_body = message["text"]["body"]
                    log_chat(phone, text_body, "user")
                elif message["type"] == "interactive":
                    text_body = f"[{message['interactive']['button_reply']['title']}]"
                    log_chat(phone, text_body, "user")

                # 2. CHECK IF BOT IS PAUSED FOR THIS USER
                if is_bot_paused(phone):
                    print(f"Bot is paused for {phone}. Human is in control.")
                    return "ok"

                # 3. AUTOMATED BOT LOGIC
                if message["type"] == "text":
                    text = text_body.lower()

                    if text in ["hi", "hello", "start"]:
                        buttons = [
                            {"id": "schedule_order", "title": "Schedule Order"},
                            {"id": "raise_complaint", "title": "Raise Complaint"},
                            {"id": "customer_support", "title": "Customer Support"}
                        ]
                        reply_buttons(phone, "Welcome to Andes Laundry\n\nHow can we help you today?", buttons)

                    elif phone in user_state and user_state[phone].get("mode") == "complaint":
                        save_complaint(phone, text)
                        reply_text(phone, "✅ Your complaint is recorded.\n\nWe'll reach out to you soon.")
                        del user_state[phone]

                    elif phone in user_state and "address" not in user_state[phone]:
                        user_state[phone]["address"] = text
                        buttons = [
                            {"id": "today_evening", "title": "Today Evening"},
                            {"id": "tomorrow_morning", "title": "Tomorrow Morning"},
                            {"id": "tomorrow_evening", "title": "Tomorrow Evening"}
                        ]
                        reply_buttons(phone, "Select Pickup Time:", buttons)

                elif message["type"] == "interactive":
                    button_id = message["interactive"]["button_reply"]["id"]
                    
                    if button_id == "schedule_order":
                        buttons = [{"id": "book_pickup", "title": "Book Pickup"}, {"id": "order_status", "title": "Order Status"}, {"id": "price_list", "title": "Price List"}]
                        reply_buttons(phone, "Order Menu:", buttons)

                    elif button_id == "book_pickup":
                        services = get_services()
                        buttons = [{"id": s["id"], "title": s["name"]} for s in services]
                        reply_buttons(phone, "Select Service:", buttons)

                    elif button_id in [s["id"] for s in get_services()]:
                        user_state[phone] = {"service": button_id}
                        reply_text(phone, "Please enter your pickup address:")

                    elif button_id in ["today_evening","tomorrow_morning","tomorrow_evening"]:
                        user_state[phone]["pickup"] = button_id
                        order_id = save_order(phone, user_state[phone])
                        reply_text(phone, f"✅ Order Confirmed!\n\n🆔 Order ID: {order_id}\n\nOur rider will arrive for pickup.")
                        del user_state[phone]

                    elif button_id == "order_status":
                        reply_text(phone, get_order_status(phone))

                    elif button_id == "price_list":
                        url = "https://firebasestorage.googleapis.com/v0/b/andesuser-792d4.firebasestorage.app/o/price_list.jpeg?alt=media&token=311e0a46-3a6f-4446-8a9c-c83ff8033769"
                        reply_image(phone, url, "📋 Andes Laundry Price List")

                    elif button_id == "raise_complaint":
                        user_state[phone] = {"mode": "complaint"}
                        reply_text(phone, "Write your complaint in detail.\n\nWe'll reach out to you within 2 hours.")

                    elif button_id == "customer_support":
                        save_support_request(phone)
                        reply_text(phone, "Our support team will contact you in a few minutes.")

    except Exception as e:
        print("Webhook Error:", e)

    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
