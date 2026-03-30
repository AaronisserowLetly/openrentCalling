#!/usr/bin/env python3
"""
Twilio Automated CSV Caller

Reads reference numbers from a CSV file and places automated calls via Twilio,
entering each reference number via DTMF tones following a fixed call flow.

Usage:
    python caller.py path/to/references.csv
"""

import csv
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(override=True)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")

# Destination number to call — change this to your target number
DESTINATION_NUMBER = "+442036418510"

# Max concurrent calls (set to 1 for sequential mode)
MAX_CONCURRENT_CALLS = 25

# Seconds to stagger between launching calls within a batch (avoids rate-limiting)
STAGGER_DELAY = 2

# How often (seconds) to poll Twilio for call status updates
POLL_INTERVAL = 3

# Terminal call statuses that mean the call is finished
TERMINAL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}

# Log file path — use DATA_DIR if set (Railway), otherwise next to this script
LOG_FILE = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent))) / "call_log.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Thread-safe locks
_log_lock = threading.Lock()
_csv_lock = threading.Lock()


def log(message: str) -> None:
    """Print to console and append to the log file with a timestamp (thread-safe)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    with _log_lock:
        print(line, flush=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


def make_twiml_pause(seconds: int) -> str:
    """TwiML that just keeps the call alive with a long pause."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Pause length="{seconds}"/></Response>'
    )


def make_twiml_digits(digits: str, label: str = "", hold: int = 120) -> str:
    """TwiML that announces the step, plays DTMF digits, then holds the line."""
    say_part = ""
    if label:
        say_part = f'<Say voice="alice">{label}</Say>'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response>{say_part}<Play digits="ww{digits}ww"/>'
        f'<Pause length="{hold}"/></Response>'
    )


