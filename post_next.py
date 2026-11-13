#!/usr/bin/env python3
"""Post carousels to Instagram on an even schedule (Graph API).

Run it every ~10 minutes (the GitHub workflow does this). Each run asks "is it
time for the next post?" If yes, it publishes the next folder in order (carousel-16,
then 17, then 18...). It never skips ahead: if a folder is missing or fails, it stops
and waits instead of jumping past it. If the next slot is only a few minutes away it waits
for it, so posts land on time instead of on 10-minute ticks. Otherwise it exits.

Pacing: today's posts are spread evenly from `start` to `end` (schedule.json).
The first post goes out at `start`; after each post the remaining ones are
re-spaced so the last one lands at `end`. Late starts and delayed runs fix
themselves automatically.

If schedule.json includes a `distribution` block with mode `even`, the remaining
queue is also split as evenly as possible across that fixed date window. The
per-day count is recalculated from the remaining queue and days, so missed days
are rebalanced automatically. `per_day` remains the safety cap (50 when `all`).

Slide files can be 1.jpg / 2.jpg or 01.jpg / 02.jpg; they are ordered by number.

Env vars
  IG_USER_ID        Instagram account ID                            (required)
  IG_ACCESS_TOKEN   Access token                                    (required)
  PUBLIC_BASE_URL   Public URL prefix that maps to CAROUSEL_DIR     (required)
  GRAPH_HOST        graph.facebook.com (default) or graph.instagram.com
                    (use the latter for tokens made with "Instagram Login")
  CAROUSEL_DIR      Local folder with carousel-NN dirs              (default: carousels)
  STATE_FILE        Posted log                                      (default: posted.json)
  SCHEDULE_FILE     Schedule settings                               (default: schedule.json)
  DEFAULT_CAPTION   Used when a folder has no caption.txt
  DRY_RUN=1         Say what would happen, post nothing
  LOOP_MINUTES      How long one run waits for upcoming slots (default 9)
  NOW_OVERRIDE      Testing only, e.g. 2026-11-07T13:20
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

ROOT = Path(os.environ.get("CAROUSEL_DIR", "carousels"))
STATE = Path(os.environ.get("STATE_FILE", "posted.json"))
SCHEDULE = Path(os.environ.get("SCHEDULE_FILE", "schedule.json"))
ALERT_FILE = Path(os.environ.get("ALERT_FILE", "alert.txt"))  # read by the workflow to notify you
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
DEFAULT_CAPTION = os.environ.get("DEFAULT_CAPTION", "")
GRAPH = f"https://{os.environ.get('GRAPH_HOST') or 'graph.facebook.com'}/{os.environ.get('GRAPH_VERSION') or 'v23.0'}"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
MAX_SLIDES = 10      # Graph API carousel limit
MAX_PER_DAY = 50     # safety cap (Instagram's API limit is 100 per 24h)
GRACE = timedelta(minutes=15)  # a late cron run may still post this long after `end`
POLL_TIMEOUT = 300
LOOP_SECONDS = int(os.environ.get("LOOP_MINUTES") or 9) * 60  # how long one run stays alive waiting for its next slot


# ---------- files ----------
def num(path: Path) -> int:
    m = re.search(r"\d+", path.stem)
    return int(m.group()) if m else 10**9


def carousels() -> list[Path]:
    dirs = [d for d in ROOT.iterdir() if d.is_dir() and re.fullmatch(r"carousel-\d+", d.name)]
    return sorted(dirs, key=num)


def slides(folder: Path) -> list[Path]:
    files = [f for f in folder.iterdir() if f.suffix.lower() in (".jpg", ".jpeg")]
    files.sort(key=num)
    if not files:
        raise RuntimeError(f"{folder.name}: no .jpg slides found")
    if len(files) > MAX_SLIDES:
        print(f"WARNING: {folder.name} has {len(files)} slides; posting first {MAX_SLIDES}")
        files = files[:MAX_SLIDES]
    return files


def order_problem(folder: Path, done_names: set[str]) -> str | None:
    """Strict folder order: the next folder must directly follow the last one posted."""
    n = num(folder)
    earlier = [num(Path(d)) for d in done_names if num(Path(d)) < n]
    expected = max(earlier) + 1 if earlier else n
    if n != expected:
        return (f"{folder.name} cannot go next because carousel-{expected:02d} is missing. "
                f"Add that folder, or add its name to posted.json to skip it on purpose.")
    return None


def caption(folder: Path) -> str:
    f = folder / "caption.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else DEFAULT_CAPTION


def load_state() -> list[dict]:
    return json.loads(STATE.read_text()) if STATE.exists() else []


def save_state(entries: list[dict]) -> None:
    STATE.write_text(json.dumps(entries, indent=2))


# ---------- timing ----------
def now_local(tz: ZoneInfo) -> datetime:
    o = os.environ.get("NOW_OVERRIDE")
    if o:
        t = datetime.fromisoformat(o)
        return t.replace(tzinfo=tz) if t.tzinfo is None else t.astimezone(tz)
    return datetime.now(tz)


def at(day, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def posted_today(entries: list[dict], tz: ZoneInfo, day) -> list[datetime]:
    times = []
    for e in entries:
        try:
            t = datetime.fromisoformat(e["posted_at"]).astimezone(tz)
        except Exception:  # "manual" entries have no timestamp
            continue
        if t.date() == day:
            times.append(t)
    return sorted(times)


def plan(now, times, quota, start, end):
    """Return (post_now, next_due_or_None, message)."""
    remaining = quota - len(times)
    if remaining <= 0:
        return False, None, "Today's quota is done."
    if now < start:
        return False, start, f"Waiting for start time {start:%H:%M}."
    if now > end + GRACE:
        return False, None, "Past the end time; picking up again tomorrow."
    if not times:
        return True, None, "First post of the day."
    last = times[-1]
    gap = (end - last) / remaining if last < end else timedelta(minutes=5)
    next_due = last + gap
    if now >= next_due:
        return True, None, "On schedule."
    return False, next_due, f"Next post due at {next_due:%H:%M}."


def daily_cap(cfg: dict) -> int:
    """Read per_day as a ceiling; `all` means the hard safety limit."""
    raw = cfg.get("per_day", "all")
    if raw == "all":
        return MAX_PER_DAY
    try:
        cap = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("`per_day` must be a positive number or \"all\".") from e
    if cap < 1:
        raise ValueError("`per_day` must be at least 1.")
    return min(cap, MAX_PER_DAY)


def distribution_window(cfg: dict):
    """Return inclusive (first_day, last_day), or (None, None) for legacy mode."""
    settings = cfg.get("distribution")
    if settings is None:
        return None, None
    if not isinstance(settings, dict) or settings.get("mode") != "even":
        raise ValueError("`distribution` must be an object with mode `even`.")
    try:
        first_day = date.fromisoformat(settings["start_date"])
        days = int(settings["days"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("`distribution` needs an ISO `start_date` and positive `days`.") from e
    if days < 1:
        raise ValueError("`distribution.days` must be at least 1.")
    return first_day, first_day + timedelta(days=days - 1)


def even_daily_quota(queue_count: int, posted_today_count: int, days_left: int) -> int:
    """Ceiling division, counting today's completed posts so the target stays fixed."""
    if days_left < 1:
        raise ValueError("days_left must be at least 1")
    pending_for_window = queue_count + posted_today_count
    return (pending_for_window + days_left - 1) // days_left


