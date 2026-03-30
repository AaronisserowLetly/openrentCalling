#!/usr/bin/env python3
"""
next_batch.py — Run the next batch of OpenRent calls from the terminal.

Usage:
    python3 next_batch.py [--batch-size 20]

What it does:
    1. Reads called_references.csv to find the last reference called
    2. Finds that reference in the not-contacted list and takes the next N rows
    3. Writes them to references.csv
    4. Runs caller.py
    5. Appends the batch to called_references.csv with today's date
"""

import csv
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

# Paths — data files live in DATA_DIR (Railway volume) or next to this script locally
_CODE_DIR      = Path(__file__).parent
BASE           = Path(os.environ.get("DATA_DIR", str(_CODE_DIR)))
CALLED_CSV     = BASE / "called_references.csv"
NOT_CONTACTED  = BASE / "Referances" / "Rightmove property references - not contacted.csv"
REFERENCES_CSV = BASE / "references.csv"
CALLER_SCRIPT  = _CODE_DIR / "caller.py"

DEFAULT_BATCH_SIZE = 20


def read_column(path: Path, delimiter=";") -> list[str]:
    """Read the first non-status column from a CSV, skipping the header."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        col = next((c for c in reader.fieldnames if c.strip().lower() != "status"), None)
        if col is None:
            return []
        return [row[col].strip() for row in reader if row[col].strip()]


def main():
    batch_size = DEFAULT_BATCH_SIZE
    if "--batch-size" in sys.argv:
        idx = sys.argv.index("--batch-size")
        batch_size = int(sys.argv[idx + 1])

    # 1. Load called references (semicolon-delimited)
    called = set(read_column(CALLED_CSV, delimiter=";"))
    print(f"References called so far: {len(called)}")

    # 2. Load the full not-contacted list (comma-delimited, single column)
    with open(NOT_CONTACTED, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        all_refs = [row[0].strip() for row in reader if row and row[0].strip()]

    print(f"Total references in source list: {len(all_refs)}")

    # 3. Find the first reference not yet called, in order
    batch = []
    for ref in all_refs:
        if ref not in called:
            batch.append(ref)
            if len(batch) >= batch_size:
                break

    if not batch:
        print("All references have been called!")
        sys.exit(0)

    # Show what we're about to call
    start_ref = batch[0]
    end_ref = batch[-1]
    start_idx = all_refs.index(start_ref) + 1  # 1-based row number
    print(f"\nNext batch: {len(batch)} references (rows {start_idx}–{start_idx + len(batch) - 1})")
    print(f"  First: {start_ref}")
    print(f"  Last:  {end_ref}")
    print()

    # 4. Write references.csv (semicolon-delimited, with empty status column)
    with open(REFERENCES_CSV, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Reference Number", "status"])
        for ref in batch:
            writer.writerow([ref, ""])

    print(f"Written {len(batch)} references to {REFERENCES_CSV.name}")

    # 5. Run the caller
    print("\n--- Starting calls ---\n")
    result = subprocess.run(
        [sys.executable, str(CALLER_SCRIPT), str(REFERENCES_CSV)],
        cwd=str(BASE),
    )

    if result.returncode != 0:
        print(f"\nCaller exited with code {result.returncode}. Not appending to called log.")
        sys.exit(result.returncode)

    # 6. Append batch to called_references.csv
    today = date.today().isoformat()
    with open(CALLED_CSV, "a", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        for ref in batch:
            writer.writerow([ref, today])

    print(f"\nLogged {len(batch)} references to {CALLED_CSV.name} with date {today}")
    print(f"Total called: {len(called) + len(batch)}")


if __name__ == "__main__":
    main()
