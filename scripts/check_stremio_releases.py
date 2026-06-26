#!/usr/bin/env python3
"""Early warning: alert when Stremio ships a new release (a possible streaming-server protocol change).

Polls GitHub for the latest tag of watched repos; on a new tag vs the last seen, exits non-zero (wire
to cron / Telegram) so we re-test the protocol BEFORE users hit breakage. First run just records the
current tags (no alert). Stdlib only.

Env:  WATCH_REPOS=Stremio/stremio-web,tsaridas/stremio-docker   STATE=/path/state.json
"""
import json
import os
import sys
import urllib.request

REPOS = os.environ.get("WATCH_REPOS", "Stremio/stremio-web,tsaridas/stremio-docker").split(",")
STATE = os.environ.get(
    "STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stremio_release_state.json")
)


def latest_tag(repo):
    url = f"https://api.github.com/repos/{repo}/tags?per_page=1"
    req = urllib.request.Request(
        url, headers={"User-Agent": "stremio-release-watch", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return data[0]["name"] if data else None


def main():
    try:
        with open(STATE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    changed = []
    for repo in [r.strip() for r in REPOS if r.strip()]:
        try:
            tag = latest_tag(repo)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: {repo}: {e}")
            continue
        if tag and state.get(repo) != tag:
            if state.get(repo) is not None:  # don't alert on the very first sighting
                changed.append(f"{repo}: {state.get(repo)} -> {tag}")
            state[repo] = tag
    try:
        with open(STATE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        print(f"WARN: cannot write state {STATE}: {e}")
    if changed:
        print("NEW STREMIO RELEASE(S) — re-test the streaming-server protocol:")
        for c in changed:
            print("  -", c)
        sys.exit(1)
    print("no new Stremio releases")


if __name__ == "__main__":
    main()
