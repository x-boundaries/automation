import os
import sys
import re
import smtplib
import html as html_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def render_html_digest(markdown_text: str) -> str:
    lines = markdown_text.splitlines()

    html = [
        "<html>",
        "<head>",
        "<meta charset='utf-8'>",
        "<style>",
        "body { font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif; background-color: #f6f8fa; color: #24292f; margin: 0; padding: 20px; line-height: 1.5; }",
        ".container { max-width: 720px; margin: 0 auto; }",
        ".header { margin-bottom: 20px; }",
        ".title { font-size: 24px; font-weight: bold; margin-bottom: 5px; }",
        ".date { color: #57606a; font-size: 14px; margin-bottom: 15px; }",
        ".reminder { background-color: #fff8c5; border: 1px solid #f0b429; border-radius: 6px; padding: 10px 15px; margin-bottom: 20px; font-size: 14px; }",
        ".buttons { margin-bottom: 20px; display: flex; gap: 10px; flex-wrap: wrap; }",
        ".btn { display: inline-block; background-color: #0969da; color: #ffffff; text-decoration: none; padding: 5px 12px; border-radius: 6px; font-size: 14px; font-weight: 500; margin-right: 10px; margin-bottom: 5px; }",
        ".section { margin-bottom: 20px; }",
        ".section-title { font-size: 18px; font-weight: bold; margin-bottom: 10px; border-bottom: 1px solid #d0d7de; padding-bottom: 5px; }",
        ".card { background-color: #ffffff; border: 1px solid #d0d7de; border-radius: 6px; padding: 15px; margin-bottom: 10px; }",
        ".task-title { font-weight: bold; margin-bottom: 5px; }",
        ".task-meta { color: #57606a; font-size: 12px; margin-bottom: 10px; }",
        ".task-detail { font-size: 14px; margin-bottom: 5px; }",
        ".task-detail strong { color: #24292f; }",
        ".compact-list { background-color: #ffffff; border: 1px solid #d0d7de; border-radius: 6px; padding: 15px; }",
        ".compact-list ul { margin: 0; padding-left: 20px; font-size: 14px; }",
        ".compact-list li { margin-bottom: 5px; }",
        ".compact-list p { margin: 0; font-size: 14px; font-style: italic; color: #57606a; }",
        ".footer { margin-top: 30px; font-size: 12px; color: #57606a; text-align: center; border-top: 1px solid #d0d7de; padding-top: 10px; }",
        "a { color: #0969da; text-decoration: none; }",
        "a:hover { text-decoration: underline; }",
        "</style>",
        "</head>",
        "<body>",
        "<div class='container'>"
    ]

    in_section = None
    list_items = []

    def flush_list():
        if list_items:
            html.append("<div class='compact-list'><ul>")
            for item in list_items:
                html.append(f"<li>{item}</li>")
            html.append("</ul></div>")
            list_items.clear()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Title
        if line.startswith("# Daily Action Plan"):
            html.append("<div class='header'>")
            html.append("<div class='title'>X-Boundaries Daily Action Plan</div>")
            i += 1
            continue

        # Date
        if line.startswith("**Date:**"):
            date_str = line.replace("**Date:**", "").strip()
            date_str = html_lib.escape(date_str)
            html.append(f"<div class='date'>Date: {date_str}</div>")
            i += 1
            continue

        # Reminder
        if line.startswith(">"):
            reminder_text = line.replace("> ", "").replace("⚠️", "").strip()
            reminder_text = html_lib.escape(reminder_text)
            # simple bold replacement after escaping
            reminder_text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', reminder_text)
            html.append(f"<div class='reminder'>⚠️ {reminder_text}</div>")
            i += 1
            continue

        # Top level links (Buttons)
        if in_section is None and line.startswith("- ["):
            # collect buttons
            html.append("<div class='buttons'>")
            while i < len(lines) and lines[i].strip().startswith("- ["):
                m = re.match(r'- \[(.*?)\]\((.*?)\)', lines[i].strip())
                if m:
                    btn_text = html_lib.escape(m.group(1))
                    btn_link = html_lib.escape(m.group(2))
                    html.append(f"<a href='{btn_link}' class='btn'>{btn_text}</a>")
                i += 1
            html.append("</div>")
            html.append("</div>") # close header
            continue

        # Sections
        if line.startswith("## "):
            flush_list()
            if in_section:
                html.append("</div>") # close previous section
            in_section = line[3:].strip()
            in_section_escaped = html_lib.escape(in_section)
            html.append(f"<div class='section'><div class='section-title'>{in_section_escaped}</div>")
            i += 1
            continue

        # Parse Task Card in Top 5 Tasks
        if in_section and "Top 5" in in_section and re.match(r'^\d+\.', line):
            # parse card
            m = re.match(r'^\d+\.\s+\*\*(.*?)\*\*\s+\((.*?)\)', line)
            if m:
                task_title = html_lib.escape(m.group(1))
                task_meta = html_lib.escape(m.group(2))
                html.append("<div class='card'>")
                html.append(f"<div class='task-title'>{task_title}</div>")

                # replace commas with pipes if meta is like "Priority: High, Effort: 2"
                task_meta_clean = task_meta.replace(", ", " | ")
                html.append(f"<div class='task-meta'>{task_meta_clean}</div>")

                # read sub-bullets
                i += 1
                while i < len(lines) and (lines[i].startswith("   -") or lines[i].strip() == ""):
                    subline = lines[i].strip()
                    if subline.startswith("-"):
                        # remove bullet
                        subtext = subline[1:].strip()
                        subtext = html_lib.escape(subtext)
                        # parse **Key:** Value
                        subtext = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', subtext)
                        html.append(f"<div class='task-detail'>{subtext}</div>")
                    i += 1
                html.append("</div>")
                continue

        # "No items" text
        if line.startswith("*") and line.endswith("*"):
            flush_list()
            escaped_line = html_lib.escape(line.strip('*'))
            html.append(f"<div class='compact-list'><p>{escaped_line}</p></div>")
            i += 1
            continue

        # Normal lists in other sections
        if in_section and line.startswith("-"):
            text = line[1:].strip()
            text = html_lib.escape(text)
            text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
            text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', text)
            list_items.append(text)
            i += 1
            continue

        i += 1

    flush_list()
    if in_section:
        html.append("</div>")

    html.append("<div class='footer'>Generated from x-boundaries/automation.</div>")
    html.append("</div></body></html>")

    return "\n".join(html)

