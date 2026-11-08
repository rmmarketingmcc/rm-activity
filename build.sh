#!/bin/bash
# Builds build/RM Activity.app (Apple silicon + Intel) and build/RM-Activity.zip. Needs Xcode or the Command Line Tools.
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(cat VERSION)
APP="build/RM Activity.app"
rm -rf build
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
for arch in arm64 x86_64; do
  xcrun swiftc -O -swift-version 5 -target "$arch-apple-macos13.0" -o "build/app-$arch" app/ActivityApp.swift -framework AppKit -framework WebKit
done
lipo -create -output "$APP/Contents/MacOS/RM Activity" build/app-arm64 build/app-x86_64
clang -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 -o "$APP/Contents/MacOS/rm-activity-tracker" app/launcher.c
sed "s/__VERSION__/$VERSION/g" app/Info.plist > "$APP/Contents/Info.plist"
cp app/widget.html "$APP/Contents/Resources/widget.html"
cp tracker/activity_tracker.py tracker/settings.example.json "$APP/Contents/Resources/"
cp assets/logo.png assets/menubar.png assets/AppIcon.icns "$APP/Contents/Resources/"
rm -f build/app-arm64 build/app-x86_64
find "$APP" -exec xattr -c {} + 2>/dev/null || true
codesign --force --deep --sign - "$APP"
(cd build && ditto -c -k --keepParent "RM Activity.app" RM-Activity.zip)
echo "built $APP ($VERSION) and build/RM-Activity.zip"
