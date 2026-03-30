#!/usr/bin/env python3
"""
OpenRent Dashboard — Unified control panel.
Run: python3 dashboard/app.py
"""

import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv, set_key
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ── Paths ─────────────────────────────────────────────────────────────────────
DASHBOARD_DIR = Path(__file__).parent
BASE_DIR      = DASHBOARD_DIR.parent          # auto_caller/

# When DATA_DIR env var is set (Railway), all persistent data lives there.
# Otherwise fall back to the original local paths so nothing changes locally.
_data_override = os.environ.get("DATA_DIR")
if _data_override:
    _d = Path(_data_override)
    _d.mkdir(parents=True, exist_ok=True)
    (_d / "Referances").mkdir(exist_ok=True)
    ENV_FILE       = _d / ".env"
    CALLED_CSV     = _d / "called_references.csv"
    NOT_CONTACTED  = _d / "Referances" / "Rightmove property references - not contacted.csv"
    CALL_LOG       = _d / "call_log.txt"
    SUBMITTED_FILE = _d / "submitted_enquiries.json"
    COOKIES_FILE   = _d / "cookies.json"
    INCOMING_LOG   = _d / "incoming_log.txt"
else:
    ENV_FILE       = BASE_DIR / ".env"
    CALLED_CSV     = BASE_DIR / "called_references.csv"
    NOT_CONTACTED  = BASE_DIR / "Referances" / "Rightmove property references - not contacted.csv"
    CALL_LOG       = BASE_DIR / "call_log.txt"
    SUBMITTED_FILE = DASHBOARD_DIR / "data" / "submitted_enquiries.json"
    COOKIES_FILE   = DASHBOARD_DIR / "openrent_cookies.json"
    INCOMING_LOG   = BASE_DIR / "incoming_log.txt"
    (DASHBOARD_DIR / "data").mkdir(exist_ok=True)

NEXT_BATCH    = BASE_DIR / "next_batch.py"
SCRAPE_SCRIPT = BASE_DIR / "scrape_references.py"

# ── Seed initial data on first boot (Railway) ─────────────────────────────────
if _data_override:
    import shutil as _shutil
    _seed = BASE_DIR / "_seed"
    if _seed.exists():
        for _src in _seed.rglob("*"):
            if _src.is_file():
                _dst = Path(_data_override) / _src.relative_to(_seed)
                if not _dst.exists():
                    _dst.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(_src, _dst)

load_dotenv(ENV_FILE)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")

# ── Basic auth (set DASHBOARD_PASSWORD env var to enable) ────────────────────
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

@app.before_request
def require_auth():
    if not _DASHBOARD_PASSWORD:
        return  # no password set — open access (local dev)
    auth = request.authorization
    if not auth or auth.password != _DASHBOARD_PASSWORD:
        return Response(
            "Unauthorised", 401,
            {"WWW-Authenticate": 'Basic realm="OpenRent Dashboard"'},
        )

# ── Constants ─────────────────────────────────────────────────────────────────
ENQUIRY_JS = """() => {
    const f = Array.from(document.querySelectorAll('input[id^="OR"],textarea[id^="OR"]'));
    if (f.length < 5) return false;
    const values = [
        'Aaron',
        'Isserow',
        'aaron.isserow@gmail.com',
        'I am flexible',
        'Hey, I am a working professional and am looking for a new rental. I really love your property - is the property still available? If so, I am interested and would like to schedule a viewing at a convenient time for you.'
    ];
    const inputSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    const textareaSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    f.forEach((el, i) => {
        if (i >= values.length) return;
        const setter = el.tagName === 'TEXTAREA' ? textareaSetter : inputSetter;
        setter.call(el, values[i]);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    });
    const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Update My Enquiry');
    if (btn) { btn.click(); return true; }
    return false;
}"""