def send_email():
    is_preview = '--preview' in sys.argv
    today_md_path = 'dashboard/today.md'

    if not os.path.exists(today_md_path):
        print(f"Error: {today_md_path} not found. Cannot send email.")
        sys.exit(0) # Also fail gracefully if missing md since workflow should succeed

    with open(today_md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    html_content = render_html_digest(content)

    if is_preview:
        preview_path = 'dashboard/email_preview.html'
        with open(preview_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"Preview generated at {preview_path}")
        sys.exit(0)

    smtp_host = os.environ.get('SMTP_HOST') or ''
    smtp_port = os.environ.get('SMTP_PORT') or ''
    smtp_user = os.environ.get('SMTP_USERNAME') or ''
    smtp_pass = os.environ.get('SMTP_PASSWORD') or ''
    email_from = os.environ.get('EMAIL_FROM') or ''
    email_to = os.environ.get('EMAIL_TO') or 'weijun.seh@x-boundaries.com'

    if not all([smtp_host, smtp_port, smtp_user, smtp_pass, email_from]):
        print("SMTP secrets are not fully configured. Skipping email notification gracefully.")
        sys.exit(0)

    # Use alternative to support both text/plain and text/html
    msg = MIMEMultipart("alternative")
    msg['From'] = email_from
    msg['To'] = email_to

    # Extract date for subject line if possible
    subject_date = "Today"
    for line in content.splitlines():
        if line.startswith("**Date:**"):
            subject_date = line.replace("**Date:**", "").strip()
            break

    msg['Subject'] = f"X-Boundaries Daily Task Plan - {subject_date}"

    # Attach parts, email clients prefer text/plain first, then text/html
    msg.attach(MIMEText(content, 'plain'))
    msg.attach(MIMEText(html_content, 'html'))

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
        # If we reached here, credentials were provided but connection/login failed.
        sys.exit(1)

if __name__ == '__main__':
    send_email()
