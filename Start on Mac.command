#!/bin/bash
# Double-click this file to start the Quoting Engine on a Mac.
# First time only: right-click this file and choose "Open" instead of
# double-clicking — macOS blocks downloaded scripts from running until
# you approve them that way once.

cd "$(dirname "$0")/backend" || exit 1

echo "Alpine Edge Carpentry — Quoting Engine"
echo "========================================"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 isn't installed on this Mac."
    echo "Install it from https://www.python.org/downloads/ and then run this again."
    read -p "Press Enter to close..."
    exit 1
fi

echo "Installing what's needed (only takes a while the first time)..."
python3 -m pip install --quiet -r requirements.txt

echo ""
echo "Starting the server..."
echo "Your browser will open automatically in a few seconds."
echo "To stop the server later, close this window or press Ctrl+C."
echo ""

( sleep 3 && open "http://127.0.0.1:8000" ) &

python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
