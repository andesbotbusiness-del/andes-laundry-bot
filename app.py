from flask import Flask, request
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
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

db = firestore.client(database_id="andesdb")

print("\nConnected to Firebase\n")

# -------------------------
# TEMP USER STATE
# -------------------------

user_state = {}

# -------------------------
# GET SERVICES FROM FIREBASE
# -------------------------

def get_services():

    services_ref = db.collection("services").stream()

    services = []

    for service in services_ref:
        data = service.to_dict()

        services.append({
            "id": service.id,
            "name": data["name"]
        })

    return services


# -------------------------
# GENERATE ORDER ID
# -------------------------

def generate_order_id():

    orders = db.collection("orders").stream()

    count = 0

    for _ in orders:
        count += 1

    return f"ANDES-{1000 + count + 1}"


# -------------------------
# SAVE ORDER
# -------------------------

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

    print("Order Saved:", order_data)

    return order_id


# -------------------------
# SAVE COMPLAINT
# -------------------------

def save_complaint(phone, complaint):

    data = {
        "phone": phone,
        "complaint": complaint,
        "status": "OPEN",
        "created_at": firestore.SERVER_TIMESTAMP
    }

    db.collection("complaints").add(data)

    print("Complaint saved:", data)


# -------------------------
# SAVE SUPPORT REQUEST
# -------------------------

def save_support_request(phone):

    data = {
        "phone": phone,
        "status": "PENDING",
        "created_at": firestore.SERVER_TIMESTAMP
    }

    db.collection("support_requests").add(data)

    print("Support request saved:", data)


# -------------------------
# ORDER STATUS
# -------------------------

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

        service_map = {
            "wash_fold": "Wash & Fold",
            "wash_iron": "Wash & Iron",
            "dry_clean": "Dry Clean"
        }

        pickup_map = {
            "today_evening": "Today Evening",
            "tomorrow_morning": "Tomorrow Morning",
            "tomorrow_evening": "Tomorrow Evening"
        }

        service_name = service_map.get(latest_order["service"], latest_order["service"])
        pickup_time = pickup_map.get(latest_order["pickup"], latest_order["pickup"])

        return f"""📦 Your Latest Order

🆔 Order ID : {latest_order['order_id']}
🧺 Service : {service_name}
📍 Pickup : {pickup_time}
⏳ Status : {latest_order['status']}
"""

    return "❌ No orders found."

# -------------------------
# HOME
# -------------------------

@app.route("/")
def home():
    return "Laundry Bot Running"


# -------------------------
# VERIFY WEBHOOK
# -------------------------

@app.route("/webhook", methods=["GET"])
def verify():

    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if token == VERIFY_TOKEN:
        return challenge

    return "Verification failed"


# -------------------------
# RECEIVE MESSAGE
# -------------------------

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    try:

        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        phone = message["from"]

        # ---------------------
        # TEXT MESSAGE
        # ---------------------

        if message["type"] == "text":

            text = message["text"]["body"].lower()

            # GREETING
            if text in ["hi", "hello", "start"]:

                buttons = [
                    {"id": "schedule_order", "title": "Schedule Order"},
                    {"id": "raise_complaint", "title": "Raise Complaint"},
                    {"id": "customer_support", "title": "Customer Support"}
                ]

                send_buttons(
                    phone,
                    "Welcome to Andes Laundry\n\nHow can we help you today?",
                    buttons
                )

            # COMPLAINT TEXT (CHECK FIRST)
            elif phone in user_state and user_state[phone].get("mode") == "complaint":
                
                save_complaint(phone, text)
                
                send_text(
                    phone,
                    "✅ Your complaint is recorded.\n\nWe'll reach out to you soon."
                )

                del user_state[phone]

            # ADDRESS STEP
            elif phone in user_state and "address" not in user_state[phone]:
                
                user_state[phone]["address"] = text

                buttons = [
                    {"id": "today_evening", "title": "Today Evening"},
                    {"id": "tomorrow_morning", "title": "Tomorrow Morning"},
                    {"id": "tomorrow_evening", "title": "Tomorrow Evening"}
                ]

                send_buttons(
                    phone,
                    "Select Pickup Time:",
                    buttons
                )

        # ---------------------
        # BUTTON MESSAGE
        # ---------------------

        elif message["type"] == "interactive":

            button_id = message["interactive"]["button_reply"]["id"]

            # SCHEDULE ORDER
            if button_id == "schedule_order":

                buttons = [
                    {"id": "book_pickup", "title": "Book Pickup"},
                    {"id": "order_status", "title": "Order Status"},
                    {"id": "price_list", "title": "Price List"}
                ]

                send_buttons(
                    phone,
                    "Order Menu:",
                    buttons
                )

            # BOOK PICKUP
            elif button_id == "book_pickup":

                services = get_services()

                buttons = []

                for s in services:

                    buttons.append({
                        "id": s["id"],
                        "title": s["name"]
                    })

                send_buttons(
                    phone,
                    "Select Service:",
                    buttons
                )

            # SERVICE SELECT
            elif button_id in [s["id"] for s in get_services()]:

                user_state[phone] = {
                    "service": button_id
                }

                send_text(
                    phone,
                    "Please enter your pickup address:"
                )

            # PICKUP TIME
            elif button_id in ["today_evening","tomorrow_morning","tomorrow_evening"]:

                user_state[phone]["pickup"] = button_id

                order_id = save_order(phone, user_state[phone])

                send_text(
                    phone,
                    f"""✅ Order Confirmed!

🆔 Order ID: {order_id}

Our rider will arrive for pickup."""
                )

                del user_state[phone]

            # ORDER STATUS
            elif button_id == "order_status":

                status = get_order_status(phone)

                send_text(phone, status)

            # PRICE LIST
            elif button_id == "price_list":

                image_url = "https://firebasestorage.googleapis.com/v0/b/andesuser-792d4.firebasestorage.app/o/price_list.jpeg?alt=media&token=311e0a46-3a6f-4446-8a9c-c83ff8033769"

                send_image(
                    phone,
                    image_url,
                    "📋 Andes Laundry Price List"
                )

            # RAISE COMPLAINT
            elif button_id == "raise_complaint":

                user_state[phone] = {"mode": "complaint"}

                send_text(
                    phone,
                    "Write your complaint in detail.\n\nWe'll reach out to you within 2 hours."
                )

            # CUSTOMER SUPPORT
            elif button_id == "customer_support":

                save_support_request(phone)

                send_text(
                    phone,
                    "Our support team will contact you in a few minutes."
                )

    except Exception as e:
        print("Error:", e)

    return "ok"


# -------------------------
# RUN SERVER
# -------------------------

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)