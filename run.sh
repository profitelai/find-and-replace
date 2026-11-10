#!/bin/bash
# Start Profitell Find & Replace and open it in your browser.
cd "$(dirname "$0")" && exec python3 server.py "$@"