# ---------- alerts ----------
_alerted = False


def alert(msg: str) -> None:
    """Write a problem report for the workflow to send you (the token is scrubbed out)."""
    global _alerted
    for secret in (TOKEN, quote(TOKEN, safe="")):
        if secret:
            msg = msg.replace(secret, "[hidden]")
    print("ALERT:", msg)
    if not _alerted:
        ALERT_FILE.write_text(msg[:600])
        _alerted = True


# ---------- Instagram ----------
def call(method: str, path: str, **params) -> dict:
    params["access_token"] = TOKEN
    for attempt in range(3):
        r = requests.request(method, f"{GRAPH}/{path}",
                             data=params if method == "POST" else None,
                             params=params if method == "GET" else None, timeout=60)
        if r.status_code < 500:
            break
        time.sleep(5 * (attempt + 1))
    if not r.ok:
        raise RuntimeError(f"Graph API {path} failed: {r.status_code} {r.text}")
    return r.json()


def wait_ready(container_id: str) -> None:
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        status = call("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Container {container_id} status {status}")
        time.sleep(3)
    raise RuntimeError(f"Container {container_id} not ready after {POLL_TIMEOUT}s")


def check_url(url: str) -> None:
    r = requests.head(url, allow_redirects=True, timeout=30)
    if not r.ok or "image" not in r.headers.get("content-type", ""):
        raise RuntimeError(f"Image not publicly reachable: {url} ({r.status_code})")


