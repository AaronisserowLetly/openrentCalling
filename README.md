# Twilio Automated CSV Caller

Reads reference numbers from a CSV file and places automated phone calls via Twilio. Each call dials a fixed destination number and enters the reference number using DTMF keypad tones.

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in your Twilio credentials:

```bash
cp .env.example .env
```

3. Edit `caller.py` and set `DESTINATION_NUMBER` to the phone number you want to call.

## CSV Format

Single-column CSV with reference numbers. Example (`refs.csv`):

```
reference
12345678
87654321
99001122
```

A `status` column is added automatically on the first run to track progress.

## Usage

```bash
python caller.py path/to/refs.csv
```

### Resume capability

Rows marked `done` are skipped on subsequent runs. If the script is interrupted, re-run the same command to pick up where it left off.

## Call Flow

For each reference number the script:

1. Dials the destination number
2. Waits 30 seconds after answer
3. Enters the reference digits (0.5 s gap between each)
4. Waits 12 seconds
5. Presses 1
6. Waits 12 seconds
7. Presses #
8. Hangs up

## Configuration

Constants at the top of `caller.py`:

| Variable              | Description                          | Default            |
|-----------------------|--------------------------------------|--------------------|
| `DESTINATION_NUMBER`  | Phone number to call                 | `+27XXXXXXXXX`     |
| `DELAY_BETWEEN_CALLS` | Seconds between consecutive calls    | `5`                |
| `POLL_INTERVAL`       | Seconds between call-status polls    | `3`                |

## Logs

- Console output shows real-time progress.
- `call_log.txt` is written in the project directory with timestamped entries.
