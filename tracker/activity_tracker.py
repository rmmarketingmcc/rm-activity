#!/usr/bin/env python3
"""RM Activity: a home-built Rize. Notes which app and which web page is in front every 5 seconds,
with start and end times, so we can always see where the day goes (Rasmus 2026-10-30).

What it records, on this Mac only (data/activity_tracker/, gitignored, never synced anywhere):
- the frontmost app (lsappinfo, needs no permission)
- Chrome (and other Chromium browsers): host + path + title of the active tab. Query strings and
  #fragments are dropped, so login and reset tokens never land in the database.
- Incognito windows are skipped completely: no row, no time, the URL is never even read.
- Safari: app time only. Safari's scripting cannot tell a private window from a normal one,
  so its pages are never read.
- Other apps: the title of the front window (which file/tab in VS Code, which Slack channel, which
  document in Preview), read through macOS Accessibility. Never for browsers (a private window's
  title would give the page away) or the apps in 'never_read_titles' (password managers).
- Calls (2026-11-09): which app records from the microphone (CoreAudio's per-process list, no permission,
  the audio is never touched). On a call the time is Meetings whatever window is in front, and stays
  active for 90 min without input. Only known call apps count (CALL_APPS), a browser only while a
  meeting tab is open; dictation (Wispr Flow), voice input on a page and recordings never do.
- Away: 3 min without keyboard/mouse stops the clock, trimmed back to the last input. Meetings, calls and
  video ('no_input_ok' in activity_categories.json) get 90 min. Locked screen or sleep = away, always.

Read time (page and report), nothing extra stored: booked meetings from the calendar (calendar.json,
refreshed hourly in a child process) give each meeting its name and client; Claude Code session names
are restored in full from ~/.claude/projects (VS Code cuts them); every stretch gets a client from the
meeting's guests or a client's name/domain in its title, host or path (data/client_email_registry.json).
The page has a day and a week view.

Permissions: launchd starts "RM Activity Tracker.app" (a 20-line C launcher built by install-helper),
which runs this script as its child. macOS ties Accessibility and the Chrome permission to that app,
so they are never granted to Python as a whole (which would hand every Python script on the Mac
control of the computer). Rebuilding the helper resets both permissions, so install-helper only
rebuilds when the launcher itself changed.

Categories live in scripts/activity_categories.json and are applied when the page is built,
so editing them re-sorts old days too.

Usage:
    python3 scripts/activity_tracker.py run                 # the loop (launchd com.rmmarketing.activity-tracker)
    python3 scripts/activity_tracker.py report [--date YYYY-MM-DD] [--days N | --week] [--top N] [--pages]
    python3 scripts/activity_tracker.py calendar            # refresh calendar.json now (the loop does it hourly)
    python3 scripts/activity_tracker.py page [--open]       # rebuild data/activity_tracker/index.html
    python3 scripts/activity_tracker.py status              # running? last sample? Chrome access?
    python3 scripts/activity_tracker.py install-app         # same as install-widget
    python3 scripts/activity_tracker.py install-helper      # build the launcher app + (re)load the launchd job
    python3 scripts/activity_tracker.py install-widget      # build + start ~/Applications/RM Activity.app (menu bar, panel, desktop widget)
"""
from __future__ import annotations

import argparse
import bisect
import ctypes
import fcntl
import hashlib
import json
import plistlib
import shutil
import tempfile
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

# The public app's launcher checks this file's fingerprint and hands Python the checked text on stdin, so a
# changed file never runs with the permissions macOS gave the app; then __file__ is missing and
# RM_ACTIVITY_SCRIPT says where the file is.
SELF = Path(globals().get("__file__") or os.environ.get("RM_ACTIVITY_SCRIPT") or "activity_tracker.py").resolve()
ROOT = SELF.parent.parent
# The public app (github.com/rmmarketingmcc/rm-activity) runs this same file with RM_ACTIVITY_HOME set:
# data, settings and the page then live in that one folder, clients come from the settings file and
# there is no calendar. Without it (RM's own Mac) everything stays in this repo.
APP_HOME = Path(os.environ["RM_ACTIVITY_HOME"]).expanduser() if os.environ.get("RM_ACTIVITY_HOME") else None
OUT_DIR = APP_HOME / "data" if APP_HOME else ROOT / "data" / "activity_tracker"
DB_PATH = OUT_DIR / "activity.db"
PAGE_PATH = OUT_DIR / "index.html"
STATE_PATH = OUT_DIR / "state.json"
LOCK_PATH = OUT_DIR / "run.lock"
CATEGORIES_PATH = APP_HOME / "settings.json" if APP_HOME else ROOT / "scripts" / "activity_categories.json"
CALENDAR_HELPER = ROOT / "agents" / ("calendar_helper.py" if not APP_HOME else "-")  # RM only: Google Calendar, RM's token
CALENDAR_PATH = OUT_DIR / "calendar.json"
REGISTRY_PATH = ROOT / "data" / ("client_email_registry.json" if not APP_HOME else "-")  # RM only
APP_PATH = Path.home() / "Applications" / "RM Activity.app"
LABEL = "dk.rmmarketing.rm-activity.tracker" if APP_HOME else "com.rmmarketing.activity-tracker"
PLIST_SRC = ROOT / "scripts" / f"{LABEL}.plist"
PLIST_DST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
HELPER_APP = (APP_PATH if APP_HOME else Path.home() / "Library" / "Application Support" / "RM Activity" / "RM Activity Tracker.app")
HELPER_EXE = HELPER_APP / "Contents" / "MacOS" / "rm-activity-tracker"
HELPER_ID = "dk.rmmarketing.activity-tracker"
WIDGET_JSON = OUT_DIR / "widget.json"
WIDGET_SRC = ROOT / "scripts" / "activity_widget.swift"
WIDGET_APP = APP_PATH  # one app: menu bar, panel, desktop widget (replaced the page-only app 2026-11-09)
OLD_WIDGET_APP = Path.home() / "Applications" / "RM Activity Widget.app"
WIDGET_ID = "dk.rmmarketing.activity-widget"
WIDGET_LABEL = "com.rmmarketing.activity-widget"
AX_PROMPTED = OUT_DIR / "ax_prompted"
DK = ZoneInfo("Europe/Copenhagen")
CF_UTF8 = 0x08000100

TICK = 5            # seconds between samples
IDLE_LIMIT = 180    # no input this long = away
SLEEP_GAP = 30      # a gap this long between two samples = the Mac slept or the loop stalled
PAGE_EVERY = 300    # rebuild the page this often while running
WIDGET_EVERY = 30   # rewrite widget.json (menu bar + desktop widget) this often
PAGE_DAYS = 31      # days with full detail on the page
MERGE_GAP = 30      # same site/app again within this many seconds = one stretch in the log
TAB_CARRY = 30      # a failed tab read reuses the last good one this fresh
CAL_EVERY = 3600    # refresh the calendar copy this often
CALL_TABS_EVERY = 60  # while Chrome has the mic, look for its meeting tab this often
MEETING_HOSTS = ("meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft", "whereby.com")
# A browser on the mic is a call only while one of these is open in it (voice input on a page is not).
CALL_TAB_HOSTS = MEETING_HOSTS + ("app.slack.com", "web.whatsapp.com", "voice.google.com", "messenger.com")
BROWSER_CALLS = {"com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac"}
# Who is on a call, by the bundle id of the process recording from the mic (prefix match).
CALL_APPS = {"com.google.Chrome": "Google Chrome", "com.brave.Browser": "Brave", "com.microsoft.edgemac": "Edge",
             "us.zoom.xos": "Zoom", "net.whatsapp.WhatsApp": "WhatsApp", "com.apple.FaceTime": "FaceTime",
             "com.apple.TelephonyUtilities": "Phone (iPhone)", "com.apple.telephonyutilities": "Phone (iPhone)",
             "com.microsoft.teams": "Teams", "com.microsoft.teams2": "Teams", "com.tinyspeck.slackmacgap": "Slack",
             "ai.meetgeek": "MeetGeek", "dk.rmmarketing.callcopilot": "Sales Coach", "com.hnc.Discord": "Discord"}

CHROMIUM = {"com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.canary", "com.brave.Browser",
            "com.microsoft.edgemac", "com.vivaldi.Vivaldi"}
NOT_ACTIVITY = {"com.apple.loginwindow", "com.apple.ScreenSaver.Engine", "dk.rmmarketing.activity-widget", "dk.rmmarketing.rm-activity"}  # a glance at the widget is no activity
# Browsers whose private windows we cannot detect: their window titles are never read.
NO_TITLE_BUNDLES = {"com.apple.Safari", "com.apple.SafariTechnologyPreview", "org.mozilla.firefox",
                    "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly", "company.thebrowser.Browser",
                    "company.thebrowser.dia", "com.kagi.kagimacOS", "com.operasoftware.Opera", "app.zen-browser.zen",
                    "com.duckduckgo.macos.browser", "org.torproject.torbrowser"}

TAB_SCRIPT = """tell application id "%s"
  if (count of windows) is 0 then return "NOWINDOW"
  set w to front window
  if (mode of w) is "incognito" then return "INCOGNITO"
  set t to active tab of w
  return "OK" & linefeed & (URL of t) & linefeed & (title of t)
end tell"""


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- categories

class Categories:
    """First match wins: a page goes by its host, anything else by its app. Unmatched = Other."""

    def __init__(self) -> None:
        self.mtime = 0.0
        self.load()

    def load(self) -> None:
        cfg = json.loads(CATEGORIES_PATH.read_text())
        self.rules = cfg["categories"][:8]
        self.names = [c["name"] for c in self.rules]
        self.other = len(self.names)
        quiet = cfg.get("no_input_ok", {})
        self.quiet_hosts = quiet.get("hosts", [])
        self.quiet_apps = set(quiet.get("apps", []))
        self.quiet_limit = int(quiet.get("minutes", 90)) * 60
        self.no_title_apps = set(cfg.get("never_read_titles", []))
        calls = cfg.get("calls", {})
        self.not_a_call = tuple(calls.get("not_a_call", []))
        self.evidence = int(calls.get("booked_meeting_evidence_minutes", 2)) * 60
        self.meetings = self.names.index("Meetings") if "Meetings" in self.names else None
        self.client_cfg = cfg.get("clients", {})
        self.mtime = CATEGORIES_PATH.stat().st_mtime

    def reload_if_changed(self) -> None:
        try:
            if CATEGORIES_PATH.stat().st_mtime != self.mtime:
                self.load()
        except (OSError, ValueError, KeyError) as e:
            log(f"categories file unreadable, keeping the old ones: {e}")

    @staticmethod
    def _host_in(host: str, patterns) -> bool:
        return any(host == p or host.endswith("." + p) for p in patterns)

    def of(self, app: str, bundle: str, host: str) -> int:
        for i, rule in enumerate(self.rules):
            if host:
                if self._host_in(host, rule.get("hosts", [])):
                    return i
            elif app in rule.get("apps", []) or bundle in rule.get("apps", []):
                return i
        return self.other

    def idle_limit(self, app: str, host: str) -> int:
        if app in self.quiet_apps or (host and self._host_in(host, self.quiet_hosts)):
            return self.quiet_limit
        return IDLE_LIMIT


# ---------------------------------------------------------------- sampling

_cg = _cf = None


def _quartz():
    global _cg, _cf
    if _cg is None:
        _cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        _cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        _cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        _cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        _cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        _cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        _cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        _cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _cf.CFBooleanGetValue.restype = ctypes.c_bool
        _cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        _cf.CFRelease.argtypes = [ctypes.c_void_p]
    return _cg, _cf


def idle_seconds() -> float:
    """Seconds since the last keyboard, mouse or trackpad input (hardware events only)."""
    cg, _ = _quartz()
    return cg.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF)  # HID system state, any input type


def screen_locked() -> bool:
    cg, cf = _quartz()
    session = cg.CGSessionCopyCurrentDictionary()
    if not session:
        return True  # no GUI session means nobody at the screen
    try:
        key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", CF_UTF8)
        try:
            value = cf.CFDictionaryGetValue(session, key)
            return bool(value) and cf.CFBooleanGetValue(value)
        finally:
            cf.CFRelease(key)
    finally:
        cf.CFRelease(session)


_ax = None
_AX_ATTR: dict[str, int] = {}


def _axlib():
    global _ax
    _, cf = _quartz()
    if _ax is None:
        vp = ctypes.c_void_p
        _ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        _ax.AXIsProcessTrusted.restype = ctypes.c_bool
        _ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        _ax.AXIsProcessTrustedWithOptions.argtypes = [vp]
        _ax.AXUIElementCreateApplication.restype = vp
        _ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        _ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        _ax.AXUIElementCopyAttributeValue.argtypes = [vp, vp, ctypes.POINTER(vp)]
        _ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int32
        _ax.AXUIElementSetMessagingTimeout.argtypes = [vp, ctypes.c_float]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [vp]
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        for name in ("AXFocusedWindow", "AXTitle"):  # kept for the life of the process
            _AX_ATTR[name] = cf.CFStringCreateWithCString(None, name.encode(), CF_UTF8)
    return _ax, cf


