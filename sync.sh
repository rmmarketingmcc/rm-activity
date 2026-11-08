#!/bin/bash
# Copies the shared code from RM's own repo (one codebase: improvements made for RM reach everyone),
# then checks that no client name, address or domain came along.
#   ./sync.sh
set -euo pipefail
cd "$(dirname "$0")"
SRC="${RM_REPO:-$HOME/google-ads-mcp}/scripts"
cp "$SRC/activity_tracker.py" tracker/activity_tracker.py
cp "$SRC/activity_widget.swift" app/ActivityApp.swift
cp "$SRC/activity_widget.html" app/widget.html
cp app/widget.html docs/widget.html
cp "$SRC/activity_icon.png" assets/icon-1024.png        # the app icon (drawn by scripts/activity_icon.py)
cp "$SRC/activity_icon_small.png" assets/icon-small.png  # bolder version for 16-32 px
sips -z 256 256 assets/icon-1024.png --out docs/icon.png >/dev/null
/usr/bin/python3 tools/scan_clients.py
