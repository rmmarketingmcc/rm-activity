# RM Activity

See where your day actually goes. A free Mac app that notes which app, web page and window you work in
every five seconds, and shows your day: time per category and per client, real meeting time (counted from
the microphone), and the week. Everything stays on your Mac.

**Install:** `curl -fsSL https://rmmarketingmcc.github.io/rm-activity/install.sh | bash`
(macOS 13+, Apple's Command Line Tools). Page and live demo: https://rmmarketingmcc.github.io/rm-activity/

## How it works

| Part | File | What it does |
|---|---|---|
| Tracker | `tracker/activity_tracker.py` | Every 5 s: front app, Chrome tab (host, path, title; never incognito, never query strings), window title (Accessibility), which app uses the mic. SQLite in `~/Library/Application Support/RM Activity/data/`. Builds the full page (`index.html`) every 5 min and `widget.json` every 30 s. Python standard library only. |
| Launcher | `app/launcher.c` | launchd starts it inside the app, so macOS permissions belong to "RM Activity", never to Python as a whole. |
| App | `app/ActivityApp.swift`, `app/widget.html` | Menu bar time, the panel, the desktop widget. Reads `widget.json`, records nothing. |
| Settings | `tracker/settings.example.json` | Categories, call apps, clients. Installed once as `settings.json`; never overwritten by updates. |

Useful commands (inside the installed app):

```
~/Applications/RM\ Activity.app/Contents/MacOS/rm-activity-tracker report     # today in Terminal
~/Applications/RM\ Activity.app/Contents/MacOS/rm-activity-tracker status     # running? permissions?
```

## Develop

- `./build.sh` builds `build/RM Activity.app` (universal) and `build/RM-Activity.zip`.
- The tracker, app and panel come from RM Marketing's own setup (one codebase): `./sync.sh` copies them in and
  runs `tools/scan_clients.py`, which refuses when a client name, mail address or domain would come along.
- `./release.sh 1.1 "What changed"` builds, tags, pushes and publishes the GitHub release the installer downloads.

MIT licensed. Make it your own.
