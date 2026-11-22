import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv

load_dotenv()


def send_email(content, subject="Weekly Chatbot Summary"):
    """Send an HTML email via SendGrid. MAIL_TO may be a comma-separated list.
    Raises on failure so a scheduled job shows up as failed instead of silently succeeding."""
    recipients = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]
    message = Mail(
        from_email=os.environ["MAIL_FROM"],
        to_emails=recipients,
        subject=subject,
        html_content=content)

    try:
        sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
        response = sg.send(message)
        print(f"SendGrid status: {response.status_code}")
    except Exception as e:
        print(getattr(e, "body", e))
        raise