OPENRENT_SYSTEM_PROMPT = """You are helping Aaron reply to OpenRent property enquiry messages.

GOAL: Every reply has ONE purpose — get the landlord's phone number so Aaron can call, ask a couple of questions, and organise a viewing.

FORMULA: Reflect. Interest. Call.
1. Acknowledge / reflect back what the landlord said
2. Show genuine interest in the property
3. Ask for a quick call to ask a couple of questions and arrange a viewing
4. Give Aaron's number so they can reach out, or offer to call them

Standard closing line: "Feel free to drop me a number and I'll give you a ring — just a couple of quick questions and we can sort a viewing."

AARON'S NUMBER: 07 34 81 94 75 9
If a digit gets removed by OpenRent's filter, spell it out in words: zero seven three four eight one nine four seven five nine

SCENARIO RULES:

Standard first reply: Reflect their message, show interest, ask for a call.
Example: "Hi, thanks for your message! Really interested in the property — would love to have a quick chat, ask a couple of questions and arrange a viewing. Feel free to drop me a number and I'll give you a ring, or you can reach me on 07 34 81 94 75 9."

They propose or confirm a viewing time, or a viewing has been booked/arranged: Do NOT confirm a time. Always ask for a quick call first — explain warmly that you'd love to have a quick chat before visiting, just to ask a couple of questions. Keep it friendly, not suspicious.
Example: "That sounds great! Would you mind if we had a quick call before I come down? Just a couple of questions I'd love to run through first — feel free to drop me a number and I'll give you a ring, or you can reach me on 07 34 81 94 75 9."
NEVER confirm a viewing time on-platform. No exceptions. Even if a viewing is already booked or they say "confirm here" — always push for a call first.

They ask personal/financial/screening questions: Deflect to the call.
Example: "All happy to go through that — feel free to drop me a number and I'll give you a ring, much easier to chat through quickly."

They ask about company lets: Say NOT a company let, private individual. Do NOT say "I will be living there myself."
Example: "No, not a company let — I'm a private individual looking to rent. Happy to chat more if you want to drop me a number!"

They've already accepted an offer at asking price: Warm decline, leave door open.
Example: "No worries at all, hope it all goes smoothly! Feel free to get in touch if anything changes."
Only decline if they have EXPLICITLY accepted an offer at asking price.

They haven't shared a number yet or seem reluctant: Keep pushing warmly. Vary the phrasing — offer to call them instead, or re-offer Aaron's number. Do not give up after one or two attempts. Reluctance is not a flag.

They want to keep comms on-platform but haven't rejected Aaron: Work within their constraints, keep the door open, and look for a natural moment to ask again later.

ONLY flag in these two situations — no exceptions:
1. They explicitly say they do not want to let to Aaron (e.g. not suitable, found someone, rejected)
2. They explicitly refuse any off-platform contact AND refuse to engage further

In those cases reply ONLY with FLAG: followed by the landlord's name, property, and reason. Everything else — keep going.

No response from landlord: Chase once politely.
Example: "Hi, just following up — still very keen if the property is available!"

TONE RULES:
- Friendly, warm, and human — never robotic or corporate
- Short messages — don't over-explain
- No stiff sign-offs — keep it conversational
- Never reveal unnecessary personal or financial info on-platform
- Never turn down a property — always keep the door open

OUTPUT RULES:
- Write in first person as Aaron — the message is ready to send as-is
- No quotation marks around the message or anywhere in the reply
- No preamble, no explanation, no labels — just the message itself"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_twilio_client():
    load_dotenv(ENV_FILE, override=True)
    from twilio.rest import Client
    sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise ValueError("Twilio credentials not configured in .env")
    return Client(sid, token)


def load_submitted():
    if SUBMITTED_FILE.exists():
        with open(SUBMITTED_FILE) as f:
            return json.load(f)
    return {}


def save_submitted(data):
    with open(SUBMITTED_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_progress():
    called_refs = set()
    if CALLED_CSV.exists():
        with open(CALLED_CSV, newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader, None)  # skip header
            for row in reader:
                if row:
                    called_refs.add(row[0].strip())

    not_contacted = []
    if NOT_CONTACTED.exists():
        with open(NOT_CONTACTED, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            not_contacted = [row[0].strip() for row in reader if row and row[0].strip()]

    remaining = sum(1 for r in not_contacted if r not in called_refs)
    called = len(called_refs)
    return {"called": called, "total": called + remaining, "remaining": remaining}


def is_expired_sms(date_str: str) -> bool:
    """Return True if the SMS was sent more than 2 days ago."""
    if not date_str:
        return False
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    return date_str[:10] < cutoff


# INCOMING_LOG is set above in the path block
# Regex to parse a log line: [timestamp] SMS | from=X | to=Y | body=Z | media_count=N
_LOG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] SMS \| from=(.+?) \| to=(.+?) \| body=(.*) \| media_count=\d+$")


def read_log_sms() -> list:
    """Parse incoming_log.txt and return SMS entries as dicts."""
    if not INCOMING_LOG.exists():
        return []
    results = []
    with open(INCOMING_LOG) as f:
        for line in f:
            m = _LOG_RE.match(line.strip())
            if m:
                ts, from_, to_, body = m.group(1), m.group(2), m.group(3), m.group(4)
                results.append({"date": ts.replace(" ", "T"), "from": from_, "to": to_, "body": body})
    return results


def should_skip_sms(body: str) -> bool:
    if not body:
        return True
    lower = body.lower()
    return (
        "/r/" in body
        or "no longer available" in lower
        or "similar listings" in lower
    )


def extract_addinfo_urls(body: str) -> list:
    if not body:
        return []
    matches = re.findall(r"(?:https?://)?(?:www\.)?openrent\.(?:co\.)?uk/addinfo/(\d+)/(\d+)", body)
    # Keep openrent.uk domain — these are special no-login enquiry links
    return [f"https://www.openrent.uk/addinfo/{enquiry_id}/{phone}" for enquiry_id, phone in matches]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# --- Config ---

@app.route("/api/config")
def get_config():
    load_dotenv(ENV_FILE, override=True)
    return jsonify({
        "from_number": os.environ.get("TWILIO_FROM_NUMBER", ""),
        "forward_to":  os.environ.get("TWILIO_FORWARD_TO", ""),
        "has_cookies": COOKIES_FILE.exists(),
        "progress":    get_progress(),
    })


@app.route("/api/config/number", methods=["POST"])
def set_number():
    number = (request.json or {}).get("number", "").strip()
    if not number:
        return jsonify({"error": "No number provided"}), 400
    set_key(str(ENV_FILE), "TWILIO_FROM_NUMBER", number)
    set_key(str(ENV_FILE), "TWILIO_NUMBER", number)
    load_dotenv(ENV_FILE, override=True)
    return jsonify({"ok": True, "number": number})


# --- Twilio numbers ---

KNOWN_NUMBERS = ["+447445030638", "+447588741030", "+447366253862", "+447348194759",
                 "+447782563433", "+447480559308", "+447426720412", "+447427871750"]


def get_all_twilio_numbers(client):
    """Return all phone number strings to check — Twilio API list merged with known numbers."""
    seen = set()
    try:
        for n in client.incoming_phone_numbers.list():
            seen.add(n.phone_number)
    except Exception:
        pass
    for num in KNOWN_NUMBERS:
        seen.add(num)
    return seen


@app.route("/api/twilio/numbers")
def get_numbers():
    current = os.environ.get("TWILIO_FROM_NUMBER", "")
    seen = set()
    result = []

    try:
        client = get_twilio_client()
        for n in client.incoming_phone_numbers.list():
            if n.phone_number not in KNOWN_NUMBERS:
                continue
            seen.add(n.phone_number)
            result.append({
                "sid":           n.sid,
                "phone_number":  n.phone_number,
                "friendly_name": n.friendly_name or n.phone_number,
                "active":        n.phone_number == current,
            })
    except Exception:
        pass

    for num in KNOWN_NUMBERS:
        if num not in seen:
            result.append({"sid": "", "phone_number": num, "friendly_name": num, "active": num == current})

    return jsonify(result)


# --- SMS inbox ---

@app.route("/api/sms")
def get_sms():
    try:
        client    = get_twilio_client()
        numbers   = get_all_twilio_numbers(client)
        submitted = load_submitted()
        all_msgs  = []
        seen_sids: set = set()

        for num in numbers:
            msgs = client.messages.list(to=num, limit=100)
            for m in msgs:
                if m.sid in seen_sids:
                    continue
                seen_sids.add(m.sid)
                body      = m.body or ""
                skip      = should_skip_sms(body)
                urls      = extract_addinfo_urls(body)
                has_addinfo = bool(urls)
                submitted_all = all(u in submitted for u in urls) if urls else False
                date_str = m.date_sent.isoformat() if m.date_sent else ""
                all_msgs.append({
                    "sid":               m.sid,
                    "from":              m.from_,
                    "to":                m.to,
                    "body":              body,
                    "date":              date_str,
                    "has_addinfo":       has_addinfo,
                    "skip":              skip,
                    "enquiry_urls":      urls,
                    "enquiry_submitted": submitted_all,
                    "expired":           is_expired_sms(date_str),
                })

        # Also include SMS from incoming_log.txt (covers verified caller ID +447348194759)
        seen_bodies: set = set()
        for entry in read_log_sms():
            dedup_key = (entry["date"][:16], entry["from"], entry["body"][:50])
            if dedup_key in seen_bodies:
                continue
            seen_bodies.add(dedup_key)
            body        = entry["body"]
            skip        = should_skip_sms(body)
            urls        = extract_addinfo_urls(body)
            has_addinfo = bool(urls)
            submitted_all = all(u in submitted for u in urls) if urls else False
            date_str    = entry["date"]
            all_msgs.append({
                "sid":               f"log-{entry['date']}-{entry['from']}",
                "from":              entry["from"],
                "to":                entry["to"],
                "body":              body,
                "date":              date_str,
                "has_addinfo":       has_addinfo,
                "skip":              skip,
                "enquiry_urls":      urls,
                "enquiry_submitted": submitted_all,
                "expired":           is_expired_sms(date_str),
                "source":            "log",
            })

        all_msgs.sort(key=lambda x: x["date"], reverse=True)
        return jsonify(all_msgs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Enquiries ---

@app.route("/api/enquiries")
def get_enquiries():
    try:
        client    = get_twilio_client()
        numbers   = get_all_twilio_numbers(client)
        submitted = load_submitted()
        seen_urls: set = set()
        seen_sids: set = set()
        items: list   = []

        for num in numbers:
            msgs = client.messages.list(to=num, limit=100)
            for m in msgs:
                if m.sid in seen_sids:
                    continue
                seen_sids.add(m.sid)
                body = m.body or ""
                if should_skip_sms(body):
                    continue
                for url in extract_addinfo_urls(body):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    sub_info = submitted.get(url)
                    sms_date = m.date_sent.isoformat() if m.date_sent else ""
                    expired = is_expired_sms(sms_date)
                    items.append({
                        "url":          url,
                        "sms_from":     m.from_,
                        "sms_to":       m.to,
                        "sms_date":     sms_date,
                        "status":       "expired" if expired else ("submitted" if sub_info else "pending"),
                        "submitted_at": sub_info.get("submitted_at") if sub_info else None,
                        "expired":      expired,
                    })

        # Also pull enquiry URLs from incoming_log.txt
        for entry in read_log_sms():
            body = entry["body"]
            if should_skip_sms(body):
                continue
            for url in extract_addinfo_urls(body):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sub_info = submitted.get(url)
                sms_date = entry["date"]
                expired  = is_expired_sms(sms_date)
                items.append({
                    "url":          url,
                    "sms_from":     entry["from"],
                    "sms_to":       entry["to"],
                    "sms_date":     sms_date,
                    "status":       "expired" if expired else ("submitted" if sub_info else "pending"),
                    "submitted_at": sub_info.get("submitted_at") if sub_info else None,
                    "expired":      expired,
                })

        items.sort(key=lambda x: x.get("sms_date", ""), reverse=True)
        pending         = sum(1 for i in items if i["status"] == "pending")
        submitted_count = sum(1 for i in items if i["status"] == "submitted")
        expired_count   = sum(1 for i in items if i["status"] == "expired")
        return jsonify({"items": items, "pending": pending, "submitted": submitted_count, "expired": expired_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/enquiries/submit", methods=["POST"])
def submit_enquiry():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    return jsonify(_submit_url(url))


@app.route("/api/enquiries/submit-all", methods=["POST"])
def submit_all():
    urls      = (request.json or {}).get("urls", [])
    submitted = load_submitted()
    pending   = [u for u in urls if u not in submitted]
    results   = [_submit_url(u) for u in pending]
    ok        = sum(1 for r in results if r.get("success"))
    return jsonify({"results": results, "submitted": ok, "total": len(pending)})


def _submit_url(url: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        import re as _re

        # Extract property ID from addinfo URL to build messagelandlord URL
        m = _re.search(r"/addinfo/(\d+)/", url)
        property_id = m.group(1) if m else None

        cookies = []
        if COOKIES_FILE.exists():
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            # Try addinfo URL first (works without login if OpenRent still supports it)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # If redirected to login or property page, go directly to messagelandlord
            if property_id and ("logon" in page.url or "addinfo" not in page.url):
                page.goto(f"https://www.openrent.co.uk/messagelandlord/{property_id}",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

            # If still on login page, we need cookies
            if "logon" in page.url or "signin" in page.url.lower():
                browser.close()
                return {"success": False, "url": url, "error": "Not logged in — please paste your OpenRent cookies in the Replies tab"}

            # Wait for enquiry form fields
            try:
                page.wait_for_selector('input[id^="OR"], textarea[id^="OR"]', timeout=15000)
            except Exception:
                page_title = page.title()
                page_url   = page.url
                browser.close()
                return {"success": False, "url": url, "error": f"Form not found — '{page_title}' at {page_url}"}

            success = page.evaluate(ENQUIRY_JS)
            page.wait_for_timeout(3000)
            browser.close()

        if success:
            data = load_submitted()
            data[url] = {"submitted_at": datetime.now().isoformat(), "success": True}
            save_submitted(data)
        return {"success": bool(success), "url": url, "error": None if success else "Button not found or fewer than 5 fields"}
    except Exception as e:
        return {"success": False, "url": url, "error": str(e)}


# --- Calls ---

@app.route("/api/calls/run", methods=["POST"])
def run_calls():
    batch_size = (request.json or {}).get("batch_size", 20)

    def generate():
        try:
            process = subprocess.Popen(
                [sys.executable, str(NEXT_BATCH), "--batch-size", str(batch_size)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(BASE_DIR),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            for line in iter(process.stdout.readline, ""):
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            process.wait()
            yield f"data: {json.dumps({'done': True, 'returncode': process.returncode})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/scrape/run", methods=["POST"])
def run_scrape():
    def generate():
        try:
            process = subprocess.Popen(
                [sys.executable, str(SCRAPE_SCRIPT)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(BASE_DIR),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            for line in iter(process.stdout.readline, ""):
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            process.wait()
            yield f"data: {json.dumps({'done': True, 'returncode': process.returncode})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/calls/log")
def get_call_log():
    if not CALL_LOG.exists():
        return jsonify({"lines": []})
    with open(CALL_LOG) as f:
        lines = f.readlines()
    return jsonify({"lines": [ln.rstrip() for ln in lines[-150:]]})


# --- OpenRent replies ---

@app.route("/api/cookies", methods=["POST"])
def set_cookies():
    cookies = (request.json or {}).get("cookies")
    if not cookies:
        return jsonify({"error": "No cookies provided"}), 400
    try:
        parsed = json.loads(cookies) if isinstance(cookies, str) else cookies
        normalised = [{
            "name":     c["name"],
            "value":    c["value"],
            "domain":   c.get("domain", ".openrent.co.uk"),
            "path":     c.get("path", "/"),
            "secure":   c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": "Lax",
        } for c in parsed]
        with open(COOKIES_FILE, "w") as f:
            json.dump(normalised, f, indent=2)
        return jsonify({"ok": True, "count": len(normalised)})
    except Exception as e:
        return jsonify({"error": f"Invalid cookie format: {e}"}), 400


_OPENRENT_JUNK = re.compile(
    r"your enquiries.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|pre-screening answers.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|tenant viewing availability.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|verified tenant status.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|hey elan,?\s*chris from openrent.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|we have a new feature.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|learn more about verified tenant[^\n]*"
    r"|ready to move forward\?.*?(?=\d+ (?:minute|hour|day)s? ago|$)"
    r"|place holding deposit[^\n]*"
    r"|be first in the landlord.s inbox[^\n]*"
    r"|verify now[^\n]*"
    r"|available actions.*$"
    r"|rent now progress.*$"
    r"|complete referencing.*$"
    r"|sign contract.*$"
    r"|pay final balance.*$"
    r"|tenants undergoing credit check[^\n]*"
    r"|contract signing in progress[^\n]*"
    r"|awaiting full deposit[^\n]*"
    r"|cancel enquiry[^\n]*"
    r"|chase landlord[^\n]*"
    r"|report listing[^\n]*"
    r"|view listing[^\n]*"
    r"|user notes[^\n]*"
    r"|joined \d+ years? ago[^\n]*"
    r"|please wait for the landlord[^\n]*"
    r"|listing is available[^\n]*"
    r"|viewing requested[^\n]*"
    r"|unverified tenant[^\n]*"
    r"|\d+ (?:minute|hour|day)s? ago",
    re.IGNORECASE | re.DOTALL,
)

_TIMESTAMP_LINE = re.compile(
    r"^\s*(?:\d+\s+(?:minute|hour|day|second)s?\s+ago|an?\s+(?:hour|minute|day)\s+ago)\s*$",
    re.IGNORECASE,
)
_STEP_NUMBERS   = re.compile(r"^\s*[\d\s]{1,8}\s*$")   # lines like "1 2 3 4"
_BLANK_LINES    = re.compile(r"\n{3,}")

_SKIP_PHRASES = [
    "your enquiries", "pre-screening answers", "tenant viewing availability",
    "verified tenant status", "unverified tenant", "viewing requested",
    "hey elan", "chris from openrent", "we have a new feature",
    "it also gives you a little badge", "here's a link to find out more",
    "it's only £10", "applies to all your enquiries",
    "learn more about", "ready to move forward",
    "you can now place a holding deposit", "place holding deposit",
    "be first in the landlord", "verify now",
    "available actions", "rent now progress",
    "complete referencing", "sign contract", "pay final balance",
    "tenants undergoing credit check", "contract signing in progress",
    "awaiting full deposit", "cancel enquiry", "chase landlord",
    "report listing", "view listing", "user notes", "joined ",
    "please wait for the landlord", "listing is available",
    "availability:", "tenant insights", "boosted to the top of their inbox",
    "landlords typically get over", "verified tenant",
]


def _clean_thread(raw: str) -> str:
    """Strip OpenRent UI boilerplate, leaving only actual message text."""
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if _TIMESTAMP_LINE.match(s):
            continue
        if _STEP_NUMBERS.match(s):
            continue
        lower = s.lower()
        if any(lower.startswith(p) or p in lower for p in _SKIP_PHRASES):
            continue
        # Skip short landlord profile lines: "Matthew M." / "Misa T." style
        if re.match(r"^[A-Z][a-z]+ [A-Z]\.$", s):
            continue
        cleaned.append(s)

    result = "\n".join(cleaned)
    result = _BLANK_LINES.sub("\n\n", result).strip()
    return result


@app.route("/api/replies/generate", methods=["POST"])
def generate_reply():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    try:
        from playwright.sync_api import sync_playwright
        import anthropic as ant

        cookies = []
        if COOKIES_FILE.exists():
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, slow_mo=30)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            title = page.title()
            raw_text = page.evaluate("""
                () => {
                    const main = document.querySelector('main');
                    return main ? main.innerText : document.body.innerText;
                }
            """)
            browser.close()

        thread_text = _clean_thread(raw_text)

        load_dotenv(ENV_FILE, override=True)
        client = ant.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=OPENRENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": (
                f"Property: {title}\n\nFull thread:\n{thread_text}\n\n"
                "Draft a reply. If landlord clearly will not share number, reply only with FLAG: and reason."
            )}],
        )
        reply   = msg.content[0].text.strip()
        flagged = reply.startswith("FLAG:")
        return jsonify({"url": url, "title": title, "reply": reply, "flagged": flagged, "thread": thread_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies/debug-page", methods=["POST"])
def debug_page():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        from playwright.sync_api import sync_playwright
        cookies = []
        if COOKIES_FILE.exists():
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            text = page.evaluate("() => document.body.innerText")
            html_snippet = page.evaluate("() => document.body.innerHTML.substring(0, 8000)")
            all_links = page.evaluate("""
                () => [...document.querySelectorAll('a')].map(a => ({ href: a.href, text: a.innerText.trim() })).filter(a => a.href)
            """)
            browser.close()
        return jsonify({"text": text[:3000], "html": html_snippet, "links": all_links})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies/unread-threads", methods=["POST"])
def unread_threads():
    url = (request.json or {}).get("url", "https://www.openrent.co.uk/myenquiries").strip()
    try:
        from playwright.sync_api import sync_playwright
        cookies = []
        if COOKIES_FILE.exists():
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            base_url = url.split('?')[0]
            unread_urls = []
            seen = set()
            start = 0

            while True:
                page.goto(f"{base_url}?Start={start}", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)

                batch = page.evaluate("""
                    () => {
                        // Find enquiry containers that contain an "Unread messages" indicator
                        const unreadLinks = [...document.querySelectorAll('a')].filter(a =>
                            a.innerText.trim().toLowerCase() === 'unread messages' && a.href.includes('/messages/')
                        );
                        // Also check for text nodes saying "Unread messages" near a message link
                        const results = [];
                        const seen = new Set();

                        // Strategy 1: direct unread links
                        unreadLinks.forEach(a => {
                            if (!seen.has(a.href)) { seen.add(a.href); results.push(a.href); }
                        });

                        // Strategy 2: any container that has "unread" text and a /messages/ link
                        if (!results.length) {
                            document.querySelectorAll('a[href*="/messages/"]').forEach(a => {
                                if (seen.has(a.href)) return;
                                let container = a.parentElement;
                                for (let i = 0; i < 8; i++) {
                                    if (!container) break;
                                    if (container.innerText.toLowerCase().includes('unread message')) {
                                        seen.add(a.href);
                                        results.push(a.href);
                                        break;
                                    }
                                    container = container.parentElement;
                                }
                            });
                        }
                        return results;
                    }
                """)

                # Check if page had any enquiries at all (to know when to stop)
                has_enquiries = page.evaluate("""
                    () => document.querySelectorAll('a[href*="/messages/"]').length > 0
                """)

                for u in batch:
                    if u not in seen:
                        seen.add(u)
                        unread_urls.append(u)

                if not has_enquiries:
                    break
                start += 10

            browser.close()

        return jsonify({"urls": unread_urls, "count": len(unread_urls)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies/search-landlord", methods=["POST"])
def search_landlord():
    url  = (request.json or {}).get("url", "").strip()
    name = (request.json or {}).get("name", "").strip().lower()
    if not url or not name:
        return jsonify({"error": "URL and name are required"}), 400

    try:
        from playwright.sync_api import sync_playwright

        cookies = []
        if COOKIES_FILE.exists():
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            base_url = url.split('?')[0]
            all_enquiries = []
            start = 0
            while True:
                paged_url = f"{base_url}?Start={start}"
                page.goto(paged_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                batch = page.evaluate("""
                () => {
                    const results = [];
                    const SKIP = ['available', 'let agreed', 'view messages', 'unread messages', 'place holding deposit', 'manage', 'viewing requested', 'viewing arranged'];
                    // Only take one link per unique href (avoid duplicates from "Unread messages" links)
                    const seen = new Set();
                    const viewLinks = [...document.querySelectorAll('a[href*="/messages/"]')].filter(a => {
                        if (seen.has(a.href)) return false;
                        seen.add(a.href);
                        return true;
                    });
                    viewLinks.forEach(link => {
                        // Walk up until we find a container whose first line looks like a landlord name
                        let container = link.parentElement;
                        for (let i = 0; i < 12; i++) {
                            if (!container) break;
                            const lines = container.innerText.trim().split('\\n').map(l => l.trim()).filter(l => l);
                            const first = (lines[0] || '').toLowerCase();
                            if (lines.length > 3 && !SKIP.includes(first) && first.length > 1 && first.length < 40) break;
                            container = container.parentElement;
                        }
                        const text = container ? container.innerText.trim() : '';
                        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);
                        const landlord = lines[0] || '';
                        const property = lines.find(l =>
                            /flat|house|studio|bed|terrace|apartment|maisonette|room/i.test(l) && l !== landlord
                        ) || lines[1] || '';
                        results.push({ url: link.href, landlord, property });
                    });
                    return results;
                }
            """)
                if not batch:
                    break
                all_enquiries.extend(batch)
                start += 10

            enquiries = all_enquiries
            browser.close()

        # Filter by landlord name (partial, case-insensitive)
        matches = []
        seen_urls = set()
        for item in enquiries:
            if name in item.get("landlord", "").lower() or name in item.get("text", "").lower():
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    matches.append({
                        "url": item["url"],
                        "landlord": item["landlord"],
                        "property": item["property"],
                    })

        return jsonify({"results": matches})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies/send", methods=["POST"])
def send_reply():
    url     = (request.json or {}).get("url", "").strip()
    message = (request.json or {}).get("message", "").strip()
    if not url or not message:
        return jsonify({"error": "URL and message are required"}), 400

    try:
        from playwright.sync_api import sync_playwright

        cookies = []
        if COOKIES_FILE.exists():
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, slow_mo=30)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            textarea = page.locator("#message-compose-textarea")
            textarea.wait_for(timeout=10000)
            page.evaluate("(t) => { document.getElementById('message-compose-textarea').value = t; }", message)
            page.wait_for_timeout(300)
            page.evaluate("() => document.getElementById('send-message-button').removeAttribute('disabled')")
            page.locator("#send-message-button").click()
            page.wait_for_timeout(2000)
            browser.close()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Twilio webhook routes (merged from webhook_server.py) ─────────────────────

def _log_event(event_type: str, details: dict) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {event_type} | " + " | ".join(f"{k}={v}" for k, v in details.items())
    with open(INCOMING_LOG, "a") as f:
        f.write(line + "\n")


@app.route("/sms", methods=["POST"])
def twilio_sms():
    sender    = request.form.get("From", "unknown")
    to_number = request.form.get("To", "")
    body      = request.form.get("Body", "")
    _log_event("SMS", {"from": sender, "to": to_number, "body": body, "media_count": request.form.get("NumMedia", 0)})
    forward_to = os.environ.get("TWILIO_FORWARD_TO", "")
    if forward_to:
        try:
            load_dotenv(ENV_FILE, override=True)
            from twilio.rest import Client as TwilioClient
            c = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
            c.messages.create(body=f"From {sender}: {body}", from_=to_number or os.environ.get("TWILIO_FROM_NUMBER", ""), to=forward_to)
            _log_event("FWD_SMS", {"to": forward_to, "original_from": sender})
        except Exception as e:
            _log_event("FWD_SMS_ERROR", {"error": str(e)})
    from flask import Response as FlaskResponse
    return FlaskResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', mimetype="application/xml")


@app.route("/voice", methods=["POST"])
def twilio_voice():
    _log_event("CALL", {"from": request.form.get("From", ""), "to": request.form.get("To", ""), "sid": request.form.get("CallSid", ""), "status": request.form.get("CallStatus", "")})
    from flask import Response as FlaskResponse
    return FlaskResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', mimetype="application/xml")


@app.route("/status", methods=["POST"])
def twilio_status():
    _log_event("STATUS", {"sid": request.form.get("CallSid", ""), "status": request.form.get("CallStatus", ""), "duration_sec": request.form.get("CallDuration", "0")})
    from flask import Response as FlaskResponse
    return FlaskResponse("", status=204)


# --- Manual SMS (for messages received directly on +447348194759) ---

@app.route("/api/sms/manual", methods=["POST"])
def manual_sms():
    body = (request.json or {}).get("body", "").strip()
    from_  = (request.json or {}).get("from", "+447348194759").strip()
    to_    = (request.json or {}).get("to", "+447348194759").strip()
    if not body:
        return jsonify({"error": "No body provided"}), 400
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log_event("SMS", {"from": from_, "to": to_, "body": body, "media_count": 0})
    return jsonify({"ok": True, "timestamp": timestamp})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  OpenRent Dashboard")
    print("  http://localhost:8080")
    print("=" * 50)
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, port=port, threaded=True, use_reloader=True)
