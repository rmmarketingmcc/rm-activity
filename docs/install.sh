#!/bin/bash
# Installs (or updates) RM Activity. Run in Terminal:
#   curl -fsSL https://rmmarketingmcc.github.io/rm-activity/install.sh | bash
# Remove it again:
#   curl -fsSL https://rmmarketingmcc.github.io/rm-activity/install.sh | bash -s -- --uninstall
set -euo pipefail

# release.sh pins these: this script only installs the exact zip it was released with.
VERSION="1.0.3"
SHA256="cd2908a44ff41a20fd93b9ce8591eaa0eaa8bfab5b62d8326ff7a81eebfcc0f6"
URL="${RM_ACTIVITY_ZIP:-https://github.com/rmmarketingmcc/rm-activity/releases/download/v$VERSION/RM-Activity.zip}"
ID="dk.rmmarketing.rm-activity"
DEST="$HOME/Applications"
APP="$DEST/RM Activity.app"
DATA="$HOME/Library/Application Support/RM Activity"
AGENTS="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"

stop_jobs() {
  [[ -n "${RM_ACTIVITY_NO_SERVICES:-}" ]] && return 0
  for job in tracker app; do launchctl bootout "$GUI/$ID.$job" 2>/dev/null || true; done
}

if [[ "${1:-}" == "--uninstall" ]]; then
  stop_jobs
  rm -f "$AGENTS/$ID.tracker.plist" "$AGENTS/$ID.app.plist"
  rm -rf "$APP"
  echo "RM Activity is removed. Your recorded days are still in:"
  echo "  $DATA"
  echo "Delete that folder too if you want them gone."
  exit 0
fi

if [[ -f "$HOME/google-ads-mcp/scripts/activity_tracker.py" && -z "${RM_ACTIVITY_FORCE:-}" ]]; then
  echo "This Mac runs RM's own build of RM Activity; the public one would replace it. Nothing installed."
  exit 1
fi
if ! xcode-select -p >/dev/null 2>&1 || [[ ! -x /usr/bin/python3 ]]; then
  echo "RM Activity needs Apple's Command Line Tools (they bring Python, which does the tracking)."
  echo "Run this, click Install, then run the RM Activity line again:"
  echo "  xcode-select --install"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Downloading RM Activity..."
if [[ -f "$URL" ]]; then cp "$URL" "$TMP/RM-Activity.zip"; else curl -fsSL "$URL" -o "$TMP/RM-Activity.zip"; fi
GOT="$(shasum -a 256 "$TMP/RM-Activity.zip" | cut -d' ' -f1)"
if [[ "$GOT" != "$SHA256" ]]; then
  echo "The download does not match RM Activity $VERSION (its fingerprint differs), so nothing was installed."
  echo "Try again in a few minutes; if it keeps happening, tell the person who sent you the link."
  exit 1
fi

stop_jobs
mkdir -p "$DEST" "$DATA/data" "$AGENTS"
chmod 700 "$DATA"   # your recorded days: readable by you only
rm -rf "$APP"
ditto -x -k "$TMP/RM-Activity.zip" "$DEST/"
find "$APP" -exec xattr -d com.apple.quarantine {} + >/dev/null 2>&1 || true
RES="$APP/Contents/Resources"
[[ -f "$DATA/settings.json" ]] || cp "$RES/settings.example.json" "$DATA/settings.json"
cp "$RES/logo.png" "$DATA/logo.png"

# The tracker notes the front app every 5 seconds; the app shows it in the menu bar. Both start at login.
cat > "$AGENTS/$ID.tracker.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$ID.tracker</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/rm-activity-tracker</string></array>
  <key>AssociatedBundleIdentifiers</key><array><string>$ID</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DATA/tracker.log</string>
  <key>StandardErrorPath</key><string>$DATA/tracker.log</string>
</dict></plist>
PLIST
cat > "$AGENTS/$ID.app.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$ID.app</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/RM Activity</string><string>--login</string></array>
  <key>AssociatedBundleIdentifiers</key><array><string>$ID</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$DATA/app.log</string>
  <key>StandardErrorPath</key><string>$DATA/app.log</string>
</dict></plist>
PLIST

if [[ -n "${RM_ACTIVITY_NO_SERVICES:-}" ]]; then
  echo "Installed without starting anything (test mode): $APP"
  exit 0
fi
launchctl bootstrap "$GUI" "$AGENTS/$ID.tracker.plist"
launchctl bootstrap "$GUI" "$AGENTS/$ID.app.plist"
sleep 1
open "$APP"
echo
echo "Done! RM Activity sits at the top right of your screen, next to the clock."
echo "macOS asks twice, both optional:"
echo "  - Accessibility: lets it read window titles (which file, which chat). Settings > Privacy & Security > Accessibility > RM Activity."
echo "  - Google Chrome: lets it read the address of the tab in front. Click OK when asked."
echo "Your clients and categories: $DATA/settings.json"
