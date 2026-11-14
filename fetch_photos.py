"""
Find photos for this week's issue of The Weekly Rotation.

Reads reports/summary.json (written by analyze.py), works out who the week's
stories are about (top artists, new discoveries, listening streaks), searches
the web for each one, and saves a photo of them to photos/, plus
photos/index.json saying who each photo is, why they're in this week's issue,
and who took the photo.

Where it searches, in order:
  1. Wikipedia - the photo at the top of the artist's page.
  2. Openverse (openverse.org) - freely licensed photos from Flickr and other
     sites, for artists Wikipedia has no photo of.

Only freely licensed photos are kept, because this repo is public. Fair-use
images (logos, album art), drawings and no-changes (ND) licenses are skipped.
A photo is only used if it's clearly about this artist and about music (e.g.
"Mercury" the planet is ignored), so you don't get the wrong person who
happens to share the name.

Old photos are removed each run, so the folder only holds the current week.
No API keys and no packages to install.
"""

import html
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SUMMARY_FILE = Path("reports/summary.json")
PHOTO_DIR = Path("photos")
WIKI_API = "https://en.wikipedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
# Free licenses we accept from Openverse. ND ("no derivatives") is left out
# because the newsletter may crop photos.
OPENVERSE_LICENSES = "cc0,pdm,by,by-sa,by-nc,by-nc-sa"
# Wikipedia asks scripts to identify themselves.
USER_AGENT = "WeeklyRotationPhotos/1.0 (personal listening newsletter; GitHub Actions)"

PHOTO_WIDTH = 800       # px; plenty for a newsletter, small enough for the repo
MAX_PHOTOS = 15         # cap so the repo stays small
TOP_ARTISTS = 8         # how many of the week's top artists to look for
NEW_ARTISTS = 5         # how many new discoveries to look for

# A Wikipedia page counts as "about a musician" if its short description has one of these.
MUSIC_WORDS = (
    "singer", "rapper", "musician", "band", "songwriter", "producer", "dj",
    "composer", "group", "duo", "trio", "vocalist", "guitarist", "drummer",
    "pianist", "bassist", "music", "hip hop", "record", "artist", "orchestra",
    "ensemble", "collective", "idol",
)
TITLE_SUFFIXES = ("", " (band)", " (musician)", " (singer)", " (rapper)",
                  " (group)", " (DJ)", " (producer)", " (duo)")


# ---------------------------------------------------------------- who to find

def pick_subjects(summary):
    """Artists this week's issue will talk about, most important first."""
    subjects = {}  # name -> list of reasons

    def add(name, reason):
        if name and name != "Unknown":
            subjects.setdefault(name, []).append(reason)

    for a in summary.get("top_artists", [])[:TOP_ARTISTS]:
        reason = f'#{a["rank"]} artist this week ({a["plays"]} plays)'
        if a.get("change") == "NEW":
            reason += ", new to the chart"
        add(a["name"], reason)
    for a in summary.get("discovery", {}).get("new_artists", [])[:NEW_ARTISTS]:
        add(a["name"], f'new discovery ({a["plays"]} plays)')
    for s in summary.get("streaks", []):
        add(s["artist"], f'{s["days"]}-day listening streak')
    return list(subjects.items())


# ---------------------------------------------------------------- Wikipedia

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return {}


def wiki(params):
    params = {"format": "json", "formatversion": 2, **params}
    return get_json(WIKI_API + "?" + urllib.parse.urlencode(params))


