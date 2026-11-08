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
/usr/bin/python3 tools/scan_clients.py
