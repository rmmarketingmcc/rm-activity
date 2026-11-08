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
# The launcher runs only this exact tracker: its SHA-256 is compiled in (see app/launcher.c).
SHA=$(shasum -a 256 tracker/activity_tracker.py | cut -d' ' -f1)
clang -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 -DSCRIPT_SHA256="\"$SHA\"" \
  -o "$APP/Contents/MacOS/rm-activity-tracker" app/launcher.c
sed "s/__VERSION__/$VERSION/g" app/Info.plist > "$APP/Contents/Info.plist"
cp app/widget.html "$APP/Contents/Resources/widget.html"
cp tracker/activity_tracker.py tracker/settings.example.json "$APP/Contents/Resources/"
cp assets/logo.png assets/menubar.png assets/Poppins-Regular.ttf assets/Poppins-Bold.ttf "$APP/Contents/Resources/"
# App icon from the two PNGs (sips + iconutil): the bolder one for the small sizes.
ICONSET=build/AppIcon.iconset; mkdir -p "$ICONSET"
for base in 16 32 128 256 512; do for scale in 1 2; do
  px=$((base * scale)); src=assets/icon-1024.png; [[ $px -le 64 ]] && src=assets/icon-small.png
  name="icon_${base}x${base}"; [[ $scale == 2 ]] && name="${name}@2x"
  sips -z $px $px "$src" --out "$ICONSET/$name.png" >/dev/null
done; done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$ICONSET"
rm -f build/app-arm64 build/app-x86_64
find "$APP" -exec xattr -c {} + 2>/dev/null || true
codesign --force --deep --sign - "$APP"
(cd build && ditto -c -k --keepParent "RM Activity.app" RM-Activity.zip)
echo "built $APP ($VERSION) and build/RM-Activity.zip"