def ax_trusted(prompt: bool = False) -> bool:
    """Whether macOS lets us read window titles. prompt=True also shows the system dialog that adds
    the responsible app (RM Activity Tracker under launchd) to Settings > Privacy & Security > Accessibility."""
    ax, cf = _axlib()
    if not prompt:
        return ax.AXIsProcessTrusted()
    keys = (ctypes.c_void_p * 1)(ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt").value)
    vals = (ctypes.c_void_p * 1)(ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value)
    opts = cf.CFDictionaryCreate(None, keys, vals, 1,
                                 ctypes.addressof(ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
                                 ctypes.addressof(ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
    try:
        return ax.AXIsProcessTrustedWithOptions(opts)
    finally:
        cf.CFRelease(opts)


def ax_trusted_fresh() -> bool:
    """Ask from a new process: macOS caches the Accessibility answer per process, so a grant made
    while the tracker runs only shows up in a process started after it."""
    code = ("import ctypes; ax = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices'); ax.AXIsProcessTrusted.restype = ctypes.c_bool; print(ax.AXIsProcessTrusted())")
    try:
        return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                              timeout=10).stdout.strip() == "True"
    except (subprocess.SubprocessError, OSError):
        return False


def _ax_copy(element: int, attr: str) -> int | None:
    ax, _ = _axlib()
    out = ctypes.c_void_p()
    return out.value if ax.AXUIElementCopyAttributeValue(element, _AX_ATTR[attr], ctypes.byref(out)) == 0 else None


def window_title(pid: int) -> str:
    """Title of the app's focused window ('' if it has none or we may not read it)."""
    ax, cf = _axlib()
    app = ax.AXUIElementCreateApplication(pid)
    if not app:
        return ""
    try:
        ax.AXUIElementSetMessagingTimeout(app, 0.5)  # a hung app must not stall the loop
        win = _ax_copy(app, "AXFocusedWindow")
        if not win:
            return ""
        try:
            value = _ax_copy(win, "AXTitle")
            if not value:
                return ""
            try:
                if cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
                    return ""
                buf = ctypes.create_string_buffer(8192)
                return buf.value.decode("utf-8", "replace") if cf.CFStringGetCString(value, buf, len(buf), CF_UTF8) else ""
            finally:
                cf.CFRelease(value)
        finally:
            cf.CFRelease(win)
    finally:
        cf.CFRelease(app)


def front_app() -> tuple[str, str, int] | None:
    """(app name, bundle id, pid) of the frontmost app."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(["lsappinfo", "info", "-only", "name", "-only", "bundleid", "-only", "pid", asn],
                             capture_output=True, text=True, timeout=3).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    name = re.search(r'^"(.*?)" ASN:', out, re.M)
    bundle = re.search(r'bundleID="([^"]+)"', out)
    pid = re.search(r"\bpid = (\d+)", out)
    if not name and not bundle:
        return None
    return ((name.group(1) if name else bundle.group(1)), (bundle.group(1) if bundle else ""),
            int(pid.group(1)) if pid else 0)


def browser_tab(bundle: str) -> tuple[str, str, str]:
    """('ok', url, title) | ('incognito', '', '') | ('none', '', '') | ('error', message, '')."""
    try:
        r = subprocess.run(["osascript", "-e", TAB_SCRIPT % bundle], capture_output=True, text=True, timeout=4)
    except subprocess.TimeoutExpired:
        return "error", "browser did not answer within 4 s", ""
    if r.returncode != 0:
        return "error", r.stderr.strip()[:200], ""
    out = r.stdout.rstrip("\n")
    if out == "INCOGNITO":
        return "incognito", "", ""
    if out.startswith("OK\n"):
        _, url, title = (out.split("\n", 2) + ["", ""])[:3]
        return "ok", url, title
    return "none", "", ""


class _AOAddr(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_ca = None


def _coreaudio():
    global _ca
    if _ca is None:
        vp, u32p = ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
        _ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        _ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, vp, ctypes.c_uint32, vp, u32p]
        _ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, vp, ctypes.c_uint32, vp, u32p, vp]
    return _ca


def _ao_get(obj: int, selector: bytes, out) -> bool:
    addr = _AOAddr(int.from_bytes(selector, "big"), int.from_bytes(b"glob", "big"), 0)
    size = ctypes.c_uint32(ctypes.sizeof(out))
    return _coreaudio().AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(out)) == 0


def mic_users() -> set[str]:
    """Bundle ids of the processes recording from a microphone right now. CoreAudio's per-process list
    (macOS 14.2+) needs no permission and never touches the audio itself."""
    ca, (_, cf) = _coreaudio(), _axlib()
    addr = _AOAddr(int.from_bytes(b"prs#", "big"), int.from_bytes(b"glob", "big"), 0)
    size = ctypes.c_uint32(0)
    if ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(addr), 0, None, ctypes.byref(size)) or not size.value:
        return set()
    ids = (ctypes.c_uint32 * (size.value // 4))()
    if ca.AudioObjectGetPropertyData(1, ctypes.byref(addr), 0, None, ctypes.byref(size), ids):
        return set()
    users = set()
    for obj in ids:
        flag = ctypes.c_uint32(0)
        if not (_ao_get(obj, b"piri", flag) and flag.value):  # kAudioProcessPropertyIsRunningInput
            continue
        ref = ctypes.c_void_p()
        bundle = ""
        if _ao_get(obj, b"pbid", ref) and ref.value:  # kAudioProcessPropertyBundleID
            buf = ctypes.create_string_buffer(512)
            if cf.CFStringGetCString(ref.value, buf, len(buf), CF_UTF8):
                bundle = buf.value.decode("utf-8", "replace")
            cf.CFRelease(ref.value)
        users.add(bundle or "unknown")
    return users


MEET_TABS_SCRIPT = """tell application id "%s"
  set AppleScript's text item delimiters to linefeed
  set out to ""
  repeat with w in windows
    if (mode of w) is not "incognito" then set out to out & ((URL of every tab of w) as text) & linefeed
  end repeat
  return out
end tell"""


def _prefixed(bundle: str, prefixes) -> str | None:
    return next((p for p in prefixes if bundle == p or bundle.startswith(p + ".")), None)


def call_now(cats: Categories, notes: dict) -> str:
    """'' or who has the mic: 'meet.google.com' (the meeting tab, for a browser), 'Zoom', 'WhatsApp', ...
    Only known call apps count (CALL_APPS), and a browser only while a meeting tab is open, so dictation
    (Wispr Flow), voice input on a web page or a screen recording is never a meeting. 'calls.not_a_call'
    wins over everything. Any other mic user is logged once, so a missing call app can be added."""
    try:
        users = mic_users()
    except (OSError, AttributeError, ValueError) as e:
        if notes.get("mic") != "error":
            log(f"cannot read who uses the microphone, calls are not detected: {e}")
        notes["mic"] = "error"
        return ""
    notes["mic"] = "ok"
    logged = notes.setdefault("mic_logged", set())
    labels = []
    for b in sorted(users):
        if _prefixed(b, cats.not_a_call):
            continue
        app = _prefixed(b, CALL_APPS)
        if app is None:
            if b not in logged:
                logged.add(b)
                log(f"microphone used by {b}: not a call app, not counted (add it to CALL_APPS if it is one)")
            continue
        if app in BROWSER_CALLS:
            host = browser_meeting_tab(app, notes)
            if not host:
                if app not in logged:
                    logged.add(app)
                    log(f"{CALL_APPS[app]} uses the microphone without a meeting tab open: not counted as a call")
                continue
            labels.append(host)
        else:
            labels.append(CALL_APPS[app])
    return labels[0] if labels else ""


def browser_meeting_tab(bundle: str, notes: dict) -> str:
    """Host of an open call tab in the browser (meet.google.com, zoom.us, ...), checked once a minute."""
    cached = notes.setdefault("call_tab", {}).get(bundle)
    if cached and time.time() - cached[1] < CALL_TABS_EVERY:
        return cached[0]
    host = ""
    try:
        r = subprocess.run(["osascript", "-e", MEET_TABS_SCRIPT % bundle], capture_output=True, text=True, timeout=4)
        for url in r.stdout.split("\n"):
            h, _ = clean_url(url.strip())
            if h and any(h == m or h.endswith("." + m) for m in CALL_TAB_HOSTS):
                host = "zoom.us" if h.endswith("zoom.us") else h
                break
    except (subprocess.SubprocessError, OSError):
        pass
    notes["call_tab"][bundle] = (host, time.time())
    return host


def clean_url(url: str) -> tuple[str, str]:
    """(host, path): www. stripped, query string and #fragment dropped so tokens are never stored."""
    try:
        p = urlsplit(url)
    except ValueError:
        return "", ""
    if p.scheme in ("http", "https"):
        host = (p.hostname or "").lower()
        return (host[4:] if host.startswith("www.") else host), (p.path or "/")[:300]
    if p.scheme == "file":
        return "local file", p.path[:300]
    if p.scheme in ("chrome", "edge", "brave", "vivaldi", "about"):  # chrome://newtab -> "chrome:newtab"
        return f"{p.scheme}:{p.netloc or p.path}".rstrip(":")[:60], "/"
    return "", ""  # extensions and the like: counts as app time only


_COUNTER = re.compile(r"\s*\(\d[\d,.]*\+?\)\s*")
_LONG_DASH = re.compile(r"\s+[" + chr(0x2014) + chr(0x2013) + r"]\s+")  # VS Code's title separator
_UNSAVED = re.compile(r"^[" + chr(0x25CF) + chr(0x2022) + r"]\s*")      # VS Code's unsaved-file dot


def clean_title(title: str) -> str:
    """Drop unread counters like 'Inbox (23)' / '(3) Feed' and VS Code's unsaved dot, so a new mail or
    a keystroke is not a new page. Long dashes become plain ones."""
    title = _LONG_DASH.sub(" - ", _COUNTER.sub(" ", _UNSAVED.sub("", title or "")))
    return re.sub(r"\s+", " ", title).strip()[:200]


def sample(cats: Categories, notes: dict) -> tuple[str, tuple | None, float]:
    """('active', key, idle) | ('away', None, idle) | ('skip', None, idle). key = (app, bundle, host, path, title, call).
    call = who has the mic ('' when nobody): on a call you are active for 90 min without input, whatever is in front."""
    if screen_locked():
        return "away", None, 0.0
    idle = idle_seconds()
    call = call_now(cats, notes)
    front = front_app()
    if not front or front[1] in NOT_ACTIVITY:
        return "skip", None, idle
    app, bundle, pid = front
    host = path = title = ""
    if bundle not in CHROMIUM:
        if (notes.get("titles") == "ok" and pid and bundle not in NO_TITLE_BUNDLES
                and app not in cats.no_title_apps and bundle not in cats.no_title_apps):
            title = clean_title(window_title(pid))
    else:
        state, url, raw_title = browser_tab(bundle)
        if state == "incognito":
            return "skip", None, idle
        if state == "ok":
            host, path = clean_url(url)
            title = clean_title(raw_title) if host else ""
            notes["browser"] = "ok"
            notes["last_tab"] = (bundle, host, path, title, time.time())
        elif state == "error":
            # A tab mid-load or a window closing fails one read; keep the page read just before it.
            last_tab = notes.get("last_tab")
            if last_tab and last_tab[0] == bundle and time.time() - last_tab[4] <= TAB_CARRY:
                host, path, title = last_tab[1:4]
            notes["browser"] = f"error: {url}"
            if time.time() - notes.get("browser_logged", 0) > 3600:
                notes["browser_logged"] = time.time()
                log(f"cannot read {app} tabs, logging app time only ({url}). "
                    "Fix: System Settings > Privacy & Security > Automation > allow Python to control Google Chrome.")
    if idle > (cats.quiet_limit if call else cats.idle_limit(app, host)):
        return "away", None, idle
    return "active", (app, bundle, host, path, title, call), idle


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY,
  start_ts REAL NOT NULL,
  end_ts REAL NOT NULL,
  app TEXT NOT NULL,
  bundle_id TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_start ON sessions(start_ts);
CREATE INDEX IF NOT EXISTS sessions_end ON sessions(end_ts);
"""


def connect(db: Path = DB_PATH) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    if "call" not in {r[1] for r in con.execute("PRAGMA table_info(sessions)")}:
        con.execute("ALTER TABLE sessions ADD COLUMN call TEXT NOT NULL DEFAULT ''")  # 2026-11-09: who had the mic
        con.commit()
    return con


def load_rows(con: sqlite3.Connection, t0: float, t1: float) -> list[tuple]:
    return con.execute(
        "SELECT start_ts, end_ts, app, bundle_id, host, path, title, call FROM sessions "
        "WHERE end_ts > ? AND start_ts < ? AND end_ts > start_ts ORDER BY start_ts", (t0, t1)).fetchall()


# ---------------------------------------------------------------- the loop

def run() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("activity tracker already running (another process holds data/activity_tracker/run.lock)")
    con = connect()
    cats = Categories()
    notes: dict = {}
    cur: dict | None = None  # {"id", "key", "start"}
    last = last_call = 0.0
    last_page = last_state = last_ax = last_cal = last_widget = 0.0
    cal_job: subprocess.Popen | None = None
    log("started")
    while True:
        now = time.time()
        try:
            if now - last_ax > 60:
                last_ax = now
                had = notes.get("titles")
                notes["titles"] = "ok" if ax_trusted() else "no permission"
                if notes["titles"] != had:
                    log(f"window titles: {notes['titles']}")
                if notes["titles"] != "ok" and ax_trusted_fresh():
                    log("Accessibility was just granted; restarting to pick it up")
                    con.close()
                    if APP_HOME:  # public app: restart through the launcher (launchd), which checks the file again
                        sys.exit(75)
                    os.execv(sys.executable, [sys.executable, str(SELF), "run"])
                if notes["titles"] != "ok" and (not AX_PROMPTED.exists() or now - AX_PROMPTED.stat().st_mtime > 7 * 86400):
                    AX_PROMPTED.touch()
                    ax_trusted(prompt=True)
                    log("asked macOS for Accessibility: System Settings > Privacy & Security > Accessibility > RM Activity Tracker")
            if cur and last and now - last > SLEEP_GAP:
                cur = None  # slept: the open stretch already ends at the last sample
            state, key, idle = sample(cats, notes)
            if state == "active":
                if key[5]:
                    last_call = now
                if cur and cur["key"] == key:
                    con.execute("UPDATE sessions SET end_ts=? WHERE id=?", (now, cur["id"]))
                else:
                    if cur:
                        con.execute("UPDATE sessions SET end_ts=? WHERE id=?", (now, cur["id"]))
                    app, bundle, host, path, title, call = key
                    cid = con.execute("INSERT INTO sessions(start_ts,end_ts,app,bundle_id,host,path,title,call) "
                                      "VALUES(?,?,?,?,?,?,?,?)", (now, now, app, bundle, host, path, title, call)).lastrowid
                    cur = {"id": cid, "key": key, "start": now}
            elif state == "away":
                if cur:
                    if idle:  # trim everything after the last input, but never a call you sat listening to
                        last_input = max(now - idle, min(last_call, now))
                        con.execute("UPDATE sessions SET end_ts=MAX(start_ts, ?) WHERE end_ts > ?", (last_input, last_input))
                        con.execute("DELETE FROM sessions WHERE end_ts - start_ts < 1 AND start_ts >= ?", (last_input,))
                    cur = None
            else:  # incognito, login window: the previous stretch ends here, nothing new starts
                if cur:
                    con.execute("UPDATE sessions SET end_ts=? WHERE id=?", (now, cur["id"]))
                    cur = None
            con.commit()
            last = now
            if now - last_state > 60:
                last_state = now
                STATE_PATH.write_text(json.dumps({"pid": os.getpid(), "last_sample": now, "state": state,
                                                  "browser": notes.get("browser", "not checked yet"),
                                                  "titles": notes.get("titles", "not checked yet"),
                                                  "mic": notes.get("mic", "not checked yet"),
                                                  "call": key[5] if state == "active" else ""}))
            if cal_job and cal_job.poll() is not None:
                cal_job = None
            if now - last_cal > CAL_EVERY and not cal_job and CALENDAR_HELPER.exists():
                last_cal = now
                cal_job = refresh_calendar_async()
            if now - last_page > PAGE_EVERY:
                last_page = now
                cats.reload_if_changed()
                build_page(con, cats)
            if now - last_widget > WIDGET_EVERY:
                last_widget = now
                build_widget(con, cats)
        except Exception as e:  # keep tracking whatever happens; the log says what broke
            log(f"tick failed: {type(e).__name__}: {e}")
        time.sleep(max(0.5, TICK - (time.time() - now)))


# ---------------------------------------------------------------- per-day numbers

def day_start(d: date) -> float:
    return datetime.combine(d, dtime.min).timestamp()


def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------- calendar (booked meetings)

def fetch_calendar(con: sqlite3.Connection) -> int:
    """Copy the calendar's meetings (events with at least one other guest, not declined) since the first
    tracked day into calendar.json, so meetings get their name and client. Read-only, local file."""
    sys.path.insert(0, str(ROOT / "agents"))
    import requests
    from calendar_helper import CALENDAR_API, PRIMARY_CAL, get_calendar_creds
    from google.auth.transport.requests import Request
    me = {e.lower() for e in json.loads(CATEGORIES_PATH.read_text()).get("clients", {}).get("self_emails", [])}
    creds = get_calendar_creds()
    if not creds.valid:
        creds.refresh(Request())
    start = (first_day(con) or date.today()) - timedelta(days=1)
    params = {"timeMin": datetime.combine(start, dtime.min).astimezone().isoformat(),
              "timeMax": (datetime.now().astimezone() + timedelta(days=2)).isoformat(),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
    items: list[dict] = []
    while True:
        r = requests.get(f"{CALENDAR_API}/calendars/{PRIMARY_CAL}/events", params=params,
                         headers={"Authorization": f"Bearer {creds.token}"}, timeout=20)
        r.raise_for_status()
        body = r.json()
        items += body.get("items", [])
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    events = []
    for ev in items:
        s, e = ev.get("start", {}).get("dateTime"), ev.get("end", {}).get("dateTime")
        guests = ev.get("attendees", [])
        if not s or not e or ev.get("status") == "cancelled":
            continue  # all-day blocks have no dateTime
        if any(g.get("self") and g.get("responseStatus") == "declined" for g in guests):
            continue
        others = sorted({g["email"].lower() for g in guests if g.get("email") and not g.get("self")
                         and not g.get("resource") and g["email"].lower() not in me})
        if not others:
            continue  # prep slots, focus blocks, personal time
        text = " ".join(str(ev.get(k) or "") for k in ("hangoutLink", "location", "description"))[:4000]
        events.append({"s": datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp(),
                       "e": datetime.fromisoformat(e.replace("Z", "+00:00")).timestamp(),
                       "title": (ev.get("summary") or "(no title)").strip()[:150], "guests": others[:25],
                       "video": bool(ev.get("hangoutLink") or ev.get("conferenceData")
                                     or re.search(r"zoom\.us|teams\.microsoft|meet\.google|whereby", text))})
    tmp = CALENDAR_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched": time.time(), "events": events}, ensure_ascii=False))
    tmp.replace(CALENDAR_PATH)
    return len(events)


def refresh_calendar_async() -> subprocess.Popen | None:
    """Fetch the calendar in a child process, so a slow network never stalls the 5-second loop."""
    try:
        if CALENDAR_PATH.exists() and time.time() - CALENDAR_PATH.stat().st_mtime < CAL_EVERY:
            return None
        with open(OUT_DIR / "tracker.log", "a") as logf:
            return subprocess.Popen([sys.executable, str(SELF), "calendar"], stdin=subprocess.DEVNULL,
                                    stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        log(f"calendar refresh not started: {e}")
        return None


def load_meetings() -> list[dict]:
    try:
        return json.loads(CALENDAR_PATH.read_text()).get("events", [])
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------- clients

_FOLD = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "é": "e", "è": "e", "ä": "ae", "ö": "oe", "ü": "u"})


def _norm(s: str) -> str:
    """Lowercase, æøå spelled out, - and _ as spaces: 'Bøgehøj', 'boegehoej-vvs' and 'Boegehoej' meet."""
    return re.sub(r"[\s_\-]+", " ", (s or "").lower().translate(_FOLD)).strip()


def _segments(name: str) -> list[str]:
    """'Jens Hansen (Hansen VVS / Hansen Bad)' -> ['Jens Hansen', 'Hansen VVS', 'Hansen Bad']."""
    name = re.sub(r"\b(ApS|A/S|I/S|IVS|P/S)\b", " ", name.strip().strip('"'))
    return [p for p in (re.sub(r"\s+", " ", x).strip(" .-\"") for x in re.split(r"\s*[()/,+]\s*|\s+-\s+", name)) if p]


class Clients:
    """Which client or lead a stretch belongs to. A booked meeting goes by its guests (mail address, then
    domain); anything else by a client's name or domain in the page title, host or path. Names, mail
    addresses and domains come from data/client_email_registry.json (clients + leads, entries sharing a
    domain merged); 'clients' in activity_categories.json adds aliases, display names and stop words."""

    GENERIC = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.dk", "outlook.com", "outlook.dk", "live.com",
               "live.dk", "icloud.com", "me.com", "yahoo.com", "yahoo.dk", "mail.dk", "jubii.dk", "protonmail.com"}

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.me = {e.lower() for e in cfg.get("self_emails", [])}
        self.team_emails = {e.lower() for e in cfg.get("team_emails", [])}
        self.team_domains = {d.lower() for d in cfg.get("team_domains", [])}
        self.team = cfg.get("team_label", "RM team")
        self.names: list[str] = []
        self.by_email: dict[str, int] = {}
        self.by_domain: dict[str, int] = {}
        self.alias_of: dict[str, int] = {}
        self.regex: re.Pattern | None = None
        self._cache: dict[tuple, str] = {}
        reg: dict = {"all_clients": []}
        if REGISTRY_PATH.exists():  # RM: every client and lead with mail addresses and domains
            try:
                reg = json.loads(REGISTRY_PATH.read_text())
            except (OSError, ValueError) as e:
                log(f"client registry unreadable: {e}")
        # Anyone: "clients": {"list": [{"name": "Acme", "aliases": ["acme"], "domains": ["acme.com"], "emails": []}]}
        own = [{"client_slug": "cfg:" + c["name"], "client_name": c["name"], "entry_type": "client",
                "domains": c.get("domains", []), "contact_emails": c.get("emails", [])}
               for c in cfg.get("list", []) if isinstance(c, dict) and c.get("name")]
        self.cfg = {**cfg, "aliases": {**cfg.get("aliases", {}),
                                       **{"cfg:" + c["name"]: c.get("aliases", []) for c in cfg.get("list", []) if isinstance(c, dict) and c.get("name")}}}
        try:
            self._load({"all_clients": list(reg.get("all_clients", [])) + own})
        except (KeyError, TypeError, re.error) as e:
            log(f"client list unusable, no client view: {e}")

    def _load(self, reg: dict) -> None:
        entries = [x for x in reg.get("all_clients", []) if x.get("entry_type") in ("client", "lead")]
        ignore = self.GENERIC | {d.lower() for d in self.cfg.get("ignore_domains", [])}
        stop = {_norm(w) for w in self.cfg.get("alias_stoplist", [])}
        stopwords = {_norm(w) for w in self.cfg.get("alias_stopwords", [])}
        names_cfg, extra = self.cfg.get("names", {}), self.cfg.get("aliases", {})

        parent = list(range(len(entries)))  # merge entries that share a domain or a name

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        owner: dict[str, int] = {}
        for i, x in enumerate(entries):
            segs = _segments(x.get("client_name", ""))
            keys = [d.lower() for d in x.get("domains", []) if d.lower() not in ignore] + (["name:" + _norm(segs[0])] if segs else [])
            for k in keys:
                if k in owner:
                    parent[find(i)] = find(owner[k])
                else:
                    owner[k] = i
        groups: dict[int, list] = defaultdict(list)
        for i, x in enumerate(entries):
            groups[find(i)].append(x)

        claims: dict[str, set] = defaultdict(set)
        for members in groups.values():
            slugs = [m.get("client_slug", "") for m in members]
            lead = next((m for m in members if m.get("entry_type") == "client"), members[0])
            name = next((names_cfg[s] for s in slugs if s in names_cfg), None) or (_segments(lead.get("client_name", "")) or ["?"])[0]
            idx = len(self.names)
            self.names.append(name)
            aliases = {name}
            for m in members:
                for e in m.get("contact_emails", []):
                    self.by_email[e.lower()] = idx
                for d in m.get("domains", []):
                    d = d.lower()
                    if d not in ignore:
                        self.by_domain[d] = idx
                        aliases.add(d.split(".")[0])
                for seg in _segments(m.get("client_name", "")):
                    if not set(_norm(seg).split()) & stopwords:
                        aliases.add(seg)
            for s in slugs:
                for a in extra.get(s, []):
                    if "." in a and " " not in a:
                        self.by_domain[a.lower()] = idx
                    aliases.add(a.split(".")[0] if "." in a and " " not in a else a)
            for a in aliases:
                claims[_norm(a)].add(idx)
        # An alias two clients share (one owner running two businesses) belongs to neither.
        self.alias_of = {a: next(iter(i)) for a, i in claims.items() if len(i) == 1 and len(a) >= 4 and a not in stop}
        if self.alias_of:
            alts = sorted(self.alias_of, key=len, reverse=True)
            self.regex = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a in alts) + r")(?![a-z0-9])")

    def _text(self, text: str) -> str:
        m = self.regex.search(_norm(text)) if self.regex else None
        return self.names[self.alias_of[m.group(1)]] if m else ""

    def of(self, host: str, path: str, title: str) -> str:
        key = (host, path, title)
        hit = self._cache.get(key)
        if hit is None:
            hit = ""
            parts = host.split(".") if host and "." in host else []
            for i in range(len(parts) - 1):
                idx = self.by_domain.get(".".join(parts[i:]))
                if idx is not None:
                    hit = self.names[idx]
                    break
            hit = hit or self._text(f"{path} {title}")
            if len(self._cache) > 50000:
                self._cache.clear()
            self._cache[key] = hit
        return hit

    def of_meeting(self, guests: list[str], title: str) -> str:
        others = [g for g in guests if g not in self.me]
        for g in others:
            if g in self.by_email:
                return self.names[self.by_email[g]]
        for g in others:
            idx = self.by_domain.get(g.rsplit("@", 1)[-1])
            if idx is not None:
                return self.names[idx]
        if others and all(g in self.team_emails or g.rsplit("@", 1)[-1] in self.team_domains for g in others):
            return self.team
        return self._text(title)


# ---------------------------------------------------------------- Claude Code session names

SESSIONS_DIR = Path.home() / ".claude" / "projects"  # one folder per project, a .jsonl per session
TITLES_PATH = OUT_DIR / "session_titles.json"
_TITLE_RE = re.compile(rb'"(aiTitle|customTitle)":"((?:[^"\\]|\\.){1,300})"')
_CUT_TITLE = re.compile("^(.+?)… - (.+)$")  # VS Code: '<tab name cut short>… - <workspace>'


class SessionTitles:
    """Claude Code session names in full. VS Code cuts a session's tab name ('All tracking setup for a…');
    the session file keeps the whole name ('All tracking setup for all clients // Consent Mode v2 ...'),
    so the page shows it and the client match can find a client in it. Reads the last 512 KB of each
    session file changed since the last read (names are written there over and over); cache on disk."""

    def __init__(self, since: float) -> None:
        self.names: list[tuple[int, float, str]] = []  # (custom name first, newest first, name)
        try:
            cache = json.loads(TITLES_PATH.read_text())
        except (OSError, ValueError):
            cache = {}
        files, changed = {}, False
        try:
            paths = list(SESSIONS_DIR.glob("*/*.jsonl"))
        except OSError:
            paths = []
        for p in paths:
            try:
                st = p.stat()
                if st.st_mtime < since:
                    continue
                key = f"{p.parent.name}/{p.name}"
                old = cache.get(key)
                if old and old[0] == st.st_mtime:
                    files[key] = old
                    continue
                with open(p, "rb") as f:
                    f.seek(max(0, st.st_size - 512 * 1024))
                    chunk = f.read()
            except OSError:
                continue
            ai = custom = ""
            for kind, raw in _TITLE_RE.findall(chunk):
                try:
                    text = json.loads(b'"' + raw + b'"')
                except ValueError:
                    continue
                if kind == b"aiTitle":
                    ai = text
                else:
                    custom = text
            files[f"{p.parent.name}/{p.name}"] = [st.st_mtime, ai, custom]
            changed = True
        if changed or len(files) != len(cache):
            try:
                TITLES_PATH.write_text(json.dumps(files, ensure_ascii=False))
            except OSError:
                pass
        for mtime, ai, custom in files.values():
            if custom:
                self.names.append((1, mtime, custom))
            if ai:
                self.names.append((0, mtime, ai))
        self.names.sort(reverse=True)
        self._cache: dict[str, str] = {}

    def expand(self, title: str) -> str:
        if "…" not in title:
            return title
        hit = self._cache.get(title)
        if hit is None:
            m = _CUT_TITLE.match(title)
            full = m and next((n for _, _, n in self.names if n.startswith(m.group(1)) and len(n) > len(m.group(1))), None)
            hit = f"{full[:160]} - {m.group(2)}" if full else title
            self._cache[title] = hit
        return hit


# ---------------------------------------------------------------- days and weeks

def _meeting_hint(app: str, bundle: str, host: str, cats: Categories) -> bool:
    """What a video call looks like in front: the meeting tab, a meeting app, or Chrome with no readable
    page (Meet's picture-in-picture window and screen sharing read that way)."""
    if host:
        return any(host == m or host.endswith("." + m) for m in MEETING_HOSTS)
    return bundle in CHROMIUM or (cats.meetings is not None and cats.of(app, bundle, "") == cats.meetings)


def summarize(ps: list[tuple], cats: Categories, with_segs: bool = False) -> dict:
    """Add up pieces (a, b, app, host, path, title, cat, client, call, meeting): categories, clients,
    sites with their pages, apps with their windows, and (for a day) the stretches for the timeline/log."""
    segs: list[dict] = []
    sites: dict[str, dict] = {}
    apps: dict[str, list] = {}
    clients: dict[str, list] = {}
    cat_total = [0.0] * (cats.other + 1)
    for a, b, app, host, path, title, cat, client, call, m in ps:
        secs = b - a
        label = host or app
        cat_total[cat] += secs
        entry = apps.setdefault(app, [0.0, Counter()])
        entry[0] += secs
        if title and not host:  # window titles; a browser's pages live under sites
            entry[1][title] += secs
        if host:
            site = sites.setdefault(host, {"secs": 0.0, "cats": Counter(), "pages": defaultdict(lambda: [0.0, Counter()])})
            site["secs"] += secs
            site["cats"][cat] += secs
            page = site["pages"][path]
            page[0] += secs
            page[1][title] += secs
        c = clients.setdefault(client, [0.0, Counter()])
        c[0] += secs
        what = m["title"] if m is not None and cat == cats.meetings else (title or (host + path if host else app))
        c[1][(what, "meeting" if m is not None and cat == cats.meetings else label)] += secs
        if with_segs:
            g = segs[-1] if segs else None
            if g and g["label"] == label and g["cat"] == cat and a - g["e"] <= MERGE_GAP:
                g["e"] = max(g["e"], b)
                g["titles"][title] += secs
                g["clients"][client] += secs
                g["call"] = g["call"] or call
            else:
                segs.append({"s": a, "e": b, "label": label, "cat": cat, "app": app, "titles": Counter({title: secs}),
                             "clients": Counter({client: secs}), "call": call})
    out = {
        "total": round(sum(cat_total)),
        "cats": [round(x) for x in cat_total],
        "clients": [[name, round(v[0]), [[w, where, round(s)] for (w, where), s in v[1].most_common(10) if s >= 1]]
                    for name, v in sorted(clients.items(), key=lambda kv: -kv[1][0])],
        "sites": [[host, round(v["secs"]), v["cats"].most_common(1)[0][0],
                   [[pg[1].most_common(1)[0][0], path, round(pg[0])]
                    for path, pg in sorted(v["pages"].items(), key=lambda kv: -kv[1][0])[:12]]]
                  for host, v in sorted(sites.items(), key=lambda kv: -kv[1]["secs"])[:40]],
        "apps": [[app, round(v[0]), [[t, round(s)] for t, s in v[1].most_common(10) if s >= 1]]
                 for app, v in sorted(apps.items(), key=lambda kv: -kv[1][0])[:15]],
    }
    if with_segs:
        out["segs"] = [[round(g["s"]), round(g["e"]), g["label"], g["cat"], g["titles"].most_common(1)[0][0], g["app"],
                        [[t, round(s)] for t, s in g["titles"].most_common(5) if t] if len(g["titles"]) > 1 else [],
                        g["clients"].most_common(1)[0][0], g["call"]]
                       for g in segs if g["e"] - g["s"] >= 1]
        out["first"] = round(ps[0][0]) if ps else 0
        out["last"] = round(max(p[1] for p in ps)) if ps else 0
    return out


def build(rows: list[tuple], first: date, last: date, cats: Categories, clients: Clients | None = None,
          meetings: list[dict] | None = None, titles: SessionTitles | None = None) -> dict:
    """Split sessions into local days and ISO weeks and add them up. Seconds within a day count from that
    day's local midnight. Meeting time: a stretch while an app had the mic, plus everything inside a booked
    meeting that visibly happened at the Mac (at least 'booked_meeting_evidence_minutes' of a call, the
    meeting tab/app, or Chrome with no readable page during its slot). Returns {days, weeks, span}."""
    clients = clients or Clients({})
    meetings = sorted((dict(m) for m in (meetings or []) if m["e"] > m["s"]), key=lambda m: m["s"])
    for m in meetings:
        m.update(seen=0.0, tracked=0.0, client=clients.of_meeting(m.get("guests", []), m.get("title", "")))
    starts = [m["s"] for m in meetings]
    bounds = sorted({t for m in meetings for t in (m["s"], m["e"])})

    def meeting_at(t: float) -> dict | None:  # the booked meeting running at t; the later start wins
        i = bisect.bisect_right(starts, t) - 1
        while i >= 0 and t - meetings[i]["s"] < 12 * 3600:
            if t < meetings[i]["e"]:
                return meetings[i]
            i -= 1
        return None

    cut = []
    for s, e, app, bundle, host, path, title, call in rows:
        if titles is not None and not host:
            title = titles.expand(title)
        pts = [s, *bounds[bisect.bisect_right(bounds, s):bisect.bisect_left(bounds, e)], e]
        for a, b in zip(pts, pts[1:]):
            if b > a:
                m = meeting_at((a + b) / 2)
                if m is not None and (call or _meeting_hint(app, bundle, host, cats)):
                    m["seen"] += b - a
                cut.append((a, b, app, bundle, host, path, title, call, m))

    pieces: dict[date, list] = defaultdict(list)
    for a, b, app, bundle, host, path, title, call, m in cut:
        cat = cats.of(app, bundle, host)
        in_meeting = cats.meetings is not None and (bool(call) or (m is not None and m["seen"] >= cats.evidence))
        if in_meeting:
            cat = cats.meetings
        if in_meeting and m is not None:
            m["tracked"] += b - a
            client = m["client"]
        else:
            m = None if not in_meeting else m
            client = clients.of(host, path, title)
        d = datetime.fromtimestamp(a).date()
        while d <= last:
            t0, t1 = day_start(d), day_start(d + timedelta(days=1))
            x, y = max(a, t0), min(b, t1)
            if y > x and d >= first:
                pieces[d].append((x - t0, y - t0, app, host, path, title, cat, client, call, m))
            if b <= t1:
                break
            d += timedelta(days=1)

    clock = time.time()

    def kind_of(m: dict) -> str:  # booked (happened at the Mac or running now), upcoming, or missed
        if m["seen"] >= cats.evidence or m["tracked"]:
            return "booked"
        return "upcoming" if m["s"] > clock else ("booked" if m["e"] > clock else "missed")

    def meeting_rows(d: date, ps: list[tuple]) -> list[list]:
        """[day, start, end, name, client, booked secs, meeting secs, kind] for booked meetings starting
        this day and for calls nobody booked (a run of mic time with a gap under 2 minutes)."""
        t0, t1, key = day_start(d), day_start(d + timedelta(days=1)), d.isoformat()
        out = [[key, round(m["s"] - t0), round(m["e"] - t0), m["title"], m["client"], round(m["e"] - m["s"]),
                round(m["tracked"]), kind_of(m)]
               for m in meetings if t0 <= m["s"] < t1]
        runs: list[list] = []
        for a, b, app, host, path, title, cat, client, call, m in ps:
            if not call or m is not None:
                continue
            r = runs[-1] if runs else None
            if r and r[3] == call and a - r[1] <= 120:
                r[1] = max(r[1], b)
                r[2] += b - a
                r[4][client] += b - a
            else:
                runs.append([a, b, b - a, call, Counter({client: b - a})])
        out += [[key, round(a), round(b), f"Call ({call})", c.most_common(1)[0][0], 0, round(secs), "call"]
                for a, b, secs, call, c in runs if secs >= 60]
        return sorted(out, key=lambda r: r[1])

    days, weeks = {}, {}
    d = first
    while d <= last:
        ps = pieces.get(d, [])
        day = {"t0": day_start(d), **summarize(ps, cats, with_segs=True), "meetings": meeting_rows(d, ps)}
        days[d] = day
        w = weeks.setdefault(week_key(d), {"start": (d - timedelta(days=d.weekday())).isoformat(), "days": [],
                                           "pieces": [], "meetings": []})
        w["days"].append(d.isoformat())
        w["pieces"] += ps
        w["meetings"] += day["meetings"]
        d += timedelta(days=1)
    for w in weeks.values():
        w.update(summarize(w.pop("pieces"), cats), active_days=sum(1 for k in w["days"] if days[date.fromisoformat(k)]["total"]))
    allp = [p for ps in pieces.values() for p in ps]
    span = {**summarize(allp, cats), "meetings": [r for v in days.values() for r in v["meetings"]]}
    return {"days": days, "weeks": weeks, "span": span}


def first_day(con: sqlite3.Connection) -> date | None:
    row = con.execute("SELECT MIN(start_ts) FROM sessions").fetchone()
    return datetime.fromtimestamp(row[0]).date() if row and row[0] else None


# ---------------------------------------------------------------- report (terminal)

def fmt_dur(secs: float) -> str:
    m = round(secs / 60)
    if secs < 60:
        return f"{round(secs)}s"
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def hhmm(ts: float) -> tuple[str, str]:
    """(local, DK) clock time."""
    return datetime.fromtimestamp(ts).strftime("%H:%M"), datetime.fromtimestamp(ts, DK).strftime("%H:%M")


def report(con: sqlite3.Connection, cats: Categories, end: date, ndays: int, top: int, pages: bool) -> None:
    start = end - timedelta(days=ndays - 1)
    rows = load_rows(con, day_start(start), day_start(end + timedelta(days=1)))
    built = build(rows, start, end, cats, Clients(cats.client_cfg), load_meetings(), SessionTitles(day_start(start) - 86400))
    days, span = built["days"], built["span"]
    names = cats.names + ["Other"]
    total = span["total"]
    label = end.isoformat() if ndays == 1 else f"{start.isoformat()} to {end.isoformat()}"
    print(f"Activity {label}: {fmt_dur(total)} active  (times: this Mac's clock, DK in brackets)")
    if not total:
        print("Nothing recorded.")
        return
    if ndays > 1:
        print("\nPer day:")
    for d, v in days.items():
        if not v["total"]:
            if ndays > 1:
                print(f"  {d:%a %d %b}  nothing")
            continue
        (f_l, f_dk), (l_l, l_dk) = hhmm(v["t0"] + v["first"]), hhmm(v["t0"] + v["last"])
        meet = f"  meetings {fmt_dur(v['cats'][cats.meetings])}" if cats.meetings is not None and v["cats"][cats.meetings] else ""
        if ndays > 1:
            print(f"  {d:%a %d %b}  {fmt_dur(v['total']):>8}  {f_l}-{l_l} (DK {f_dk}-{l_dk}){meet}")
        else:
            print(f"First activity {f_l}, last {l_l} (DK {f_dk}-{l_dk})")

    print("\nBy category:")
    for i in sorted(range(len(names)), key=lambda i: -span["cats"][i]):
        if span["cats"][i]:
            print(f"  {names[i]:<24}{fmt_dur(span['cats'][i]):>9}  {span['cats'][i] / total:>4.0%}")

    ms = span["meetings"]
    if cats.meetings is not None and ms:
        booked = [r for r in ms if r[7] != "call"]
        print(f"\nMeetings: {fmt_dur(span['cats'][cats.meetings])} in meetings and calls; "
              f"{fmt_dur(sum(r[5] for r in booked))} booked on the calendar ({len(booked)} meetings, "
              f"{sum(1 for r in booked if r[7] == 'missed')} not at this Mac)")
        for key, a, b, title, client, booked_s, secs, kind in ms:
            t0 = day_start(date.fromisoformat(key))
            (al, adk), _ = hhmm(t0 + a), hhmm(t0 + b)
            when = f"{date.fromisoformat(key):%a %d} {al} (DK {adk})"
            b_txt = fmt_dur(booked_s) if booked_s else "-"
            s_txt = fmt_dur(secs) if secs else {"missed": "not at this Mac", "upcoming": "later"}.get(kind, "0m")
            print(f"  {when:<22} {title[:46]:<46} {(client or '-')[:22]:<22} booked {b_txt:>7}  in it {s_txt}")

    print("\nBy client:")
    for name, secs, items in span["clients"][:top]:
        print(f"  {(name or 'No client match'):<34}{fmt_dur(secs):>9}  {secs / total:>4.0%}")
        if pages:
            for what, where, s in items[:5]:
                print(f"      {fmt_dur(s):>8}  {what[:70]}  ({where})")
    print("\nTop sites:")
    for host, secs, cat, pgs in span["sites"][:top]:
        print(f"  {host:<34}{fmt_dur(secs):>9}  {names[cat]}")
        if pages:
            for title, path, s in pgs[:5]:
                print(f"      {fmt_dur(s):>8}  {(title or path)[:90]}")
    print("\nTop apps:")
    for app, secs, wins in span["apps"][:top]:
        print(f"  {app:<34}{fmt_dur(secs):>9}")
        if pages:
            for title, s in wins[:5]:
                print(f"      {fmt_dur(s):>8}  {title[:90]}")


def status(con: sqlite3.Connection) -> None:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    running = re.search(r"\bstate = running", r.stdout) is not None
    pid = re.search(r"\bpid = (\d+)", r.stdout)
    print(f"launchd job: {'running' if running else 'NOT running'}" + (f" (pid {pid.group(1)})" if pid else ""))
    st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    if st:
        age = time.time() - st["last_sample"]
        print(f"last sample: {round(age)}s ago, state {st['state']}; browser tabs: {st['browser']}; "
              f"window titles: {st.get('titles', 'not checked yet')}; microphone: {st.get('mic', 'not checked yet')}"
              + (f"; on a call ({st['call']})" if st.get("call") else ""))
    if not CALENDAR_HELPER.exists():
        print("calendar: not set up on this Mac (meetings come from the microphone only)")
    elif CALENDAR_PATH.exists():
        cal = json.loads(CALENDAR_PATH.read_text())
        print(f"calendar: {len(cal.get('events', []))} meetings, fetched {datetime.fromtimestamp(cal.get('fetched', 0)):%Y-%m-%d %H:%M}")
    else:
        print("calendar: not fetched yet (python3 scripts/activity_tracker.py calendar)")
    print(f"launcher app: {HELPER_APP}" + ("" if HELPER_EXE.exists() else " (not built: run install-helper)"))
    last = con.execute("SELECT MAX(end_ts), COUNT(*) FROM sessions").fetchone()
    if last[0]:
        print(f"last recorded activity: {datetime.fromtimestamp(last[0]):%Y-%m-%d %H:%M:%S}; {last[1]} rows; "
              f"db {DB_PATH.stat().st_size / 1e6:.1f} MB")
    print(f"page: {PAGE_PATH}" + ("" if PAGE_PATH.exists() else " (not built yet)"))


# ---------------------------------------------------------------- page

def page_images() -> tuple[str, str]:
    """RM logo + tab icon, borrowed from the RM Automations page. Never fatal."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from automation_health import page_images as _imgs
        return _imgs()
    except Exception:
        pass
    try:  # the public app installs its logo next to the settings
        import base64
        uri = "data:image/png;base64," + base64.b64encode((APP_HOME / "logo.png").read_bytes()).decode()
        return uri, uri
    except Exception:
        return "", ""


def build_page(con: sqlite3.Connection, cats: Categories) -> Path:
    today = date.today()
    start = max(today - timedelta(days=PAGE_DAYS - 1), first_day(con) or today)
    rows = load_rows(con, day_start(start), time.time() + 60)
    clients = Clients(cats.client_cfg)
    built = build(rows, start, today, cats, clients, load_meetings(), SessionTitles(day_start(start) - 86400))
    days = built["days"]
    data = {
        "generated": time.time(),
        "today": today.isoformat(),
        "categories": cats.names,
        "meetings_cat": cats.meetings,
        "team": clients.team,
        "order": [d.isoformat() for d in days],
        "days": {d.isoformat(): v for d, v in days.items()},
        "worder": sorted(built["weeks"]),
        "weeks": built["weeks"],
    }
    logo, icon = page_images()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (PAGE_HTML.replace("%%DATA%%", payload)
            .replace("%%LOGO%%", f'<img src="{logo}" alt="RM">' if logo else "")
            .replace("%%ICON%%", f'<link rel="icon" href="{icon}">' if icon else "")
            .replace("%%DATADIR%%", str(OUT_DIR).replace(str(Path.home()), "~"))
            .replace("%%SETTINGS%%", str(CATEGORIES_PATH).replace(str(Path.home()), "~")))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PAGE_PATH.with_suffix(".tmp")
    tmp.write_text(page)
    tmp.replace(PAGE_PATH)
    return PAGE_PATH


def build_widget(con: sqlite3.Connection, cats: Categories) -> Path:
    """widget.json for the RM Activity Widget (menu bar + desktop): today, the stretch going on now,
    today's meetings and the next one, top clients and the week so far. Rewritten every 30 s."""
    now = time.time()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    clients = Clients(cats.client_cfg)
    meetings = load_meetings()
    built = build(load_rows(con, day_start(monday), now + 60), monday, today, cats, clients, meetings,
                  SessionTitles(day_start(monday) - 86400))
    day, week = built["days"][today], built["weeks"][week_key(today)]
    t0 = day["t0"]
    try:
        st = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        st = {}
    running = now - st.get("last_sample", 0) < 120
    cur = None
    if day["segs"]:
        s, e, label, cat, title, app, _many, client, call = day["segs"][-1]
        cur = {"label": label, "title": "" if title == label else title, "app": app, "cat": cat, "client": client,
               "call": call, "since": t0 + s, "until": t0 + e}
    active = running and st.get("state") == "active" and cur is not None and now - cur["until"] < 90
    blocks: list[list] = []
    for s, e, _label, cat, *_ in day["segs"]:
        b = blocks[-1] if blocks else None
        if b and b[2] == cat and s - b[1] <= 60:
            b[1] = max(b[1], e)
        else:
            blocks.append([s, e, cat])
    meeting_now = next_meeting = None
    for m in sorted(meetings, key=lambda m: m["s"]):
        if m["s"] <= now < m["e"] and meeting_now is None:
            meeting_now = {"title": m["title"], "s": m["s"], "e": m["e"], "client": clients.of_meeting(m.get("guests", []), m["title"])}
        elif now < m["s"] < now + 18 * 3600 and next_meeting is None:
            next_meeting = {"title": m["title"], "s": m["s"], "e": m["e"], "client": clients.of_meeting(m.get("guests", []), m["title"])}
    named = [c for c in day["clients"] if c[0]]
    week_named = sum(c[1] for c in week["clients"] if c[0])
    meet_i = cats.meetings
    data = {
        "generated": now, "today": today.isoformat(), "t0": t0, "categories": cats.names, "meetings_cat": meet_i,
        "state": "active" if active else ("away" if running else "stopped"),
        "in_call": bool(active and cur and cur["call"]),
        "menubar": fmt_dur(day["total"]) if day["total"] else "0m",
        "now": cur,
        "day": {"total": day["total"], "first": t0 + day["first"], "cats": day["cats"], "blocks": blocks,
                "clients": [[n, secs] for n, secs, _ in named[:6]], "no_client": sum(c[1] for c in day["clients"] if not c[0]),
                "meetings": [[t0 + a, t0 + b, title, client, booked, secs, kind] for _k, a, b, title, client, booked, secs, kind in day["meetings"]]},
        "meeting_now": meeting_now, "next_meeting": next_meeting,
        "week": {"key": week_key(today), "total": week["total"], "meet": week["cats"][meet_i] if meet_i is not None else 0,
                 "client_pct": round(week_named / week["total"] * 100) if week["total"] else 0,
                 "days": [[(monday + timedelta(days=i)).isoformat(),
                           built["days"][monday + timedelta(days=i)]["total"] if monday + timedelta(days=i) <= today else None]
                          for i in range(7)]},
    }
    tmp = WIDGET_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(WIDGET_JSON)
    return WIDGET_JSON


def install_widget() -> None:
    """Build ~/Applications/RM Activity.app from scripts/activity_widget.swift + activity_widget.html and start it
    at login (launchd com.rmmarketing.activity-widget, with --login so it stays in the menu bar; opening the app
    by hand shows the panel). Quitting it from its menu keeps it quit until the next login."""
    exe_name = "RM Activity"
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp) / WIDGET_APP.name
        res = app / "Contents" / "Resources"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        res.mkdir()
        subprocess.run(["xcrun", "swiftc", "-O", "-swift-version", "5", "-o", str(app / "Contents" / "MacOS" / exe_name),
                        str(WIDGET_SRC), "-framework", "AppKit", "-framework", "WebKit"], check=True)
        shutil.copy(ROOT / "scripts" / "activity_widget.html", res / "widget.html")
        for font in ("Poppins-Regular.ttf", "Poppins-Bold.ttf"):  # the panel's font, so it never loads from the web
            shutil.copy(ROOT / "audits" / "themes" / "rm" / "fonts" / font, res / font)
        with open(app / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({
                "CFBundleIdentifier": WIDGET_ID, "CFBundleName": "RM Activity", "CFBundleDisplayName": "RM Activity",
                "CFBundleExecutable": exe_name, "CFBundlePackageType": "APPL", "CFBundleVersion": "3",
                "CFBundleShortVersionString": "2.1", "CFBundleIconFile": "AppIcon",
                "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "13.0",
                "RMDataDir": str(OUT_DIR), "RMPage": str(PAGE_PATH),
            }, f)
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from automation_health import _logo_png
            from activity_icon import icns
            icns(res / "AppIcon.icns")                          # the activity ring (scripts/activity_icon.py)
            (res / "logo.png").write_bytes(_logo_png(96))      # panel header
            (res / "menubar.png").write_bytes(_logo_png(64))   # template image: macOS colours it for the menu bar
        except Exception as e:  # cosmetic: the app falls back to a timer symbol and no header logo
            print(f"no icon/logo: {e}")
        subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", WIDGET_ID, str(app)], check=True)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{WIDGET_LABEL}"], capture_output=True)
        for name in (exe_name, "rm-activity-widget"):
            subprocess.run(["pkill", "-x", name], capture_output=True)
        WIDGET_APP.parent.mkdir(parents=True, exist_ok=True)
        for old in (WIDGET_APP, OLD_WIDGET_APP):
            if old.exists():
                shutil.rmtree(old)
        shutil.copytree(app, WIDGET_APP, symlinks=True)
    subprocess.run(["touch", str(WIDGET_APP)])
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                    "-f", str(WIDGET_APP)], capture_output=True)
    plist = Path.home() / "Library" / "LaunchAgents" / f"{WIDGET_LABEL}.plist"
    with open(plist, "wb") as f:
        plistlib.dump({
            "Label": WIDGET_LABEL,
            "ProgramArguments": [str(WIDGET_APP / "Contents" / "MacOS" / exe_name), "--login"],
            "AssociatedBundleIdentifiers": [WIDGET_ID],  # macOS names the background item "RM Activity", not the binary
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua", "ThrottleInterval": 30,
            "StandardOutPath": str(OUT_DIR / "widget.log"), "StandardErrorPath": str(OUT_DIR / "widget.log"),
        }, f)
    con = connect(DB_PATH)
    build_widget(con, Categories())
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)
    print(f"built {WIDGET_APP}, started {WIDGET_LABEL} (Dock app + menu bar time)")


