import os

VERIFY_TOKEN = "verify_token_123"
PHONE_NUMBER_ID = "1109427478922656"

# This pulls the token safely from Render's secure vault later!
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")