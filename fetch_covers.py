"""
Download album covers for this week's listening.

Reads data/listening_log.csv, finds the albums behind your top songs and
top artists from the last 7 days, and saves their official Spotify cover art
to covers/ (300px JPGs), plus covers/index.json describing each one.

Old covers are removed each run, so the folder only holds the current week.
Uses the same credentials as spotify_logger.py. No packages to install.
"""

import csv
import json
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spotify_logger import api_get, get_access_token

LOG_FILE = Path("data/listening_log.csv")
ART_FILE = Path("data/album_art.json")
COVER_DIR = Path("covers")
MAX_COVERS = 15   # cap so the repo stays small


def this_weeks_plays():
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with LOG_FILE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        dt = datetime.fromisoformat(r["played_at"].replace("Z", "+00:00"))
        if dt >= cutoff and r.get("track_id"):
            out.append(r)
    return out


def pick_tracks(plays):
    """Top songs, plus each top artist's most played song, in priority order."""
    track_counts = Counter(p["track_id"] for p in plays)
    chosen = [tid for tid, _ in track_counts.most_common(10)]

    artist_counts = Counter(p["artists"].split(";")[0].strip() for p in plays)
    for artist, _ in artist_counts.most_common(10):
        theirs = Counter(p["track_id"] for p in plays
                         if p["artists"].split(";")[0].strip() == artist)
        tid = theirs.most_common(1)[0][0]
        if tid not in chosen:
            chosen.append(tid)
    return chosen, track_counts


def pick_image(images, target=300):
    """Smallest image at least `target` px wide, else the largest."""
    if not images:
        return None
    big_enough = [i for i in images if (i.get("width") or 0) >= target]
    if big_enough:
        return min(big_enough, key=lambda i: i["width"])["url"]
    return max(images, key=lambda i: i.get("width") or 0)["url"]


def main():
    plays = this_weeks_plays()
    if not plays:
        print("No plays this week - covers unchanged.")
        return

    track_ids, track_counts = pick_tracks(plays)
    names = {p["track_id"]: p["track"] for p in plays}

    # Album info saved by spotify_logger.py from your recent plays.
    art = {}
    if ART_FILE.exists():
        art = json.loads(ART_FILE.read_text(encoding="utf-8"))

    token = None
    albums = {}  # album_id -> info, in priority order
    for tid in track_ids:
        info = art.get(tid)
        if info is None:
            # Not saved yet: look this one song up (one at a time; Spotify
            # blocks multi-song lookups for personal apps).
            try:
                token = token or get_access_token()
                t = api_get(f"/tracks/{tid}", token)
                album = t["album"]
                info = {
                    "album_id": album["id"], "album": album["name"],
                    "artists": [a["name"] for a in album["artists"]],
                    "release_date": album.get("release_date", ""),
                    "spotify_url": album.get("external_urls", {}).get("spotify", ""),
                    "images": album.get("images", []),
                }
            except Exception as e:  # skip this song, keep going
                print(f"Couldn't look up {names.get(tid, tid)}: {e}")
                continue
        if not info.get("album_id"):
            continue
        entry = albums.setdefault(info["album_id"], {
            "album_id": info["album_id"],
            "album": info["album"],
            "artists": info["artists"],
            "release_date": info.get("release_date", ""),
            "spotify_url": info.get("spotify_url", ""),
            "image_url": pick_image(info.get("images", [])),
            "tracks": [],
        })
        entry["tracks"].append({"track_id": tid, "track": names.get(tid, ""),
                                "plays": track_counts.get(tid, 0)})

    COVER_DIR.mkdir(exist_ok=True)
    keep = []
    for entry in list(albums.values())[:MAX_COVERS]:
        url = entry.pop("image_url")
        if not url:
            continue
        path = COVER_DIR / f'{entry["album_id"]}.jpg'
        if not path.exists():
            try:
                with urllib.request.urlopen(url) as resp:
                    path.write_bytes(resp.read())
            except Exception as e:
                print(f'Couldn\'t download cover for {entry["album"]}: {e}')
                continue
        entry["file"] = f"covers/{path.name}"
        keep.append(entry)

    # Remove covers from earlier weeks.
    wanted = {Path(e["file"]).name for e in keep}
    for old in COVER_DIR.glob("*.jpg"):
        if old.name not in wanted:
            old.unlink()

    index = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
             "covers": keep}
    (COVER_DIR / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(keep)} covers.")


if __name__ == "__main__":
    main()
