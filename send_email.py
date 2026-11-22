"""Send the HTML report via Azure Communication Services (ACS) Email.

Env:
  ACS_CONNECTION_STRING  Communication Services resource connection string
  MAIL_FROM              sender address on a domain linked to that resource,
                         e.g. DoNotReply@<guid>.azurecomm.net (Azure-managed domain)
  MAIL_TO                recipients, comma-separated

Raises on failure so a scheduled job shows up as failed instead of silently succeeding.
"""
import os

from azure.communication.email import EmailClient
from dotenv import load_dotenv

load_dotenv()

SEND_TIMEOUT_S = 120


def send_email(html, subject="Chatbot Summary", text=None):
    to = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]
    if not to:
        raise ValueError("MAIL_TO is empty")

    message = {
        "senderAddress": os.environ["MAIL_FROM"],
        "recipients": {"to": [{"address": a} for a in to]},
        "content": {
            "subject": subject,
            "plainText": text or "This report is best viewed in an HTML-capable email client.",
            "html": html,
        },
    }
    client = EmailClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])
    result = client.begin_send(message).result(timeout=SEND_TIMEOUT_S)
    if result.get("status") != "Succeeded":
        raise RuntimeError(f"ACS email failed: status={result.get('status')} error={result.get('error')}")
    print(f"ACS email sent to {len(to)} recipient(s), id={result.get('id')}")