def make_twiml_hangup() -> str:
    """TwiML that hangs up."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def wait_for_status(client: Client, call_sid: str, target: set, timeout: int = 30) -> str:
    """Poll call status until it matches one of the target statuses or times out."""
    elapsed = 0
    while elapsed < timeout:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        call = client.calls(call_sid).fetch()
        if call.status in target:
            return call.status
    return client.calls(call_sid).fetch().status


def place_call(client: Client, reference: str) -> tuple[str, str]:
    """
    Place a call and drive the IVR step-by-step using mid-call TwiML updates.

    Call flow:
      1. Wait 30s (destination answers, IVR plays greeting)
      2. Enter reference number
      3. Press # (submit reference)
      4. Wait 17s
      5. Press 1
      6. Wait 15s
      7. Press #
      8. Hang up

    Returns (call_sid, final_status).
    """
    ref_digits = "w".join(reference.strip())

    # Place the call with a long hold to keep the line alive
    call = client.calls.create(
        twiml=make_twiml_pause(180),
        to=DESTINATION_NUMBER,
        from_=TWILIO_FROM_NUMBER,
        record=True,
    )
    call_sid = call.sid
    log(f"  Call initiated — SID: {call_sid}")

    # Wait for the call to be answered
    status = wait_for_status(client, call_sid, {"in-progress"} | TERMINAL_STATUSES, timeout=45)
    if status in TERMINAL_STATUSES:
        log(f"  Call ended before answer — status: {status}")
        return call_sid, status

    answered_at = datetime.now().strftime("%H:%M:%S")

    # Step 1: Wait 30s for IVR greeting
    log(f"  [1] Answered at {answered_at} — waiting 30s")
    time.sleep(30)

    # Step 2+3: Enter reference number then press #
    log(f"  [2+3] {datetime.now().strftime('%H:%M:%S')} — Entering reference {reference} then #")
    client.calls(call_sid).update(twiml=make_twiml_digits(ref_digits + "ww#"))

    # Step 4: Wait 18s
    log(f"  [4] {datetime.now().strftime('%H:%M:%S')} — Waiting 18s")
    time.sleep(18)

    # Check call is still alive
    call_status = client.calls(call_sid).fetch().status
    if call_status in TERMINAL_STATUSES:
        log(f"  Call ended after reference — status: {call_status}")
        return call_sid, call_status

    # Step 5: Press 1
    log(f"  [5] {datetime.now().strftime('%H:%M:%S')} — Pressing 1")
    client.calls(call_sid).update(twiml=make_twiml_digits("1"))

    # Step 6: Wait 15s
    log(f"  [6] {datetime.now().strftime('%H:%M:%S')} — Waiting 15s")
    time.sleep(15)

    # Check call is still alive
    call_status = client.calls(call_sid).fetch().status
    if call_status in TERMINAL_STATUSES:
        log(f"  Call ended after pressing 1 — status: {call_status}")
        return call_sid, call_status

    # Step 7: Press #
    log(f"  [7] {datetime.now().strftime('%H:%M:%S')} — Pressing #")
    client.calls(call_sid).update(twiml=make_twiml_digits("#"))

    # Step 8: Wait for IVR to finish then hang up
    log(f"  [8] {datetime.now().strftime('%H:%M:%S')} — Waiting for IVR to end call...")
    wait_for_status(client, call_sid, TERMINAL_STATUSES, timeout=30)
    try:
        client.calls(call_sid).update(twiml=make_twiml_hangup())
    except TwilioRestException:
        pass
    log(f"  [DONE] {datetime.now().strftime('%H:%M:%S')} — Call ended")

    # Fetch final status & recording
    call = client.calls(call_sid).fetch()
    final_status = call.status

    time.sleep(3)
    recordings = client.calls(call_sid).recordings.list()
    if recordings:
        rec_url = f"https://api.twilio.com{recordings[0].uri.replace('.json', '.mp3')}"
        log(f"  Recording: {rec_url}")
    else:
        log(f"  No recording available")

    return call_sid, final_status


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    """
    Read the CSV file. Returns (fieldnames, rows).

    If a 'status' column is missing it will be added to the fieldnames.
    """
    with open(path, newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    # Ensure a status column exists
    if "status" not in fieldnames:
        fieldnames.append("status")
        for row in rows:
            row["status"] = ""

    return fieldnames, rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write all rows back to the CSV (in-place update)."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_single_call(client: Client, reference: str, idx: int, row: dict,
                         fieldnames: list[str], rows: list[dict], csv_path: Path) -> str:
    """
    Process a single call in a thread. Returns 'ok', 'failed', or 'skipped'.
    Updates the row status and persists to CSV (thread-safe).
    """
    try:
        call_sid, final_status = place_call(client, reference)
        log(f"Row {idx}: [{reference}] SID={call_sid} status={final_status}")

        if final_status == "completed":
            row["status"] = "done"
            result = "ok"
        else:
            row["status"] = f"failed ({final_status})"
            result = "failed"

    except TwilioRestException as exc:
        log(f"Row {idx}: Twilio error — {exc}")
        row["status"] = f"failed (twilio: {exc.code})"
        result = "failed"

    except Exception as exc:
        log(f"Row {idx}: unexpected error — {exc}")
        row["status"] = f"failed ({exc})"
        result = "failed"

    # Persist progress (thread-safe)
    with _csv_lock:
        write_csv(csv_path, fieldnames, rows)

    return result


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python caller.py <path/to/references.csv>")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.is_file():
        print(f"Error: file not found — {csv_path}")
        sys.exit(1)

    # Validate env vars
    missing = []
    if not TWILIO_ACCOUNT_SID:
        missing.append("TWILIO_ACCOUNT_SID")
    if not TWILIO_AUTH_TOKEN:
        missing.append("TWILIO_AUTH_TOKEN")
    if not TWILIO_FROM_NUMBER:
        missing.append("TWILIO_FROM_NUMBER")
    if missing:
        print(f"Error: missing environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    fieldnames, rows = read_csv(csv_path)

    # Determine the reference column name (first column that isn't 'status')
    ref_col = next((c for c in fieldnames if c != "status"), None)
    if ref_col is None:
        print("Error: CSV has no reference number column.")
        sys.exit(1)

    total = len(rows)
    successful = 0
    failed = 0
    skipped = 0

    # Build work list (skip already-done rows)
    work_items = []
    for idx, row in enumerate(rows, start=1):
        reference = row.get(ref_col, "").strip()
        status = row.get("status", "").strip().lower()

        if status == "done":
            log(f"Row {idx}: [{reference}] — skipped (already done)")
            skipped += 1
            continue

        if not reference:
            log(f"Row {idx}: empty reference — skipping")
            skipped += 1
            continue

        work_items.append((idx, row, reference))

    pending = len(work_items)
    log(f"Starting run — {total} rows in {csv_path.name} | {skipped} already done | {pending} to call")
    log(f"Reference column: '{ref_col}' | Destination: {DESTINATION_NUMBER}")
    log(f"Concurrency: {MAX_CONCURRENT_CALLS} parallel calls")

    if not work_items:
        log("Nothing to do — all rows already processed.")
        return

    # Run calls in parallel batches
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS) as executor:
        futures = {}
        for i, (idx, row, reference) in enumerate(work_items):
            log(f"Row {idx}: queuing call for reference [{reference}]")
            future = executor.submit(
                process_single_call, client, reference, idx, row,
                fieldnames, rows, csv_path,
            )
            futures[future] = (idx, reference)
            # Stagger launches to avoid Twilio rate-limiting
            if i < len(work_items) - 1:
                time.sleep(STAGGER_DELAY)

        # Collect results as they complete
        for future in as_completed(futures):
            idx, reference = futures[future]
            try:
                result = future.result()
                if result == "ok":
                    successful += 1
                elif result == "failed":
                    failed += 1
            except Exception as exc:
                log(f"Row {idx}: [{reference}] thread error — {exc}")
                failed += 1

    # Summary
    log("=" * 50)
    log(f"Run complete — Total: {total} | OK: {successful} | Failed: {failed} | Skipped: {skipped}")
    log("=" * 50)


if __name__ == "__main__":
    main()
