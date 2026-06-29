import os

VERIFY_TOKEN = "verify_token_123"
SUPPORT_PHONE_ID = os.environ.get("SUPPORT_PHONE_ID", "1109427478922656")
MARKETING_PHONE_ID = os.environ.get("MARKETING_PHONE_ID", "1125023584038567")

# This pulls the token safely from Render's secure vault later!
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")