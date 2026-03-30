#!/bin/bash
# OpenRent Dashboard — start script
# Usage: ./run_dashboard.sh

cd "$(dirname "$0")"

echo "Checking dependencies..."

# Install Python packages if needed
pip3 install -q -r requirements.txt

# Install Playwright browsers if not already installed
python3 -c "from playwright.sync_api import sync_playwright; sync_playwright().__enter__()" 2>/dev/null || {
  echo "Installing Playwright browsers..."
  python3 -m playwright install chromium
}

echo ""
echo "Starting OpenRent Dashboard at http://localhost:8080"
echo "Press Ctrl+C to stop."
echo ""

python3 dashboard/app.py
