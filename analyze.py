"""
Listening pattern analyzer.

Reads data/listening_log.csv and writes:
  reports/summary.md    - human-readable report (open it on GitHub)
  reports/summary.json  - the same numbers, for Claude's weekly newsletter

"This week" = the last 7 days, compared against the 7 days before that.
Times are shown in Eastern time. No packages to install.

Skips: Spotify doesn't say how long you listened, so this estimates it from
the gap between plays. Spotify stamps each play when it stops, so the time
since the previous play is roughly how long you heard this one. A play only
counts if you heard more than LISTEN_THRESHOLD of the song; the rest are
tracked as skips. (Anything skipped in under ~30s never reaches the log.)
"""

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_FILE = Path("data/listening_log.csv")
TOP_DIR = Path("data/top")
REPORT_DIR = Path("reports")
LOCAL_TZ = ZoneInfo("America/New_York")

TIME_BLOCKS = [  # (label, start hour, end hour) in local time
    ("Late night (12am-5am)", 0, 5),
    ("Morning (5am-12pm)", 5, 12),
    ("Afternoon (12pm-5pm)", 12, 17),
    ("Evening (5pm-9pm)", 17, 21),
    ("Night (9pm-12am)", 21, 24),
]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LISTEN_THRESHOLD = 0.5  # must hear more than half the song to count


def load_plays():
    if not LOG_FILE.exists():
        return []
    plays = []
    with LOG_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = row["played_at"].replace("Z", "+00:00")
            row["dt"] = datetime.fromisoformat(ts).astimezone(LOCAL_TZ)
            row["artist_list"] = [a.strip() for a in row["artists"].split(";") if a.strip()]
            row["main_artist"] = row["artist_list"][0] if row["artist_list"] else "Unknown"
            row["duration"] = int(row.get("duration_sec") or 0)
            plays.append(row)
    plays.sort(key=lambda r: r["dt"])
    estimate_listening(plays)
    return plays


def estimate_listening(plays):
    """Estimate seconds heard for each play and mark skips."""
    prev_dt = None
    for p in plays:
        dur = p["duration"]
        if prev_dt is None:
            heard = dur  # first play of the log: assume full
        else:
            gap = (p["dt"] - prev_dt).total_seconds()
            # A gap longer than the song means a pause or break: count it as full.
            heard = dur if gap >= dur else max(gap, 0)
        p["heard_sec"] = heard
        p["skipped"] = dur > 0 and heard <= LISTEN_THRESHOLD * dur
        p["minutes"] = heard / 60
        prev_dt = p["dt"]


def track_key(p):
    return f'{p["track"]} - {p["main_artist"]}'


def count_artists(plays):
    """Each credited artist gets credit, so features count too."""
    c = Counter()
    for p in plays:
        for a in p["artist_list"]:
            c[a] += 1
    return c


def ranked_with_change(this_counts, last_counts, n=10):
    last_rank = {name: i + 1 for i, (name, _) in enumerate(last_counts.most_common())}
    out = []
    for i, (name, plays) in enumerate(this_counts.most_common(n), start=1):
        prev = last_rank.get(name)
        if prev is None:
            change = "NEW"
        elif prev == i:
            change = "="
        elif prev > i:
            change = f"+{prev - i}"
        else:
            change = f"-{i - prev}"
        out.append({"rank": i, "name": name, "plays": plays,
                    "last_week_plays": last_counts.get(name, 0), "change": change})
    return out


def time_of_day(plays):
    blocks = Counter()
    days = Counter()
    for p in plays:
        h = p["dt"].hour
        for label, start, end in TIME_BLOCKS:
            if start <= h < end:
                blocks[label] += 1
        days[WEEKDAYS[p["dt"].weekday()]] += 1
    return ({label: blocks.get(label, 0) for label, _, _ in TIME_BLOCKS},
            {d: days.get(d, 0) for d in WEEKDAYS})


def artist_streaks(plays, today):
    """Artists played every day in a row up to today (or yesterday)."""
    days_by_artist = defaultdict(set)
    for p in plays:
        for a in p["artist_list"]:
            days_by_artist[a].add(p["dt"].date())
    streaks = []
    for artist, days in days_by_artist.items():
        day = today if today in days else today - timedelta(days=1)
        length = 0
        while day in days:
            length += 1
            day -= timedelta(days=1)
        if length >= 3:
            streaks.append({"artist": artist, "days": length})
    return sorted(streaks, key=lambda s: -s["days"])[:5]


