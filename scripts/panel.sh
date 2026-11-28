#!/bin/bash
# Reachy control panel: start/stop any mode from a web page at http://127.0.0.1:8790
#   ./scripts/panel.sh      (leave this window open; Ctrl+C closes the panel, not Reachy)
cd "$(dirname "$0")/.."
(sleep 1.5; open http://127.0.0.1:8790) &
exec .venv/bin/python -m greeter.panel