def publish(folder: Path) -> str:
    urls = [f"{BASE_URL}/{quote(folder.name)}/{quote(f.name)}" for f in slides(folder)]
    for u in urls:
        check_url(u)
    children = [call("POST", f"{IG_USER_ID}/media", image_url=u, is_carousel_item="true")["id"]
                for u in urls]
    for cid in children:
        wait_ready(cid)
    parent = call("POST", f"{IG_USER_ID}/media", media_type="CAROUSEL",
                  children=",".join(children), caption=caption(folder))["id"]
    wait_ready(parent)
    return call("POST", f"{IG_USER_ID}/media_publish", creation_id=parent)["id"]


# ---------- main ----------
def main() -> int:
    try:
        cfg = json.loads(SCHEDULE.read_text())
        tz = ZoneInfo(cfg["timezone"])
        cap = daily_cap(cfg)
        spread_start, spread_end = distribution_window(cfg)
    except Exception as e:
        alert(f"Invalid schedule settings: {e}")
        return 1

    single = DRY_RUN or bool(os.environ.get("NOW_OVERRIDE"))  # test modes: one pass only
    stop_at = time.time() + LOOP_SECONDS

    if not DRY_RUN:
        missing = [k for k, v in {"IG_USER_ID": IG_USER_ID, "IG_ACCESS_TOKEN": TOKEN,
                                  "PUBLIC_BASE_URL": BASE_URL}.items() if not v]
        if missing:
            alert("Missing settings in GitHub: " + ", ".join(missing))
            return 1

    while True:
        now = now_local(tz)
        start, end = at(now.date(), cfg["start"], tz), at(now.date(), cfg["end"], tz)
        if end <= start:
            alert("schedule.json: `end` must be later than `start`.")
            return 1

        entries = load_state()
        done = {e["carousel"] for e in entries}
        queue = [d for d in carousels() if d.name not in done]
        if not queue:
            print("Queue empty: every carousel has been posted.")
            return 0

        problem = order_problem(queue[0], done)
        if problem:
            alert("Stopped to keep folder order. " + problem)
            return 1

        if spread_start is not None and now.date() < spread_start:
            first_slot = at(spread_start, cfg["start"], tz)
            print(f"Schedule window starts {spread_start.isoformat()} at {cfg['start']}; waiting.")
            if single:
                return 0
            wait = (first_slot - now).total_seconds()
            if time.time() + wait > stop_at:
                return 0
            time.sleep(max(wait, 1))
            continue

        if spread_end is not None and now.date() > spread_end:
            alert(f"7-day schedule ended on {spread_end.isoformat()} with {len(queue)} unposted carousel(s). Extend the distribution window or update the schedule.")
            return 0

        times = posted_today(entries, tz, now.date())
        if spread_start is not None:
            days_left = (spread_end - now.date()).days + 1
            quota = even_daily_quota(len(queue), len(times), days_left)
            if quota > cap:
                alert(f"Cannot evenly distribute the remaining {len(queue) + len(times)} carousel(s) across {days_left} day(s): this needs {quota} posts/day, above the configured per_day cap of {cap}.")
                return 1
        else:
            quota = min(cap, len(queue) + len(times))

        post_now, next_due, why = plan(now, times, quota, start, end)
        print(f"[{now:%Y-%m-%d %H:%M:%S}] today {len(times)}/{quota} posted, {len(queue)} in queue. {why}")

        if post_now:
            folder = queue[0]
            if DRY_RUN:
                print(f"DRY RUN: would post {folder.name} with {len(slides(folder))} slides")
                return 0
            try:
                media_id = publish(folder)
            except Exception as e:
                alert(f"{folder.name} failed to post. Reason: {e}")
                raise
            entries.append({"carousel": folder.name, "media_id": media_id,
                            "posted_at": now_local(tz).isoformat()})
            save_state(entries)  # record right away so a later failure can't cause a double post
            print(f"Posted {folder.name} -> media {media_id}")
            if single:
                return 0
            continue

        # Not time yet: if the next slot falls inside this run's lifetime, wait for it.
        if single or next_due is None:
            return 0
        wait = (next_due - now).total_seconds()
        if time.time() + wait > stop_at:
            return 0
        time.sleep(max(wait, 1))


if __name__ == "__main__":
    try:
        code = main()
    except Exception as e:
        alert(f"The poster crashed: {e}")
        raise
    sys.exit(code)