def release_eras(plays):
    eras = Counter()
    for p in plays:
        year = (p.get("album_release") or "")[:4]
        if year.isdigit():
            eras[f"{year[:3]}0s"] += 1
    return dict(sorted(eras.items()))


def listening_style(plays):
    ctx = Counter(p.get("context_type") or "other" for p in plays)
    # "Album sessions": 4+ consecutive plays from the same album
    sessions, run = 0, 1
    for prev, cur in zip(plays, plays[1:]):
        if cur["album"] == prev["album"] and (cur["dt"] - prev["dt"]) < timedelta(minutes=15):
            run += 1
        else:
            sessions += run >= 4
            run = 1
    sessions += run >= 4
    return {"played_from": dict(ctx.most_common()), "full_album_sessions": sessions}


def latest_top_snapshot():
    if not TOP_DIR.exists():
        return None
    files = sorted(TOP_DIR.glob("*.json"))
    if not files:
        return None
    snap = json.loads(files[-1].read_text(encoding="utf-8"))
    return {term: {"artists": snap[term]["artists"][:10]}
            for term in ("short_term", "medium_term", "long_term") if term in snap}


def pct(part, whole):
    return round(100 * part / whole) if whole else 0


def skip_stats(all_week):
    skipped = [p for p in all_week if p["skipped"]]
    by_track = Counter(track_key(p) for p in skipped)
    by_artist = Counter(p["main_artist"] for p in skipped)
    return {
        "skip_rate_pct": pct(len(skipped), len(all_week)),
        "skipped_plays": len(skipped),
        "most_skipped_tracks": [{"track": t, "skips": n} for t, n in by_track.most_common(5)],
        "most_skipped_artists": [{"artist": a, "skips": n} for a, n in by_artist.most_common(5)],
    }


def analyze():
    all_plays = load_plays()
    now = datetime.now(LOCAL_TZ)
    week_start = now - timedelta(days=7)
    last_start = now - timedelta(days=14)

    all_this_week = [p for p in all_plays if p["dt"] >= week_start]
    skips = skip_stats(all_this_week)

    # Everything below uses real listens only (skips removed).
    plays = [p for p in all_plays if not p["skipped"]]
    this_week = [p for p in plays if p["dt"] >= week_start]
    last_week = [p for p in plays if last_start <= p["dt"] < week_start]
    before = [p for p in plays if p["dt"] < week_start]

    seen_artists = {a for p in before for a in p["artist_list"]}
    seen_tracks = {p["track_id"] for p in before}

    this_artists, last_artists = count_artists(this_week), count_artists(last_week)
    this_tracks = Counter(track_key(p) for p in this_week)
    last_tracks = Counter(track_key(p) for p in last_week)

    new_artists = Counter()
    for p in this_week:
        for a in p["artist_list"]:
            if a not in seen_artists:
                new_artists[a] += 1
    new_track_plays = sum(1 for p in this_week if p["track_id"] not in seen_tracks)

    blocks, days = time_of_day(this_week)
    minutes = sum(p["minutes"] for p in this_week)

    return {
        "generated_at": now.isoformat(timespec="minutes"),
        "window": {"start": week_start.date().isoformat(), "end": now.date().isoformat()},
        "log_since": all_plays[0]["dt"].date().isoformat() if all_plays else None,
        "total_plays_logged": len(all_plays),
        "this_week": {
            "plays": len(this_week),
            "minutes": round(minutes),
            "unique_tracks": len(this_tracks),
            "unique_artists": len(this_artists),
        },
        "last_week": {
            "plays": len(last_week),
            "minutes": round(sum(p["minutes"] for p in last_week)),
        },
        "counting_rule": f"Only plays where more than {int(LISTEN_THRESHOLD * 100)}% of the song was heard (estimated).",
        "skips": skips,
        "top_artists": ranked_with_change(this_artists, last_artists),
        "top_tracks": ranked_with_change(this_tracks, last_tracks),
        "dropped_off": [a for a, _ in last_artists.most_common(5) if a not in this_artists],
        "discovery": {
            "new_artists": [{"name": a, "plays": n} for a, n in new_artists.most_common(10)],
            "plays_of_new_tracks_pct": pct(new_track_plays, len(this_week)),
            "note": "New = first time in the log (only meaningful once the log has some history).",
        },
        "on_repeat": [{"track": t, "plays": n} for t, n in this_tracks.most_common() if n >= 3][:10],
        "time_of_day": blocks,
        "day_of_week": days,
        "busiest_day": max(days, key=days.get) if this_week else None,
        "streaks": artist_streaks(plays, now.date()),
        "release_eras": release_eras(this_week),
        "listening_style": listening_style(this_week),
        "spotify_top_snapshot": latest_top_snapshot(),
    }


