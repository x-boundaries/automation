import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email():
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = os.environ.get('SMTP_PORT')
    smtp_user = os.environ.get('SMTP_USERNAME')
    smtp_pass = os.environ.get('SMTP_PASSWORD')
    email_from = os.environ.get('EMAIL_FROM')
    email_to = os.environ.get('EMAIL_TO') or 'weijun.seh@x-boundaries.com'

    if not all([smtp_host, smtp_port, smtp_user, smtp_pass, email_from]):
        print("SMTP secrets are not fully configured. Skipping email notification gracefully.")
        return

    today_md_path = 'dashboard/today.md'
    if not os.path.exists(today_md_path):
        print(f"Error: {today_md_path} not found. Cannot send email.")
        return

    with open(today_md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    msg = MIMEMultipart()
    msg['From'] = email_from
    msg['To'] = email_to
    msg['Subject'] = "X-Boundaries Daily Task Plan"

    # We send the Markdown content as plain text, or you could convert it to HTML.
    # The prompt says "Email should include: ... Today's Top 5 tasks ..." which matches our today.md content.
    msg.attach(MIMEText(content, 'plain'))

    try:
        port = int(smtp_port)
        if port == 465:
            server = smtplib.SMTP_SSL(smtp_host, port)
        else:
            server = smtplib.SMTP(smtp_host, port)
            server.starttls()

        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
        server.quit()
        print(f"Daily digest email successfully sent to {email_to}")
    except Exception as e:
        print(f"Failed to send email: {e}")
        # The prompt says: "either skip email gracefully or fail with a clear message explaining which secrets are missing."
        # If we reached here, credentials were provided but connection/login failed.
        import sys
        sys.exit(1)

if __name__ == '__main__':
    send_email()
