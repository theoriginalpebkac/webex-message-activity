#!/usr/bin/env python3
"""
Webex message activity fetcher.

Pulls the messages you authored (plus a little surrounding context) for a target
day or month and saves them, grouped by local calendar day, into data/.

Design contract:
  - Read broadly, persist narrowly. Webex has no server-side author filter, so we
    fetch every active room in the window and keep only the (room, local-date)
    buckets where YOU posted at least one message. Everything else is discarded.
  - The report script never touches the Webex API. It reads only data/. So every
    fact it needs (room title, author display name) is denormalized onto each
    saved record here.
  - Re-runnable: a fresh run for a window rebuilds that window's day files from
    scratch (idempotent). An interrupted run for the same window resumes by
    skipping rooms already completed.

Config lives in two files, split by how often they change:
  - credentials.txt : the Webex token only (you rotate this ~every 12h).
  - settings.yaml   : email, timezone, and future tunables.
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from collections import defaultdict
from typing import List, Dict, Any, Optional

import yaml
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, "credentials.txt")
SETTINGS_FILE = os.path.join(HERE, "settings.yaml")
DATA_DIR = os.path.join(HERE, "data")

BASE_URL = "https://webexapis.com/v1"
UTC = ZoneInfo("UTC")

# Pace proactively well under the limit; a 429 can cost us an hour (Cisco's docs
# show a 3600s Retry-After example), so we never want to provoke one.
REQUESTS_PER_MINUTE = 150
REQUEST_TIMEOUT = 30  # seconds, per HTTP request
MAX_NETWORK_RETRIES = 4  # transient connection/timeout errors, not 429s

# Webex developer-portal PATs live ~12h. We warn if the token file looks stale
# before a long run, but a 401 from the API is the authoritative "expired" signal.
TOKEN_MAX_AGE_HOURS = 12

# Message keys we deliberately drop before saving. We keep the rest of the
# payload verbatim (text carries pasted XML/config as-is). We drop the styling
# twin of `text` and any uploaded/bot attachments.
DROP_KEYS = ("html", "files", "attachments")


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

def load_token() -> str:
    """Read the Webex token from credentials.txt (whole file, stripped)."""
    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(
            f"Error: {CREDENTIALS_FILE} not found.\n"
            "Create it with your current Webex token as the only contents "
            "(see credentials.txt.example)."
        )

    # The token is a bearer secret; keep the file owner-only.
    stat = os.stat(CREDENTIALS_FILE)
    mode = oct(stat.st_mode)[-3:]
    if mode != "600":
        print(f"WARNING: {CREDENTIALS_FILE} mode is {mode}; tightening to 600.")
        os.chmod(CREDENTIALS_FILE, 0o600)

    # Cheap staleness heads-up before we start a long run. Not authoritative —
    # the API's 401 is. mtime is when you last pasted a token in.
    age_h = (time.time() - stat.st_mtime) / 3600
    if age_h >= TOKEN_MAX_AGE_HOURS:
        print(f"WARNING: {os.path.basename(CREDENTIALS_FILE)} last changed "
              f"{age_h:.0f}h ago; Webex tokens expire ~{TOKEN_MAX_AGE_HOURS}h. "
              "Paste a fresh token if this run fails to authenticate.")

    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
        token = f.read().strip()

    if not token or token.startswith("PASTE"):
        sys.exit(f"Error: {CREDENTIALS_FILE} is empty or still a placeholder.")
    return token


def load_settings() -> dict:
    """Read settings.yaml and validate the keys the fetcher needs."""
    if not os.path.exists(SETTINGS_FILE):
        sys.exit(
            f"Error: {SETTINGS_FILE} not found.\n"
            "Copy settings.yaml.example to settings.yaml and fill it in."
        )
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}

    for key in ("email", "timezone"):
        if not settings.get(key):
            sys.exit(f"Error: '{key}' is missing from {SETTINGS_FILE}.")

    # Fail fast on a bad zone rather than mis-bucketing every message.
    try:
        ZoneInfo(settings["timezone"])
    except Exception:
        sys.exit(f"Error: '{settings['timezone']}' is not a valid IANA timezone.")

    return settings


# --------------------------------------------------------------------------- #
# Fetcher
# --------------------------------------------------------------------------- #

class WebexFetcher:
    def __init__(self, token: str, my_email: str, tz_str: str,
                 window_label: str, local_start: datetime, local_end: datetime):
        self.my_email = my_email.lower()
        self.tz = ZoneInfo(tz_str)
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.req_delay = 60.0 / REQUESTS_PER_MINUTE

        # Window, in both local and UTC forms. Local bounds decide which day
        # files exist; UTC bounds drive the API paging cutoff.
        self.window_label = window_label
        self.local_start = local_start
        self.local_end = local_end
        self.window_start_utc = local_start.astimezone(UTC)
        self.window_end_utc = local_end.astimezone(UTC)

        os.makedirs(DATA_DIR, exist_ok=True)
        self.state_file = os.path.join(DATA_DIR, ".state.json")
        self.people_cache_file = os.path.join(DATA_DIR, "people_cache.json")
        self.people_cache = self._load_json(self.people_cache_file)

        self.state = self._init_run_state()

    # ---- small helpers ---------------------------------------------------- #

    @staticmethod
    def _load_json(path: str) -> dict:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    @staticmethod
    def _atomic_write_json(path: str, obj: dict):
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _parse_utc(s: str) -> datetime:
        # Webex returns e.g. 2026-08-13T18:53:28.962Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)

    def _day_file(self, local_date: str) -> str:
        return os.path.join(DATA_DIR, f"{local_date}.jsonl")

    def _window_dates(self) -> List[str]:
        """Every local calendar date the window covers, as YYYY-MM-DD strings."""
        out = []
        d = self.local_start.date()
        end = self.local_end.date()  # exclusive
        while d < end:
            out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    # ---- run state (idempotency + resume) --------------------------------- #

    def _init_run_state(self) -> dict:
        """
        Decide fresh-run vs resume.

        Fresh run  (no state, or state is for a different window): delete this
                   window's day files and start with an empty rooms_done set.
        Resume     (state matches this window): keep the day files already
                   written and skip rooms recorded as done.
        """
        state = self._load_json(self.state_file)
        if state.get("window") == self.window_label:
            done = len(state.get("rooms_done", []))
            if done:
                print(f"Resuming window {self.window_label}: "
                      f"{done} room(s) already complete, skipping them.")
            return {"window": self.window_label, "rooms_done": state.get("rooms_done", [])}

        # Fresh run: wipe any prior output for these dates so we never duplicate.
        for local_date in self._window_dates():
            path = self._day_file(local_date)
            if os.path.exists(path):
                os.remove(path)
        state = {"window": self.window_label, "rooms_done": []}
        self._atomic_write_json(self.state_file, state)
        return state

    def _mark_room_done(self, room_id: str):
        self.state["rooms_done"].append(room_id)
        self._atomic_write_json(self.state_file, self.state)

    def _clear_state(self):
        if os.path.exists(self.state_file):
            os.remove(self.state_file)

    # ---- HTTP ------------------------------------------------------------- #

    def _request(self, url: str, params: dict = None, ignore_404: bool = False) -> requests.Response:
        """GET with proactive pacing, 429 back-off, and transient-error retry."""
        time.sleep(self.req_delay)

        network_attempts = 0
        while True:
            try:
                resp = requests.get(url, headers=self.headers, params=params,
                                    timeout=REQUEST_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                network_attempts += 1
                if network_attempts > MAX_NETWORK_RETRIES:
                    raise
                backoff = 2 ** network_attempts
                print(f"    [net] {type(e).__name__}; retry {network_attempts}"
                      f"/{MAX_NETWORK_RETRIES} in {backoff}s...")
                time.sleep(backoff)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"\n[!] 429 rate limited. Sleeping {retry_after}s "
                      f"(state is checkpointed; safe to Ctrl-C and resume)...")
                time.sleep(retry_after)
                continue

            if resp.status_code == 401:
                sys.exit("\nError: Webex rejected the token (401). It has likely "
                         "expired — paste a fresh token into credentials.txt and re-run "
                         "(a partial run resumes where it left off).")

            if resp.status_code == 404 and ignore_404:
                return resp

            resp.raise_for_status()
            return resp

    @staticmethod
    def _next_link(resp: requests.Response) -> Optional[str]:
        """Extract the rel=next URL from an RFC5988 Link header, if present."""
        header = resp.headers.get("Link")
        if not header:
            return None
        for part in header.split(","):
            if 'rel="next"' in part:
                start = part.find("<") + 1
                end = part.find(">")
                if 0 < start < end:
                    return part[start:end]
        return None

    def _display_name(self, person_id: str, fallback_email: str) -> str:
        """Resolve a display name, caching by personId across runs."""
        if person_id in self.people_cache:
            return self.people_cache[person_id]
        name = fallback_email
        try:
            resp = self._request(f"{BASE_URL}/people/{person_id}", ignore_404=True)
            if resp.status_code == 200:
                name = resp.json().get("displayName") or fallback_email
        except Exception:
            pass
        self.people_cache[person_id] = name
        self._atomic_write_json(self.people_cache_file, self.people_cache)
        return name

    def _other_person_id(self, room_id: str) -> Optional[str]:
        """The other party in a 1:1, needed for the /messages/direct endpoint."""
        resp = self._request(f"{BASE_URL}/memberships", params={"roomId": room_id})
        for m in resp.json().get("items", []):
            if (m.get("personEmail") or "").lower() != self.my_email:
                return m.get("personId")
        return None

    # ---- main flow -------------------------------------------------------- #

    def run(self):
        print(f"Window {self.window_label}: "
              f"{self.window_start_utc.isoformat()} .. {self.window_end_utc.isoformat()} (UTC)")
        rooms = self._list_active_rooms()
        print(f"{len(rooms)} room(s) active in window.")

        for room in rooms:
            if room["id"] in self.state["rooms_done"]:
                continue
            self._process_room(room)
            self._mark_room_done(room["id"])

        self._clear_state()
        print(f"\nDone. Day files written under {DATA_DIR}/")

    def _list_active_rooms(self) -> List[dict]:
        """
        Rooms whose lastActivity falls within the window. Sorted by lastactivity
        (newest first), so once we pass the window start we can stop paging.
        """
        active = []
        url = f"{BASE_URL}/rooms"
        params = {"sortBy": "lastactivity", "max": 100}
        stop = False
        while url and not stop:
            resp = self._request(url, params=params)
            for room in resp.json().get("items", []):
                last = self._parse_utc(room["lastActivity"])
                if last < self.window_start_utc:
                    stop = True
                    break
                active.append(room)
            url = None if stop else self._next_link(resp)
            params = None  # subsequent pages carry params in the Link URL
        return active

    def _process_room(self, room: dict):
        room_id = room["id"]
        room_title = room.get("title", "(untitled)")
        room_type = room.get("type", "group")
        print(f"  {room_title} ({room_type})")

        if room_type == "group":
            url = f"{BASE_URL}/messages"
            params = {"roomId": room_id, "max": 100}
        else:
            other = self._other_person_id(room_id)
            if not other:
                print("    skip: could not identify the other party")
                return
            url = f"{BASE_URL}/messages/direct"
            params = {"personId": other, "max": 100}

        # Backward paging from the end of the window until we cross its start.
        params["before"] = self.window_end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        collected: List[dict] = []
        while url:
            resp = self._request(url, params=params)
            items = resp.json().get("items", [])
            if not items:
                break

            reached_start = False
            for msg in items:
                if self._parse_utc(msg["created"]) < self.window_start_utc:
                    reached_start = True
                    break
                collected.append(msg)

            if reached_start:
                break
            url = self._next_link(resp)
            params = None

        if collected:
            self._filter_and_persist(room_id, room_title, room_type, collected)

    def _filter_and_persist(self, room_id: str, room_title: str, room_type: str,
                            messages: List[dict]):
        """
        Bucket by local date; keep only buckets where I posted; within each kept
        bucket keep my messages plus up to 2 messages immediately preceding each
        of mine (for context). Append results to the per-day file.
        """
        messages.sort(key=lambda m: self._parse_utc(m["created"]))  # oldest first

        buckets: Dict[str, List[dict]] = defaultdict(list)
        i_posted: Dict[str, bool] = defaultdict(bool)
        for msg in messages:
            local_date = self._parse_utc(msg["created"]).astimezone(self.tz).date().isoformat()
            buckets[local_date].append(msg)
            if (msg.get("personEmail") or "").lower() == self.my_email:
                i_posted[local_date] = True

        for local_date, msgs in buckets.items():
            if not i_posted[local_date]:
                continue

            kept: List[dict] = []
            context: List[dict] = []
            for msg in msgs:
                if (msg.get("personEmail") or "").lower() == self.my_email:
                    kept.extend(context)   # flush up-to-2 preceding others
                    context.clear()
                    kept.append(msg)
                else:
                    context.append(msg)
                    context[:] = context[-2:]

            seen = set()
            with open(self._day_file(local_date), "a", encoding="utf-8") as f:
                for msg in kept:
                    if msg["id"] in seen:
                        continue
                    seen.add(msg["id"])
                    f.write(json.dumps(self._record(msg, room_id, room_title, room_type),
                                       ensure_ascii=False) + "\n")

    def _record(self, msg: dict, room_id: str, room_title: str, room_type: str) -> dict:
        """Full payload minus styling/attachments, plus denormalized context."""
        rec = {k: v for k, v in msg.items() if k not in DROP_KEYS}
        rec["roomId"] = msg.get("roomId", room_id)
        rec["roomTitle"] = room_title
        rec["roomType"] = room_type
        rec["personDisplayName"] = self._display_name(
            msg["personId"], msg.get("personEmail", "unknown"))
        # We don't save attachments, but leave a lightweight breadcrumb that a
        # file was shared (often the only content when text is empty).
        files = msg.get("files")
        if files:
            rec["sharedFiles"] = len(files)
        return rec


# --------------------------------------------------------------------------- #
# Window parsing / CLI
# --------------------------------------------------------------------------- #

def month_window(month: str, tz: ZoneInfo):
    year, mon = map(int, month.split("-"))
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=tz)
    return month, start, end


def day_window(day: str, tz: ZoneInfo):
    year, mon, d = map(int, day.split("-"))
    start = datetime(year, mon, d, tzinfo=tz)
    return day, start, start + timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch your Webex message activity into data/ (grouped by local day).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--month", help="Target month, YYYY-MM (default: current month)")
    group.add_argument("--day", help="Target day, YYYY-MM-DD (handy for testing)")
    args = parser.parse_args()

    settings = load_settings()
    token = load_token()
    tz = ZoneInfo(settings["timezone"])

    if args.day:
        label, start, end = day_window(args.day, tz)
    elif args.month:
        label, start, end = month_window(args.month, tz)
    else:
        label = datetime.now(tz).strftime("%Y-%m")
        label, start, end = month_window(label, tz)
        print(f"No --month/--day given; defaulting to current month {label}.")

    WebexFetcher(
        token=token,
        my_email=settings["email"],
        tz_str=settings["timezone"],
        window_label=label,
        local_start=start,
        local_end=end,
    ).run()


if __name__ == "__main__":
    main()
