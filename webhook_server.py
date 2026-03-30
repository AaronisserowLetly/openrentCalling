#!/usr/bin/env python3
"""
Twilio Webhook Server

Receives and logs incoming calls and SMS to the Twilio number +447445030638.
Exposes /voice, /sms, and /logs endpoints.

Usage:
    python3 webhook_server.py

Then expose with ngrok:
    ngrok http 8080

Set the ngrok HTTPS URL in Twilio Console → Phone Numbers → +447445030638:
    Voice: https://<ngrok-url>/voice
    Messaging: https://<ngrok-url>/sms
"""

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, Response
from twilio.rest import Client

load_dotenv()

app = Flask(__name__)

LOG_FILE = Path(__file__).parent / "incoming_log.txt"

TWILIO_NUMBER = os.environ.get("TWILIO_NUMBER", "+447445030638")
TWILIO_FORWARD_TO = os.environ.get("TWILIO_FORWARD_TO", "")

twilio_client = Client(
    os.environ.get("TWILIO_ACCOUNT_SID"),
    os.environ.get("TWILIO_AUTH_TOKEN"),
)


def log_event(event_type: str, details: dict) -> None:
    """Log an incoming event to console and file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {event_type} | " + " | ".join(
        f"{k}={v}" for k, v in details.items()
    )
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Incoming SMS
# ---------------------------------------------------------------------------

@app.route("/sms", methods=["POST"])
def incoming_sms():
    """Handle incoming SMS messages."""
    sender = request.form.get("From", "unknown")
    to_number = request.form.get("To", TWILIO_NUMBER)
    body = request.form.get("Body", "")
    num_media = int(request.form.get("NumMedia", 0))

    log_event("SMS", {
        "from": sender,
        "to": to_number,
        "body": body,
        "media_count": num_media,
    })

    if TWILIO_FORWARD_TO:
        try:
            twilio_client.messages.create(
                body=f"From {sender}: {body}",
                from_=to_number,
                to=TWILIO_FORWARD_TO,
            )
            log_event("FWD_SMS", {"to": TWILIO_FORWARD_TO, "original_from": sender})
        except Exception as e:
            log_event("FWD_SMS_ERROR", {"error": str(e)})

    twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    return Response(twiml, mimetype="application/xml")


# ---------------------------------------------------------------------------
# Incoming Voice Call
# ---------------------------------------------------------------------------

@app.route("/voice", methods=["POST"])
def incoming_voice():
    """Handle incoming voice calls."""
    caller = request.form.get("From", "unknown")
    call_sid = request.form.get("CallSid", "")
    call_status = request.form.get("CallStatus", "")

    log_event("CALL", {
        "from": caller,
        "to": request.form.get("To", TWILIO_NUMBER),
        "sid": call_sid,
        "status": call_status,
    })

    # Forward call to verified number
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "</Response>"
    )
    return Response(twiml, mimetype="application/xml")


# ---------------------------------------------------------------------------
# Call Status Callback (optional — set as status callback URL in Twilio)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["POST"])
def call_status():
    """Log call status updates from Twilio."""
    call_sid = request.form.get("CallSid", "")
    status = request.form.get("CallStatus", "")
    duration = request.form.get("CallDuration", "0")

    log_event("STATUS", {
        "sid": call_sid,
        "status": status,
        "duration_sec": duration,
    })

    return Response("", status=204)


# ---------------------------------------------------------------------------
# Log Viewer
# ---------------------------------------------------------------------------

@app.route("/logs", methods=["GET"])
def view_logs():
    """Simple browser-viewable log of all incoming activity."""
    if LOG_FILE.exists():
        entries = LOG_FILE.read_text().strip().split("\n")
        # Show newest first
        entries.reverse()
    else:
        entries = ["No activity yet."]

    rows = "\n".join(f"<tr><td><pre>{e}</pre></td></tr>" for e in entries)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Incoming Log — {TWILIO_NUMBER}</title>
    <meta http-equiv="refresh" content="10">
    <style>
        body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
        h1 {{ color: #0f9; }}
        table {{ border-collapse: collapse; width: 100%; }}
        td {{ padding: 6px 10px; border-bottom: 1px solid #333; }}
        pre {{ margin: 0; white-space: pre-wrap; }}
    </style>
</head>
<body>
    <h1>Incoming Activity — {TWILIO_NUMBER}</h1>
    <p>Auto-refreshes every 10 seconds.</p>
    <table>{rows}</table>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Webhook server for {TWILIO_NUMBER}")
    print(f"Logging to: {LOG_FILE}")
    print()
    print("Endpoints:")
    print("  POST /sms    — Twilio SMS webhook")
    print("  POST /voice  — Twilio Voice webhook")
    print("  POST /status — Twilio call status callback")
    print("  GET  /logs   — View activity in browser")
    print()
    print("Next steps:")
    print("  1. Run: ngrok http 8080")
    print("  2. Copy the https://... URL")
    print(f"  3. In Twilio Console → Phone Numbers → {TWILIO_NUMBER}:")
    print("     Voice webhook:     https://<ngrok>/voice")
    print("     Messaging webhook: https://<ngrok>/sms")
    print()

    app.run(host="0.0.0.0", port=8080, debug=True)