def bar(n, biggest, width=20):
    return "█" * round(width * n / biggest) if biggest else ""


def to_markdown(s):
    w, lw = s["this_week"], s["last_week"]
    diff = w["plays"] - lw["plays"]
    lines = [
        "# Listening Report",
        f'**{s["window"]["start"]} to {s["window"]["end"]}** · updated {s["generated_at"][:16].replace("T", " ")} ET',
        "",
        f'**{w["plays"]} plays** ({diff:+d} vs last week) · **{w["minutes"]} minutes** · '
        f'{w["unique_artists"]} artists · {w["unique_tracks"]} songs',
        "",
        f'_Counting real listens only. {s["skips"]["skipped_plays"]} skips left out '
        f'({s["skips"]["skip_rate_pct"]}% skip rate)._',
        "",
        "## Top artists",
        "| # | Artist | Plays | vs last week |",
        "|---|---|---|---|",
    ]
    lines += [f'| {a["rank"]} | {a["name"]} | {a["plays"]} | {a["change"]} |' for a in s["top_artists"]]
    lines += ["", "## Top songs", "| # | Song | Plays | vs last week |", "|---|---|---|---|"]
    lines += [f'| {t["rank"]} | {t["name"]} | {t["plays"]} | {t["change"]} |' for t in s["top_tracks"]]

    if s["dropped_off"]:
        lines += ["", "**Fell off this week:** " + ", ".join(s["dropped_off"])]

    d = s["discovery"]
    lines += ["", "## Discovery",
              f'{d["plays_of_new_tracks_pct"]}% of plays were songs not in your log before this week.']
    if d["new_artists"]:
        lines.append("New artists: " + ", ".join(f'{a["name"]} ({a["plays"]})' for a in d["new_artists"]))

    if s["on_repeat"]:
        lines += ["", "## On repeat (3+ plays)"]
        lines += [f'- {r["track"]} - {r["plays"]}x' for r in s["on_repeat"]]

    sk = s["skips"]
    if sk["most_skipped_tracks"]:
        lines += ["", "## Most skipped"]
        lines += [f'- {r["track"]} - skipped {r["skips"]}x' for r in sk["most_skipped_tracks"]]

    lines += ["", "## When you listen", "```"]
    biggest = max(s["time_of_day"].values(), default=0)
    lines += [f"{k:<22} {bar(v, biggest)} {v}" for k, v in s["time_of_day"].items()]
    lines.append("")
    biggest = max(s["day_of_week"].values(), default=0)
    lines += [f"{k:<22} {bar(v, biggest)} {v}" for k, v in s["day_of_week"].items()]
    lines.append("```")

    if s["streaks"]:
        lines += ["", "## Streaks"]
        lines += [f'- {x["artist"]}: {x["days"]} days in a row' for x in s["streaks"]]

    if s["release_eras"]:
        lines += ["", "## Eras", ", ".join(f"{k}: {v}" for k, v in s["release_eras"].items())]

    st = s["listening_style"]
    lines += ["", "## How you listen",
              "Played from: " + ", ".join(f"{k} {v}" for k, v in st["played_from"].items()),
              f'Full-album sessions (4+ tracks in a row): {st["full_album_sessions"]}']

    lines += ["", f'_Log started {s["log_since"]} · {s["total_plays_logged"]} plays recorded total._']
    return "\n".join(lines) + "\n"


def main():
    summary = analyze()
    REPORT_DIR.mkdir(exist_ok=True)
    (REPORT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (REPORT_DIR / "summary.md").write_text(to_markdown(summary), encoding="utf-8")
    print(f'Report written: {summary["this_week"]["plays"]} plays this week.')


if __name__ == "__main__":
    main()