def norm(text):
    """Compare names loosely: case, accents, '&' vs 'and', punctuation."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = text.replace("&", " and ").replace("$", "s")
    text = re.sub(r"^the\s+", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def mentions_music(text, words=MUSIC_WORDS):
    text = (text or "").casefold()
    return any(re.search(rf"\b{w}", text) for w in words)


def is_music_page(page):
    return mentions_music(page.get("description"))


def name_matches(page, artist):
    title = re.sub(r"\s*\([^)]*\)$", "", page.get("title", ""))
    return norm(title) == norm(artist)


def find_page(artist):
    """The artist's Wikipedia page (with a lead image), or None."""
    props = {"prop": "pageimages|description|info", "piprop": "name",
             "inprop": "url", "redirects": 1}

    # 1. Try the obvious titles directly: "Artist", "Artist (band)", ...
    titles = "|".join(artist + s for s in TITLE_SUFFIXES)
    pages = wiki({"action": "query", "titles": titles, **props})
    pages = pages.get("query", {}).get("pages", [])
    # Prefer the exact title, then the disambiguated ones, in TITLE_SUFFIXES order.
    order = {norm(artist + s): i for i, s in enumerate(TITLE_SUFFIXES)}
    pages.sort(key=lambda p: order.get(norm(p.get("title", "")), 99))
    for p in pages:
        if not p.get("missing") and is_music_page(p) and p.get("pageimage"):
            return p

    # 2. Fall back to a search, but only accept a result with the same name.
    found = wiki({"action": "query", "generator": "search",
                  "gsrsearch": f"{artist} music", "gsrlimit": 5, **props})
    results = sorted(found.get("query", {}).get("pages", []),
                     key=lambda p: p.get("index", 99))
    for p in results:
        if name_matches(p, artist) and is_music_page(p) and p.get("pageimage"):
            return p
    return None


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def image_info(filename):
    """URL and license details for a Wikipedia lead image, or None if it isn't free."""
    data = wiki({"action": "query", "titles": f"File:{filename}",
                 "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                 "iiurlwidth": PHOTO_WIDTH})
    pages = data.get("query", {}).get("pages", [])
    if not pages or not pages[0].get("imageinfo"):
        return None
    info = pages[0]["imageinfo"][0]
    meta = {k: v.get("value", "") for k, v in (info.get("extmetadata") or {}).items()}

    if str(meta.get("NonFree", "")).lower() == "true":
        return None  # fair-use image: fine on Wikipedia, not in a public repo
    if info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
        return None  # skips SVG logos and drawings
    license_name = strip_html(meta.get("LicenseShortName"))
    if not license_name:
        return None

    return {
        "url": info.get("thumburl") or info.get("url"),
        "license": license_name,
        "license_url": meta.get("LicenseUrl", ""),
        "credit": strip_html(meta.get("Artist")) or strip_html(meta.get("Credit")),
        "source_page": info.get("descriptionurl", ""),
    }


def from_wikipedia(artist):
    page = find_page(artist)
    info = image_info(page["pageimage"]) if page else None
    if not info or not info["url"]:
        return None
    info.update(found_via="Wikipedia", wikipedia=page.get("fullurl", ""),
                about=page.get("description", ""))
    return info


# ---------------------------------------------------------------- Openverse

# Words that show a photo is of a performance or musician, in its title or tags.
PHOTO_MUSIC_WORDS = MUSIC_WORDS + (
    "concert", "live", "stage", "festival", "tour", "gig", "performing",
    "performance", "guitar", "microphone", "mic", "show",
)


def from_openverse(artist):
    """A freely licensed photo of the artist from Openverse, or None."""
    params = {"q": f'"{artist}"', "license": OPENVERSE_LICENSES,
              "category": "photograph", "page_size": 20}
    data = get_json(OPENVERSE_API + "?" + urllib.parse.urlencode(params))
    want = norm(artist)
    for r in data.get("results", []):
        if r.get("mature"):
            continue
        title = r.get("title") or ""
        tags = " ".join(t.get("name", "") for t in r.get("tags") or [])
        # The artist's name must be in the title, and the photo must be about music.
        if want not in norm(title):
            continue
        if not mentions_music(f"{title} {tags}", PHOTO_MUSIC_WORDS):
            continue
        if (r.get("width") or 0) and r["width"] < 400:
            continue  # too small to print
        license_name = f'{(r.get("license") or "").upper()} {r.get("license_version") or ""}'.strip()
        if r.get("license") in ("cc0", "pdm"):
            license_name = "Public domain" if r["license"] == "pdm" else "CC0"
        return {
            "url": r.get("url"),
            "thumb": r.get("thumbnail"),
            "license": license_name,
            "license_url": r.get("license_url", ""),
            "credit": r.get("creator") or "Unknown",
            "source_page": r.get("foreign_landing_url", ""),
            "found_via": f'Openverse ({r.get("source") or r.get("provider") or "web"})',
            "wikipedia": "",
            "about": title,
        }
    return None


# ---------------------------------------------------------------- main

def slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "artist"


MAX_BYTES = 6_000_000  # skip huge originals (use the smaller copy instead)


def download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file too large")
    path.write_bytes(data)


def save_photo(artist, info):
    """Download the photo (or its smaller copy). Returns the path, or None."""
    for url in (info["url"], info.get("thumb")):
        if not url:
            continue
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        path = PHOTO_DIR / f"{slug(artist)}{ext}"
        if path.exists():
            return path
        try:
            download(url, path)
            return path
        except Exception as e:
            print(f"Couldn't download photo of {artist} ({url}): {e}")
    return None


def main():
    if not SUMMARY_FILE.exists():
        print("No reports/summary.json yet - run analyze.py first.")
        return
    summary = json.loads(SUMMARY_FILE.read_text(encoding="utf-8"))
    subjects = pick_subjects(summary)
    if not subjects:
        print("No artists this week - photos unchanged.")
        return

    PHOTO_DIR.mkdir(exist_ok=True)
    keep, missing = [], []
    for artist, reasons in subjects:
        if len(keep) >= MAX_PHOTOS:
            break
        path = info = None
        for search in (from_wikipedia, from_openverse):
            try:
                info = search(artist)
            except Exception as e:
                print(f"{search.__name__} failed for {artist}: {e}")
                info = None
            time.sleep(1)  # be gentle with Wikipedia and Openverse
            if info:
                path = save_photo(artist, info)
                if path:
                    break
        if not path:
            print(f"No free photo found for {artist}.")
            missing.append(artist)
            continue
        keep.append({
            "artist": artist,
            "why": reasons,
            "file": f"photos/{path.name}",
            "found_via": info["found_via"],
            "wikipedia": info["wikipedia"],
            "about": info["about"],
            "credit": info["credit"],
            "license": info["license"],
            "license_url": info["license_url"],
            "source": info["source_page"],
        })
        print(f"Saved photo of {artist} (from {info['found_via']}).")

    # Remove photos from earlier weeks.
    wanted = {Path(p["file"]).name for p in keep}
    for old in PHOTO_DIR.iterdir():
        if old.name != "index.json" and old.name not in wanted:
            old.unlink()

    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "week": summary.get("window", {}),
        "note": "Credit each photo as: Photo: <credit>, <license>.",
        "photos": keep,
        "no_photo_found": missing,
    }
    (PHOTO_DIR / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(keep)} photos ({len(missing)} artists had none).")


if __name__ == "__main__":
    main()
