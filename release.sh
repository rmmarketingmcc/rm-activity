#!/bin/bash
# Publishes a new RM Activity version. Everyone updates by running the install line again.
#   ./sync.sh                                  first, to bring in the latest code from RM's repo
#   ./release.sh 1.1 "What changed"           notes: one line, or several "- " lines
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-}"; NOTES="${2:-}"
[[ -n "$VERSION" && -n "$NOTES" ]] || { echo 'Usage: ./release.sh <version> "what changed"'; exit 1; }
git rev-parse "v$VERSION" >/dev/null 2>&1 && { echo "v$VERSION already exists"; exit 1; }
/usr/bin/python3 tools/scan_clients.py
echo "$VERSION" > VERSION
./build.sh
git add -A
git commit -q -m "Release $VERSION" -m "$NOTES"
git tag "v$VERSION"
git push -q origin HEAD --tags
gh release create "v$VERSION" build/RM-Activity.zip --title "RM Activity $VERSION" --notes "$NOTES"
echo "Released $VERSION"
