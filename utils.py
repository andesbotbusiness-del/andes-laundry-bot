import requests
import os
from config import WHATSAPP_TOKEN

# 1. Prioritize Render Environment Variables, fallback to config.py
# This ensures that even if config.py is empty, the bot uses your secure Render keys.
TOKEN = os.environ.get("WHATSAPP_TOKEN") or WHATSAPP_TOKEN

# 2. Use the updated Meta API version dynamically
def get_whatsapp_url(sender_phone_id):
    return f"https://graph.facebook.com/v21.0/{sender_phone_id}/messages"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def send_text(phone, message, sender_phone_id):
    url = get_whatsapp_url(sender_phone_id)
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": message}
    }
    
    response = requests.post(url, headers=headers, json=data, timeout=10)
    # This print statement is CRUCIAL for debugging in Render logs
    print(f"Meta Text Response: {response.status_code} - {response.text}")
    return response.json()


def send_buttons(phone, text, buttons, sender_phone_id):
    url = get_whatsapp_url(sender_phone_id)
    if len(buttons) > 3:
        rows = []
        for btn in buttons:
            rows.append({
                "id": btn["id"],
                "title": str(btn["title"])[:24]
            })
            
        data = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": text},
                "action": {
                    "button": "Select Option",
                    "sections": [
                        {
                            "title": "Options",
                            "rows": rows
                        }
                    ]
                }
            }
        }
    else:
        button_list = []
        for btn in buttons:
            button_list.append({
                "type": "reply",
                "reply": {
                    "id": btn["id"],
                    "title": str(btn["title"])[:20]
                }
            })

        data = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {
                    "buttons": button_list
                }
            }
        }

    response = requests.post(url, headers=headers, json=data, timeout=10)
    # This will show you if your button formatting is incorrect
    print(f"Meta Interactive Response: {response.status_code} - {response.text}")
    return response.json()


def send_image(phone, image_url, sender_phone_id, caption=""):
    url = get_whatsapp_url(sender_phone_id)
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": {
            "link": image_url,
            "caption": caption
        }
    }

    response = requests.post(url, headers=headers, json=data, timeout=10)
    print(f"Meta Image Response: {response.status_code} - {response.text}")
    return response.json()

def send_template(phone, template_name, sender_phone_id, variables=None, lang_code="en"):
    url = get_whatsapp_url(sender_phone_id)
    components = []
    if variables:
        parameters = [{"type": "text", "text": str(v)} for v in variables]
        components.append({
            "type": "body",
            "parameters": parameters
        })
        
    data = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": lang_code
            },
            "components": components
        }
    }
    
    response = requests.post(url, headers=headers, json=data, timeout=10)
    print(f"Meta Template Response: {response.status_code} - {response.text}")
    return response.json()

def send_marketing_template(to_number, template_name, sender_phone_id):
    url = get_whatsapp_url(sender_phone_id)
    data = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": "en"
            }
        }
    }
    
    response = requests.post(url, headers=headers, json=data, timeout=10)
    print(f"Meta Marketing Template Response: {response.status_code} - {response.text}")
    return response.json()