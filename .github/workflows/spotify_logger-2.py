"""
Spotify listening logger.

Each run:
  1. Uses your refresh token to get a fresh access token.
  2. Pulls your recently played tracks (Spotify keeps only the last 50).
  3. Appends any new plays to data/listening_log.csv (no duplicates).
  4. Once a day, saves a snapshot of your top tracks/artists to data/top/.

Reads credentials from environment variables:
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN
"""

import base64
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path("data")
LOG_FILE = DATA_DIR / "listening_log.csv"
TOP_DIR = DATA_DIR / "top"
ART_FILE = DATA_DIR / "album_art.json"   # track_id -> album + cover link, for fetch_covers.py
API = "https://api.spotify.com/v1"

FIELDS = [
    "played_at",       # UTC timestamp, e.g. 2026-10-26T21:40:12.345Z
    "track",
    "artists",         # semicolon-separated
    "album",
    "album_release",
    "duration_sec",
    "explicit",
    "context_type",    # playlist / album / artist / blank
    "context_uri",     # which playlist or album you played it from
    "track_id",
]


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}")
    return value


def get_access_token():
    cid, secret = env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_CLIENT_SECRET")
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": env("SPOTIFY_REFRESH_TOKEN"),
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if "refresh_token" in data:
        print("Note: Spotify issued a new refresh token. If runs start failing, "
              "re-run get_refresh_token.py and update the secret.")
    return data["access_token"]


def api_get(path, token, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def load_seen():
    """Return the set of played_at timestamps already in the log."""
    if not LOG_FILE.exists():
        return set()
    with LOG_FILE.open(newline="", encoding="utf-8") as f:
        return {row["played_at"] for row in csv.DictReader(f)}


def to_row(item):
    t = item["track"]
    ctx = item.get("context") or {}
    return {
        "played_at": item["played_at"],
        "track": t["name"],
        "artists": "; ".join(a["name"] for a in t["artists"]),
        "album": t["album"]["name"],
        "album_release": t["album"].get("release_date", ""),
        "duration_sec": round(t["duration_ms"] / 1000),
        "explicit": t.get("explicit", False),
        "context_type": ctx.get("type", ""),
        "context_uri": ctx.get("uri", ""),
        "track_id": t["id"],
    }


def save_album_art(items):
    """Remember each track's album and cover link (Spotify includes them in recent plays)."""
    art = {}
    if ART_FILE.exists():
        try:
            art = json.loads(ART_FILE.read_text(encoding="utf-8"))
        except ValueError:
            art = {}
    changed = False
    for item in items:
        t = item.get("track") or {}
        album = t.get("album") or {}
        images = album.get("images") or []
        if not t.get("id") or not images or t["id"] in art:
            continue
        art[t["id"]] = {
            "album_id": album.get("id", ""),
            "album": album.get("name", ""),
            "artists": [a["name"] for a in album.get("artists", [])],
            "release_date": album.get("release_date", ""),
            "spotify_url": (album.get("external_urls") or {}).get("spotify", ""),
            "images": [{"url": i["url"], "width": i.get("width")} for i in images],
        }
        changed = True
    if changed:
        DATA_DIR.mkdir(exist_ok=True)
        ART_FILE.write_text(json.dumps(art, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def log_recent(token):
    data = api_get("/me/player/recently-played", token, {"limit": 50})
    save_album_art(data.get("items", []))
    seen = load_seen()
    new_rows = [to_row(i) for i in data.get("items", []) if i.get("track")]
    new_rows = [r for r in new_rows if r["played_at"] not in seen]
    new_rows.sort(key=lambda r: r["played_at"])  # oldest first

    DATA_DIR.mkdir(exist_ok=True)
    write_header = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    print(f"Added {len(new_rows)} new plays ({len(seen) + len(new_rows)} total).")
    if len(new_rows) == 50:
        print("Warning: got a full 50 new plays - some may have been missed. "
              "Consider running more often.")


def snapshot_top(token):
    """Save top tracks/artists once per day (short ~4 wks, medium ~6 mo, long ~1 yr)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = TOP_DIR / f"{today}.json"
    if out.exists():
        return
    snapshot = {"date": today}
    for term in ("short_term", "medium_term", "long_term"):
        tracks = api_get("/me/top/tracks", token, {"limit": 50, "time_range": term})
        artists = api_get("/me/top/artists", token, {"limit": 50, "time_range": term})
        snapshot[term] = {
            "tracks": [
                f'{t["name"]} - {", ".join(a["name"] for a in t["artists"])}'
                for t in tracks.get("items", [])
            ],
            "artists": [a["name"] for a in artists.get("items", [])],
        }
    TOP_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved top tracks/artists snapshot for {today}.")


def main():
    token = get_access_token()
    log_recent(token)
    snapshot_top(token)


if __name__ == "__main__":
    main()
