#!/usr/bin/env python3
"""Refuses (exit 1) when a file in this repo names an RM client or lead, their mail address or domain, or an RM
team address. The names come from RM's private client registry at run time, so this file itself names nobody.
Run by sync.sh and release.sh; only works on a Mac that has the RM repo (~/google-ads-mcp)."""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RM = Path.home() / "google-ads-mcp"
GENERIC = {"google", "design", "digital", "online", "marketing", "studio", "group", "shine", "nspire", "consulting",
           "lead", "leads", "audit", "partner", "home", "media", "contact"}
MAIL_PROVIDERS = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.dk", "outlook.com", "outlook.dk", "live.com", "live.dk",
                  "icloud.com", "me.com", "yahoo.com", "yahoo.dk", "mail.dk", "jubii.dk", "protonmail.com"}


def needles() -> set:
    reg = json.loads((RM / "data" / "client_email_registry.json").read_text())
    out = set()
    for c in reg.get("all_clients", []):
        name = re.sub(r"\b(ApS|A/S|I/S|IVS)\b", " ", str(c.get("client_name", "")).strip('"'))
        for seg in re.split(r"\s*[()/,+]\s*|\s+-\s+", name):
            seg = re.sub(r"\s+", " ", seg).strip(" .-\"").lower()
            if len(seg) >= 5 and seg not in GENERIC:
                out.add(seg)
        out |= {e.lower() for e in c.get("contact_emails", [])}
        out |= {d.lower() for d in c.get("domains", []) if d.lower() not in MAIL_PROVIDERS}
    cfg = json.loads((RM / "scripts" / "activity_categories.json").read_text()).get("clients", {})
    out |= {e.lower() for e in cfg.get("team_emails", []) + cfg.get("self_emails", []) if "rmmarketing" not in e}
    return out


def main() -> int:
    if not (RM / "data" / "client_email_registry.json").exists():
        print("scan: RM client registry not on this Mac, cannot check")
        return 1
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                           capture_output=True, text=True).stdout.split("\n")
    pats = [(n, re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])")) for n in sorted(needles())]
    hits = []
    for f in filter(None, files):
        p = REPO / f
        if p.suffix in (".png", ".icns", ".zip") or not p.is_file():
            continue
        text = p.read_text(errors="ignore").lower()
        for n, rx in pats:
            if rx.search(text):
                hits.append(f"{f}: {n}")
    if hits:
        print("STOP, client data in the repo:\n  " + "\n  ".join(hits))
        return 1
    print(f"scan: {len(files)} files, no client names, mail addresses or domains")
    return 0


if __name__ == "__main__":
    sys.exit(main())