def install_app() -> None:
    """Kept for old notes: RM Activity.app is the native app now (install_widget)."""
    install_widget()


HELPER_C = r"""// RM Activity Tracker: launchd starts this tiny app so macOS permissions (Accessibility for window
// titles, Automation for Chrome tabs) belong to "RM Activity Tracker" instead of to Python as a whole.
// It runs the tracker as its child, which inherits them, and passes stop signals on.
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
extern char **environ;
static pid_t child = 0;
static void pass_on(int sig) { if (child > 0) kill(child, sig); }
int main(void) {
    char *args[] = {PYTHON, SCRIPT, "run", 0};
    signal(SIGTERM, pass_on); signal(SIGINT, pass_on); signal(SIGHUP, pass_on);
    if (posix_spawn(&child, PYTHON, 0, 0, args, environ) != 0) return 1;
    int status = 0;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""


def install_helper(force: bool = False) -> None:
    """Build ~/Library/Application Support/RM Activity/RM Activity Tracker.app and (re)load the launchd
    job that runs it. Skips the rebuild when the launcher is unchanged: a rebuilt binary is a new app to
    macOS, so Accessibility and the Chrome permission would have to be granted again."""
    py, script = str(ROOT / ".venv" / "bin" / "python3"), str(SELF)
    stamp = hashlib.sha256((HELPER_C + py + script).encode()).hexdigest()
    stamp_file = HELPER_APP / "Contents" / "Resources" / "build.sha256"
    if force or not stamp_file.exists() or stamp_file.read_text() != stamp:
        if HELPER_APP.exists():
            shutil.rmtree(HELPER_APP)
        (HELPER_APP / "Contents" / "MacOS").mkdir(parents=True)
        (HELPER_APP / "Contents" / "Resources").mkdir()
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "launcher.c"
            src.write_text(HELPER_C)
            subprocess.run(["clang", "-O2", "-Wall", f'-DPYTHON="{py}"', f'-DSCRIPT="{script}"',
                            "-o", str(HELPER_EXE), str(src)], check=True)
        with open(HELPER_APP / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({
                "CFBundleIdentifier": HELPER_ID, "CFBundleName": "RM Activity Tracker",
                "CFBundleDisplayName": "RM Activity Tracker", "CFBundleExecutable": HELPER_EXE.name,
                "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
                "CFBundleIconFile": "AppIcon", "LSBackgroundOnly": True, "LSUIElement": True,
                "NSAppleEventsUsageDescription": "Reads the address and title of the active Chrome tab for "
                                                 "RM Activity. Incognito windows are never read.",
            }, f)
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from automation_health import write_icns
            write_icns(HELPER_APP / "Contents" / "Resources" / "AppIcon.icns")
        except Exception as e:  # the icon is cosmetic
            print(f"no icon: {e}")
        stamp_file.write_text(stamp)
        subprocess.run(["codesign", "--force", "--sign", "-", "--identifier", HELPER_ID, str(HELPER_APP)], check=True)
        print(f"built {HELPER_APP} (permissions must be granted to it once)")
    else:
        print(f"{HELPER_APP} unchanged, kept (permissions stay)")
    shutil.copy(PLIST_SRC, PLIST_DST)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    for _ in range(20):  # bootout returns before the old process is gone
        if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode != 0:
            break
        time.sleep(0.5)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_DST)], check=True)
    print(f"loaded {LABEL}")


PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RM Activity</title>%%ICON%%
<style>
:root{--ground:#F5F6FA;--card:#fff;--ink:#192230;--muted:#3E4B63;--faint:#55637E;--line:#D9E0EA;--navy:#192230;--lime:#84CE18;
--track:#EEF1F6;--neutral:#55637E;
--c0:#2a78d6;--c1:#eb6834;--c2:#1baf7a;--c3:#eda100;--c4:#e87ba4;--c5:#008300;--c6:#6250d6;--c7:#e34948;--cx:#a9a8a2}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#10151d;--card:#19202b;--ink:#EEF1F6;--muted:#C2CBD9;--faint:#9AA6B8;--line:#2A3446;
--track:#232c3b;--neutral:#9AA6B8;--c0:#3987e5;--c1:#d95926;--c2:#199e70;--c3:#c98500;--c4:#d55181;--c5:#008300;--c6:#9085e9;--c7:#e66767;--cx:#6b6a65}}
:root[data-theme="dark"]{--ground:#10151d;--card:#19202b;--ink:#EEF1F6;--muted:#C2CBD9;--faint:#9AA6B8;--line:#2A3446;
--track:#232c3b;--neutral:#9AA6B8;--c0:#3987e5;--c1:#d95926;--c2:#199e70;--c3:#c98500;--c4:#d55181;--c5:#008300;--c6:#9085e9;--c7:#e66767;--cx:#6b6a65}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.5 Poppins,-apple-system,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.bar{background:var(--navy);color:#fff}
.bar .in{max-width:1080px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:12px}
.bar img{height:30px;width:auto}
.bar .name{font-weight:600;font-size:16px;letter-spacing:.2px;white-space:nowrap}
.bar .when{margin-left:auto;color:#C2CBD9;font-size:13px;white-space:nowrap}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 56px}
.hero{padding:28px 0 6px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hero h1{margin:0 6px;font-size:26px;font-weight:600;min-width:250px}
.nav{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:10px;height:36px;min-width:36px;padding:0 12px;font:inherit;cursor:pointer}
.nav:disabled{opacity:.35;cursor:default}
.sub{color:var(--faint);font-size:13px;margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.tile .k{color:var(--faint);font-size:13px}
.tile .v{font-size:26px;font-weight:600;line-height:1.25;margin-top:2px}
.tile .s{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 22px;margin-bottom:12px}
.card h2{font-size:16px;margin:0 0 12px;font-weight:600}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid2 .card{margin-bottom:0}.grid2{margin-bottom:12px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted);margin-bottom:12px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}
.band{position:relative;height:38px;background:var(--track);border-radius:6px;overflow:hidden}
.blk{position:absolute;top:0;bottom:0;border-radius:3px;cursor:default}
.axis{position:relative;height:20px;margin-top:4px;font-size:11px;color:var(--faint)}
.tick{position:absolute;transform:translateX(-50%);white-space:nowrap}
.tick:first-child{transform:none}
.cols{display:flex;gap:6px;align-items:flex-end;margin-top:18px;padding-top:4px;border-top:1px dashed var(--line);position:relative}
.cols .ref{position:absolute;top:-9px;right:0;font-size:11px;color:var(--faint);background:var(--card);padding-left:6px}
.col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0}
.stack{height:120px;width:100%;max-width:34px;display:flex;flex-direction:column-reverse;gap:2px}
.seg{width:100%;border-radius:3px;min-height:2px}
.col .lbl{font-size:11px;color:var(--faint)}
.row{display:grid;grid-template-columns:minmax(120px,190px) 1fr 70px 40px;gap:10px;align-items:center;padding:5px 0;font-size:14px}
.row .nm{display:flex;align-items:center;gap:8px;min-width:0}
.row .nm span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trk{height:10px;background:var(--track);border-radius:4px}
.trk .b{height:100%;border-radius:4px;min-width:2px}
.val{text-align:right;font-variant-numeric:tabular-nums}
.pct{text-align:right;color:var(--faint);font-size:13px;font-variant-numeric:tabular-nums}
details.site>summary{list-style:none;cursor:pointer}
details.site>summary::-webkit-details-marker{display:none}
details.site>summary .nm::before{content:"\203A";color:var(--faint);width:10px;display:inline-block;transition:transform .15s}
details.site[open]>summary .nm::before{transform:rotate(90deg)}
details.noexp>summary{cursor:default}details.noexp>summary .nm::before{visibility:hidden}
.pages{margin:2px 0 10px 18px;border-left:2px solid var(--line);padding-left:12px}
.pg{display:grid;grid-template-columns:1fr 70px;gap:10px;font-size:13px;padding:3px 0}
.pg .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pg .p{color:var(--faint)}
.trend{display:flex;gap:8px;align-items:flex-end}
.tday{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;min-width:0;border-radius:8px;padding:4px 0}
.tday:hover{background:var(--track)}
.tday.sel{outline:2px solid var(--lime);outline-offset:-2px}
.tday .tot{font-size:11px;color:var(--muted);white-space:nowrap}
.tday .stack{height:150px;max-width:30px}
.tday .lbl{font-size:11px;color:var(--faint);text-align:center;line-height:1.2}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--faint);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td.n{white-space:nowrap;font-variant-numeric:tabular-nums}
td.t{color:var(--muted);max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.logbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-size:13px;color:var(--muted)}
.empty{color:var(--faint);margin:6px 0}
.foot{color:var(--faint);font-size:12.5px;margin-top:22px;line-height:1.6}
.modes{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card);margin-right:6px}
.modes button{border:0;background:none;color:var(--ink);font:inherit;height:34px;padding:0 14px;cursor:pointer}
.modes button.on{background:var(--lime);color:#192230;font-weight:600}
.row.dim .nm span:last-child{color:var(--faint);font-style:italic}
.note{color:var(--muted);font-size:13px;margin:-4px 0 10px}
td.faint{color:var(--faint)}
#meetings td.t{max-width:300px}
.tip{position:fixed;z-index:9;pointer-events:none;background:var(--navy);color:#fff;font-size:12.5px;line-height:1.5;padding:8px 11px;border-radius:8px;white-space:pre-line;max-width:420px;box-shadow:0 4px 18px rgba(0,0,0,.25)}
@media (max-width:760px){.tiles{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}.wrap,.bar .in{padding-left:16px;padding-right:16px}
.row{grid-template-columns:minmax(90px,130px) 1fr 64px}.row .pct{display:none}.hero h1{min-width:0;font-size:21px}td.t{max-width:160px}.bar .when{display:none}
.card{padding:16px}.cols{gap:2px}.trend{gap:2px}.tday .tot{display:none}.tday .lbl,.col .lbl{font-size:10px}}
</style></head>
<body>
<header class="bar"><div class="in">%%LOGO%%<span class="name">RM Activity</span><span class="when" id="when"></span></div></header>
<main class="wrap">
  <section class="hero">
    <div class="modes" id="modes"><button data-m="day">Day</button><button data-m="week">Week</button></div>
    <button class="nav" id="prev" aria-label="Previous">&#8249;</button>
    <h1 id="title"></h1>
    <button class="nav" id="next" aria-label="Next">&#8250;</button>
    <button class="nav" id="today">Today</button>
  </section>
  <p class="sub">Times are this Mac's clock; hover anything for DK time. Arrow keys move between days or weeks, D and W switch view.</p>
  <section class="tiles" id="tiles"></section>
  <section class="card" id="daycard"><h2>Your day</h2><div class="legend" id="legend"></div><div id="timeline"></div><div id="hours"></div></section>
  <section class="card" id="weekcard"><h2>Per day <span class="pct">(click a day to open it)</span></h2><div class="legend" id="wlegend"></div><div id="weekdays"></div></section>
  <div class="grid2">
    <section class="card"><h2>By category</h2><div id="cats"></div></section>
    <section class="card"><h2>By client <span class="pct">(click a client for what it was)</span></h2><div id="clients"></div></section>
  </div>
  <section class="card"><h2>Meetings and calls</h2><p class="note" id="meetnote"></p><div style="overflow-x:auto"><table id="meetings"></table></div></section>
  <div class="grid2">
    <section class="card"><h2>Apps <span class="pct">(click an app for its windows)</span></h2><div id="apps"></div></section>
    <section class="card"><h2>Sites <span class="pct">(click a site for its pages)</span></h2><div id="sites"></div></section>
  </div>
  <section class="card"><h2 id="trendtitle"></h2><div id="trend"></div></section>
  <section class="card" id="logcard"><h2>Log</h2>
    <div class="logbar"><span id="logcount"></span><label><input type="checkbox" id="short"> Show stretches under 1 minute</label></div>
    <div style="overflow-x:auto"><table id="log"></table></div></section>
  <p class="foot">Meetings and calls: while an app records from the microphone (Meet in Chrome, Zoom, WhatsApp, a phone call through the Mac) the time counts as a meeting whatever window is in front, and listening without touching the keyboard still counts. Dictation and screen recording are not calls. Booked meetings come from your calendar (events with at least one other guest); before 9 November there was no microphone data, so a booked meeting counts when its slot shows the meeting tab, a meeting app or Meet's picture-in-picture window for 2 minutes or more.
  Clients: a meeting goes by its guests' mail addresses, anything else by a client's name or domain in the page title, address or window title (your client list in the settings file). Claude Code sessions show their full name, not the shortened tab name.
  Incognito windows are never recorded. Safari counts as app time only, because a private Safari window cannot be told apart from a normal one.
  The clock stops after 3 minutes without keyboard or mouse (90 minutes in meetings, calls and video), and when the screen locks or the Mac sleeps.
  Data stays on this Mac, in %%DATADIR%%. Categories, calls and clients: %%SETTINGS%%. Terminal summary: activity_tracker.py report --week</p>
</main>
<div class="tip" id="tip" hidden></div>
<script id="data" type="application/json">%%DATA%%</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const CATS = D.categories.concat(['Other']);
const MEET = D.meetings_cat;
const color = i => i < D.categories.length ? `var(--c${i})` : 'var(--cx)';
const pad = n => String(n).padStart(2, '0');
const DKF = new Intl.DateTimeFormat('en-GB', {timeZone: 'Europe/Copenhagen', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
const at = (day, s) => new Date((day.t0 + s) * 1000);
const tl = (day, s) => { const d = at(day, s); return pad(d.getHours()) + ':' + pad(d.getMinutes()); };
const tdk = (day, s) => DKF.format(at(day, s));
const range = (day, a, b) => `${tl(day, a)}-${tl(day, b)}  (DK ${tdk(day, a)}-${tdk(day, b)})`;
function dur(sec) {
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const m = Math.round(sec / 60); if (m < 60) return m + 'm';
  const h = Math.floor(m / 60), r = m % 60; return r ? `${h}h ${r}m` : `${h}h`;
}
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function sw(i) { const s = el('span', 'sw'); s.style.background = color(i); return s; }
const dayName = key => new Date(key + 'T12:00:00').toLocaleDateString('en-GB', {weekday: 'long', day: 'numeric', month: 'long'});
const shortDay = key => new Date(key + 'T12:00:00').toLocaleDateString('en-GB', {weekday: 'short', day: 'numeric', month: 'short'});
const clientName = c => c || 'No client match';

let mode = 'day', cur = -1, wcur = -1;
const h0 = location.hash.slice(1);
if (h0.startsWith('w')) { mode = 'week'; wcur = D.worder.indexOf(h0.slice(1)); } else cur = D.order.indexOf(h0);
if (cur < 0) cur = D.order.indexOf(D.today);
if (cur < 0) cur = D.order.length - 1;
if (wcur < 0) wcur = Math.max(0, D.worder.findIndex(k => D.weeks[k].days.includes(D.order[cur])));
const weekOf = key => D.worder.findIndex(k => D.weeks[k].days.includes(key));

function hourSpan(day) {
  const h0 = Math.floor(day.first / 3600), h1 = Math.max(h0 + 1, Math.min(24, Math.ceil(day.last / 3600)));
  return [h0, h1];
}

function topCat(obj) { let top = -1; obj.cats.forEach((v, i) => { if (v && (top < 0 || v > obj.cats[top])) top = i; }); return top; }

function tiles(obj, isDay, key) {
  const box = document.getElementById('tiles'); box.replaceChildren();
  const top = topCat(obj), meet = MEET != null ? obj.cats[MEET] : 0;
  const booked = obj.meetings.filter(m => m[7] !== 'call'), bookedSecs = booked.reduce((p, m) => p + m[5], 0);
  const items = [['Active time', obj.total ? dur(obj.total) : '0m', isDay ? (obj.total ? `${obj.segs.length} stretches` : 'Nothing recorded')
                  : (obj.active_days ? `${dur(obj.total / obj.active_days)} per active day` : 'Nothing recorded')]];
  if (isDay) items.push(['First and last', obj.total ? `${tl(obj, obj.first)}-${tl(obj, obj.last)}` : '-', obj.total ? `DK ${tdk(obj, obj.first)}-${tdk(obj, obj.last)}` : '']);
  else items.push(['Active days', `${obj.active_days} of ${obj.days.length}`, obj.days.length < 7 ? 'week so far' : '']);
  items.push(['In meetings and calls', dur(meet), booked.length ? `${dur(bookedSecs)} booked in ${booked.length} meeting${booked.length > 1 ? 's' : ''}` : 'nothing booked']);
  items.push(['Most time on', top >= 0 ? CATS[top] : '-', top >= 0 ? `${dur(obj.cats[top])}, ${Math.round(obj.cats[top] / obj.total * 100)}%` : '']);
  for (const [k, v, s] of items) {
    const t = el('div', 'tile'); t.append(el('div', 'k', k));
    const vv = el('div', 'v', v); if (k === 'Most time on') vv.style.fontSize = '20px';
    t.append(vv, el('div', 's', s)); box.append(t);
  }
}

function legend(obj, id) {
  const box = document.getElementById(id); box.replaceChildren();
  obj.cats.forEach((v, i) => { if (!v) return; const s = el('span'); s.append(sw(i), document.createTextNode(CATS[i])); box.append(s); });
}

function timeline(day) {
  const box = document.getElementById('timeline'); box.replaceChildren();
  if (!day.total) { box.append(el('p', 'empty', 'Nothing recorded this day.')); return; }
  const [h0, h1] = hourSpan(day), span = (h1 - h0) * 3600, x = s => (s - h0 * 3600) / span * 100;
  const blocks = [];
  for (const [s, e, label, cat, , , , client] of day.segs) {
    const b = blocks[blocks.length - 1];
    if (b && b.cat === cat && s - b.e <= 60) { b.e = Math.max(b.e, e); b.on += e - s; b.parts[label] = (b.parts[label] || 0) + e - s; if (client) b.cl[client] = (b.cl[client] || 0) + e - s; }
    else blocks.push({s, e, cat, on: e - s, parts: {[label]: e - s}, cl: client ? {[client]: e - s} : {}});
  }
  const band = el('div', 'band');
  for (const b of blocks) {
    const d = el('div', 'blk');
    const w = x(b.e) - x(b.s);
    d.style.left = x(b.s) + '%';
    d.style.width = `max(1px, calc(${w}% - 2px))`;
    d.style.background = color(b.cat);
    const parts = Object.entries(b.parts).sort((p, q) => q[1] - p[1]).slice(0, 4).map(([l, v]) => `${l}  ${dur(v)}`).join('\n');
    const cl = Object.entries(b.cl).sort((p, q) => q[1] - p[1]).slice(0, 2).map(([l]) => l).join(', ');
    d.dataset.tip = `${range(day, b.s, b.e)}\n${CATS[b.cat]}  ${dur(b.on)}${cl ? '\nClient: ' + cl : ''}\n${parts}`;
    band.append(d);
  }
  const hrs = h1 - h0, perLabel = box.clientWidth / hrs;  // keep hour labels ~50px apart
  const axis = el('div', 'axis'), step = [1, 2, 3, 4, 6].find(s => perLabel * s >= 50) || 6;
  for (let h = h0; h <= h1; h += step) { const t = el('span', 'tick', pad(h % 24) + ':00'); t.style.left = x(h * 3600) + '%'; if (h === h1 && h > h0) t.style.transform = 'translateX(-100%)'; axis.append(t); }
  box.append(band, axis);
}

function hours(day) {
  const box = document.getElementById('hours'); box.replaceChildren();
  if (!day.total) return;
  const [h0, h1] = hourSpan(day);
  const per = Array.from({length: h1 - h0}, () => new Array(CATS.length).fill(0));
  for (const [s, e, , cat] of day.segs) {
    let a = s;
    while (a < e) { const h = Math.floor(a / 3600), b = Math.min(e, (h + 1) * 3600); if (h >= h0 && h < h1) per[h - h0][cat] += b - a; a = b; }
  }
  const wrap = el('div', 'cols'); wrap.append(el('span', 'ref', '60 min'));
  per.forEach((row, i) => {
    const col = el('div', 'col'), stack = el('div', 'stack');
    const h = h0 + i, label = `${pad(h % 24)}:00-${pad((h + 1) % 24)}:00`;
    row.forEach((v, c) => {
      if (v < 20) return;
      const seg = el('div', 'seg'); seg.style.height = `calc(${v / 3600 * 100}% - 2px)`; seg.style.background = color(c);
      seg.dataset.tip = `${label}  (DK ${tdk(day, h * 3600)})\n${CATS[c]}  ${dur(v)}\nWhole hour: ${dur(row.reduce((p, q) => p + q, 0))}`;
      stack.append(seg);
    });
    const every = box.clientWidth / per.length < 26 ? 3 : 1;
    col.append(stack, el('span', 'lbl', i % every ? ' ' : pad(h % 24)));
    wrap.append(col);
  });
  box.append(wrap);
}

function barRow(name, secs, max, total, cat, tip) {
  const row = el('div', 'row'), nm = el('div', 'nm');
  if (cat != null) nm.append(sw(cat));
  nm.append(el('span', '', name));
  const trk = el('div', 'trk'), b = el('div', 'b');
  b.style.width = (secs / max * 100) + '%'; b.style.background = cat != null ? color(cat) : 'var(--neutral)';
  trk.append(b);
  row.append(nm, trk, el('div', 'val', dur(secs)), el('div', 'pct', total ? Math.round(secs / total * 100) + '%' : ''));
  if (tip) row.dataset.tip = tip;
  return row;
}

function cats(obj) {
  const box = document.getElementById('cats'); box.replaceChildren();
  if (!obj.total) { box.append(el('p', 'empty', 'Nothing recorded.')); return; }
  const idx = obj.cats.map((v, i) => i).filter(i => obj.cats[i]).sort((a, b) => obj.cats[b] - obj.cats[a]);
  const max = obj.cats[idx[0]];
  for (const i of idx) box.append(barRow(CATS[i], obj.cats[i], max, obj.total, i, `${CATS[i]}: ${dur(obj.cats[i])}`));
}

// A bar row that opens to a list of [main text, small text, seconds, tooltip] lines.
function expandable(row, items) {
  const det = el('details', 'site' + (items.length ? '' : ' noexp')), sum = el('summary');
  sum.append(row); det.append(sum);
  if (!items.length) { sum.addEventListener('click', e => e.preventDefault()); return det; }
  const list = el('div', 'pages');
  for (const [main, sub, s, tip] of items) {
    const pg = el('div', 'pg'), t = el('div', 't');
    t.append(el('span', '', main), el('span', 'p', sub ? '  ' + sub : ''));
    pg.dataset.tip = tip;
    pg.append(t, el('div', 'val', dur(s)));
    list.append(pg);
  }
  det.append(list);
  return det;
}

function clients(obj) {
  const box = document.getElementById('clients'); box.replaceChildren();
  if (!obj.clients.length) { box.append(el('p', 'empty', 'Nothing recorded.')); return; }
  const named = obj.clients.filter(c => c[0]), none = obj.clients.find(c => !c[0]);
  const items = list => list.map(([w, where, s]) => [w, where, s, `${w}\n${where}  ${dur(s)}`]);
  if (named.length) {
    const secs = named.reduce((p, c) => p + c[1], 0);
    box.append(el('p', 'note', `${dur(secs)} (${Math.round(secs / obj.total * 100)}%) on ${named.length} client${named.length > 1 ? 's' : ''} or lead${named.length > 1 ? 's' : ''}, ${dur(obj.total - secs)} on no client.`));
  }
  const max = named.length ? named[0][1] : 1;
  for (const [name, secs, list] of named.slice(0, 20))
    box.append(expandable(barRow(name, secs, max, obj.total, null, `${name}: ${dur(secs)}`), items(list)));
  if (named.length > 20) box.append(el('p', 'empty', `+ ${named.length - 20} more, ${dur(named.slice(20).reduce((p, c) => p + c[1], 0))} together`));
  if (none) {
    const row = barRow(clientName(''), none[1], none[1], obj.total, null, `No client match: ${dur(none[1])} (internal work, admin, personal)`);
    row.classList.add('dim');
    box.append(expandable(row, items(none[2])));
  }
}

function meetings(obj, isDay) {
  const note = document.getElementById('meetnote'), table = document.getElementById('meetings');
  table.replaceChildren();
  const ms = obj.meetings, booked = ms.filter(m => m[7] !== 'call'), missed = booked.filter(m => m[7] === 'missed').length;
  const meet = MEET != null ? obj.cats[MEET] : 0;
  note.textContent = ms.length || meet
    ? `${dur(meet)} in meetings and calls. ${booked.length} booked meeting${booked.length === 1 ? '' : 's'} (${dur(booked.reduce((p, m) => p + m[5], 0))})` +
      (missed ? `, ${missed} not at this Mac (declined, moved, on the phone or did not happen).` : '.')
    : 'No meetings or calls.';
  if (!ms.length) return;
  const head = el('tr');
  for (const h of (isDay ? [] : ['Day']).concat(['Time', 'DK', 'Meeting', 'Client', 'Booked', 'In it'])) head.append(el('th', '', h));
  const thead = el('thead'); thead.append(head);
  const tbody = el('tbody');
  for (const [key, s, e, title, client, bookedS, secs, kind] of ms) {
    const day = D.days[key], tr = el('tr');
    if (!isDay) tr.append(el('td', 'n', shortDay(key)));
    const t = el('td', 't', title); t.dataset.tip = title;
    tr.append(el('td', 'n', `${tl(day, s)}-${tl(day, e)}`), el('td', 'n', `${tdk(day, s)}-${tdk(day, e)}`), t,
      el('td', 'n', client || '-'), el('td', 'n', bookedS ? dur(bookedS) : '-'),
      el('td', 'n' + (kind === 'missed' || kind === 'upcoming' ? ' faint' : ''), kind === 'missed' ? 'not at Mac' : kind === 'upcoming' ? 'later' : dur(secs)));
    tbody.append(tr);
  }
  table.append(thead, tbody);
}

function apps(obj) {
  const box = document.getElementById('apps'); box.replaceChildren();
  if (!obj.apps.length) { box.append(el('p', 'empty', 'Nothing recorded.')); return; }
  const max = obj.apps[0][1];
  for (const [app, secs, wins] of obj.apps)
    box.append(expandable(barRow(app, secs, max, obj.total, null, `${app}: ${dur(secs)}`),
      wins.map(([t, s]) => [t, '', s, `${t}\n${app}  ${dur(s)}`])));
}

function sites(obj) {
  const box = document.getElementById('sites'); box.replaceChildren();
  if (!obj.sites.length) { box.append(el('p', 'empty', 'No web pages recorded.')); return; }
  const max = obj.sites[0][1];
  for (const [host, secs, cat, pages] of obj.sites)
    box.append(expandable(barRow(host, secs, max, obj.total, cat, `${host}: ${dur(secs)}  (${CATS[cat]})`),
      pages.map(([title, path, s]) => [title || path, title ? path : '', s, `${title || '(no title)'}\n${host}${path}\n${dur(s)}`])));
}

// Stacked columns: [{top, l1, l2, cats, total, sel, tipName, click}]
function columns(box, items, scale) {
  box.replaceChildren();
  const max = Math.max(scale, ...items.map(i => i.total));
  const wrap = el('div', 'trend');
  for (const it of items) {
    const col = el('div', 'tday' + (it.sel ? ' sel' : '')), stack = el('div', 'stack');
    it.cats.forEach((v, c) => {
      if (v < 60) return;
      const seg = el('div', 'seg'); seg.style.height = `calc(${v / max * 100}% - 2px)`; seg.style.background = color(c);
      seg.dataset.tip = `${it.tipName}\n${CATS[c]}  ${dur(v)}\nTotal: ${dur(it.total)}`;
      stack.append(seg);
    });
    const lbl = el('div', 'lbl'); lbl.append(document.createTextNode(it.l1), el('br'), document.createTextNode(it.l2));
    col.append(el('div', 'tot', it.total ? dur(it.total) : ''), stack, lbl);
    col.addEventListener('click', it.click);
    wrap.append(col);
  }
  box.append(wrap);
}

const dayItem = (k, sel) => { const day = D.days[k], dt = new Date(k + 'T12:00:00');
  return {total: day.total, cats: day.cats, sel, tipName: dayName(k), l1: dt.toLocaleDateString('en-GB', {weekday: 'short'}), l2: String(dt.getDate()),
          click: () => { mode = 'day'; go(D.order.indexOf(k)); }}; };

function trend() {
  const box = document.getElementById('trend');
  if (mode === 'day') {
    document.getElementById('trendtitle').replaceChildren(document.createTextNode('Last 14 days '), el('span', 'pct', '(click a day to open it)'));
    let start = Math.max(0, D.order.length - 14); if (cur < start) start = cur;
    columns(box, D.order.slice(start, start + 14).map((k, j) => dayItem(k, start + j === cur)), 3600);
  } else {
    document.getElementById('trendtitle').replaceChildren(document.createTextNode('Weeks '), el('span', 'pct', '(click a week to open it)'));
    columns(box, D.worder.map((k, j) => { const w = D.weeks[k];
      return {total: w.total, cats: w.cats, sel: j === wcur, tipName: weekName(k), l1: 'W' + k.split('-W')[1], l2: shortDay(w.days[0]).replace(/^\w+ /, ''),
              click: () => goWeek(j)}; }), 3600 * 10);
  }
}

function weekName(k) { const w = D.weeks[k]; return `Week ${+k.split('-W')[1]}, ${shortDay(w.days[0])} to ${shortDay(w.days[w.days.length - 1])}`; }

function logTable(day) {
  const table = document.getElementById('log'); table.replaceChildren();
  const all = document.getElementById('short').checked;
  const rows = day.segs.filter(g => all || g[1] - g[0] >= 60);
  document.getElementById('logcount').textContent = day.segs.length
    ? `${rows.length} of ${day.segs.length} stretches` + (all ? '' : ' (1 minute or longer)') : '';
  if (!rows.length) return;
  const head = el('tr'); for (const h of ['Time', 'DK', 'Length', 'Site or app', 'Page or window', 'Client', 'Category']) head.append(el('th', '', h));
  const thead = el('thead'); thead.append(head);
  const tbody = el('tbody');
  for (const [s, e, label, cat, title, app, many, client, call] of rows) {
    const tr = el('tr');
    const c = el('td', 'n'); c.append(sw(cat), document.createTextNode(' ' + CATS[cat] + (call ? ` (call: ${call})` : '')));
    const t = el('td', 't', title + (many.length > 1 ? `  (+${many.length - 1} more)` : ''));
    if (title) t.dataset.tip = many.length ? many.map(([x, v]) => `${x}  ${dur(v)}`).join('\n') : title;
    tr.append(el('td', 'n', `${tl(day, s)}-${tl(day, e)}`), el('td', 'n', `${tdk(day, s)}-${tdk(day, e)}`),
      el('td', 'n', dur(e - s)), el('td', '', label === app ? app : label), t, el('td', 'n', client || ''), c);
    tbody.append(tr);
  }
  table.append(thead, tbody);
}

function go(i) {
  if (i < 0 || i >= D.order.length) return;
  cur = i; wcur = Math.max(0, weekOf(D.order[cur])); render();
}
function goWeek(j) {
  if (j < 0 || j >= D.worder.length) return;
  mode = 'week'; wcur = j; render();
}
function step(n) { if (mode === 'day') go(cur + n); else goWeek(wcur + n); }

function render() {
  const isDay = mode === 'day';
  for (const b of document.querySelectorAll('#modes button')) b.classList.toggle('on', b.dataset.m === mode);
  document.getElementById('daycard').hidden = !isDay;
  document.getElementById('logcard').hidden = !isDay;
  document.getElementById('weekcard').hidden = isDay;
  let obj;
  if (isDay) {
    const key = D.order[cur]; obj = D.days[key];
    history.replaceState(null, '', '#' + key);
    document.getElementById('title').textContent = key === D.today ? 'Today, ' + dayName(key) : dayName(key);
    document.getElementById('prev').disabled = cur === 0;
    document.getElementById('next').disabled = cur === D.order.length - 1;
    legend(obj, 'legend'); timeline(obj); hours(obj); logTable(obj);
  } else {
    const key = D.worder[wcur]; obj = D.weeks[key];
    history.replaceState(null, '', '#w' + key);
    document.getElementById('title').textContent = weekName(key);
    document.getElementById('prev').disabled = wcur === 0;
    document.getElementById('next').disabled = wcur === D.worder.length - 1;
    legend(obj, 'wlegend');
    columns(document.getElementById('weekdays'), obj.days.map(k => dayItem(k, false)), 3600 * 4);
  }
  tiles(obj, isDay, isDay ? D.order[cur] : D.worder[wcur]); cats(obj); clients(obj); meetings(obj, isDay); apps(obj); sites(obj); trend();
}

const gen = new Date(D.generated * 1000);
document.getElementById('when').textContent = `Updated ${pad(gen.getHours())}:${pad(gen.getMinutes())} (DK ${DKF.format(gen)})`;
document.getElementById('prev').onclick = () => step(-1);
document.getElementById('next').onclick = () => step(1);
document.getElementById('today').onclick = () => { if (mode === 'day') go(D.order.length - 1); else goWeek(D.worder.length - 1); };
for (const b of document.querySelectorAll('#modes button')) b.onclick = () => { mode = b.dataset.m; render(); };
document.getElementById('short').onchange = () => logTable(D.days[D.order[cur]]);
let resizeTimer; addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });
document.addEventListener('keydown', e => {
  if (e.target.closest && e.target.closest('input')) return;
  if (e.key === 'ArrowLeft') step(-1); if (e.key === 'ArrowRight') step(1);
  if (e.key === 'd' || e.key === 'D') { mode = 'day'; render(); } if (e.key === 'w' || e.key === 'W') { mode = 'week'; render(); }
});
const tip = document.getElementById('tip');
document.addEventListener('mousemove', ev => {
  const t = ev.target.closest && ev.target.closest('[data-tip]');
  if (!t) { tip.hidden = true; return; }
  tip.textContent = t.dataset.tip; tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
// The tracker rebuilds this file every 5 minutes; reload while looking at today or this week.
setTimeout(() => { if ((mode === 'day' && D.order[cur] === D.today) || (mode === 'week' && wcur === D.worder.length - 1)) location.reload(); }, 300000);
render();
</script>
</body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="RM Activity: time per app and web page, per day.")
    ap.add_argument("--db", type=Path, default=None, help="use another database (testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="the tracking loop (launchd)")
    r = sub.add_parser("report", help="terminal summary")
    r.add_argument("--date", default=None, help="YYYY-MM-DD, 'today' or 'yesterday' (default today)")
    r.add_argument("--days", type=int, default=1, help="number of days ending at --date")
    r.add_argument("--top", type=int, default=15)
    r.add_argument("--pages", action="store_true", help="list the top pages under each site, client and app")
    r.add_argument("--week", action="store_true", help="the whole week (Monday-Sunday) that --date falls in")
    p = sub.add_parser("page", help="rebuild the HTML page")
    p.add_argument("--open", action="store_true")
    sub.add_parser("status")
    sub.add_parser("calendar", help="copy the calendar's meetings into data/activity_tracker/calendar.json")
    sub.add_parser("install-app")
    sub.add_parser("install-widget", help="build + start RM Activity Widget (menu bar and desktop widget)")
    sub.add_parser("widget-data", help="write data/activity_tracker/widget.json now")
    h = sub.add_parser("install-helper", help="build the launcher app and (re)load the launchd job")
    h.add_argument("--force", action="store_true", help="rebuild even if unchanged (resets permissions)")
    args = ap.parse_args()

    global DB_PATH, PAGE_PATH
    if args.db:
        DB_PATH = args.db
        PAGE_PATH = args.db.with_suffix(".html")

    if args.cmd == "run":
        run()
    elif args.cmd == "install-app":
        install_app()
    elif args.cmd == "install-helper":
        install_helper(args.force)
    elif args.cmd == "install-widget":
        install_widget()
    else:
        con = connect(DB_PATH)
        cats = Categories()
        if args.cmd == "report":
            raw = (args.date or "today").lower()
            end = date.today() - timedelta(days=1) if raw == "yesterday" else date.today() if raw == "today" else date.fromisoformat(raw)
            ndays = max(1, args.days)
            if args.week:
                monday = end - timedelta(days=end.weekday())
                end = min(monday + timedelta(days=6), date.today())
                ndays = (end - monday).days + 1
            report(con, cats, end, ndays, args.top, args.pages)
        elif args.cmd == "page":
            path = build_page(con, cats)
            print(path)
            if args.open:
                subprocess.run(["open", str(path)])
        elif args.cmd == "status":
            status(con)
        elif args.cmd == "widget-data":
            print(build_widget(con, cats))
        elif args.cmd == "calendar":
            try:
                log(f"calendar: {fetch_calendar(con)} meetings saved")
            except Exception as e:  # never fatal: meetings just stay unnamed until the next try
                log(f"calendar fetch failed: {type(e).__name__}: {e}")
                sys.exit(1)


if __name__ == "__main__":
    main()
