import time
import random
import requests
import hashlib
import json
import os
import queue
import threading
import concurrent.futures
import webbrowser
from collections import deque, OrderedDict
from threading import Timer, Lock, Thread
from requests.adapters import HTTPAdapter
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)

# ---------------------------------------------------------------------------
# DEFAULT SKIN HASHES  (sha256 of Steve / Alex default skin PNGs)
# ---------------------------------------------------------------------------
DEFAULT_SKIN_HASHES = {
    "a5236c1849fe26f9ccd4969a2accafdec67aa3035ff9280b4a8a1be77b2077ad",
    "a39d5956897edd48d0e0fc0df3f820a3ec54afce4ecde193c6a8ea36af60f549",
    "80c5b58f84c3457b5774ffd769dceef45546572cca4c45531624381960e29ca7",
    "e8db37b0d619d7c59288bd8e8f4ca1f46cd908ed375203eae27a63106530aa4d",
    "bb8f45f40c53e00fc5899ff43424ae7180eb5cd0031844a6b8adf32eb8c10f7c",
}

# ---------------------------------------------------------------------------
# HTTP SESSION  — connection pool, keep-alive
# ---------------------------------------------------------------------------
http_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=0)
http_session.mount("https://", _adapter)
http_session.mount("http://", _adapter)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# TOKEN BUCKET RATE LIMITER
# Keeps us under Pika's rate limit threshold. 10 req/s = 600/min sustained.
# ---------------------------------------------------------------------------
class RateLimiter:
    """Thread-safe token bucket. `rate` = max calls per second."""

    def __init__(self, rate: float):
        self._rate      = rate
        self._tokens    = float(rate)
        self._last      = time.monotonic()
        self._lock      = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now     = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
                self._last   = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            # not enough tokens yet — sleep a proportional slice and retry
            time.sleep(1.0 / self._rate)


_pika_api_rl   = RateLimiter(rate=10)   # stats.pika-network.net   — 10 req/s
_mojang_rl     = RateLimiter(rate=5)    # api.mojang.com            — 5 req/s
_skin_rl       = RateLimiter(rate=8)    # mc-heads.net              — 8 req/s


# ---------------------------------------------------------------------------
# IN-MEMORY PROFILE CACHE  — LRU, 10 000 entries, 20-minute TTL
# Prevents double-fetching the same username within a single run.
# ---------------------------------------------------------------------------
CACHE_LOCK = Lock()
CACHE_TTL  = 600        # 10 min (was 20)
CACHE_MAX  = 2_000      # was 10,000 — saves ~80MB RAM
_PROFILE_CACHE: OrderedDict = OrderedDict()


def _cache_get(key: str):
    with CACHE_LOCK:
        if key not in _PROFILE_CACHE:
            return None
        data, ts = _PROFILE_CACHE[key]
        if time.time() - ts > CACHE_TTL:
            del _PROFILE_CACHE[key]
            return None
        _PROFILE_CACHE.move_to_end(key)
        return data


def _cache_set(key: str, data):
    with CACHE_LOCK:
        _PROFILE_CACHE[key] = (data, time.time())
        _PROFILE_CACHE.move_to_end(key)
        while len(_PROFILE_CACHE) > CACHE_MAX:
            _PROFILE_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# FILE PATHS
# On Render, set DATA_DIR env var to your persistent disk mount path (e.g. /data).
# Locally it defaults to the directory containing app.py.
# ---------------------------------------------------------------------------
_HERE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.environ.get("DATA_DIR", _HERE)
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE     = os.path.join(DATA_DIR, "crawl_state.json")
SEEN_FILE      = os.path.join(DATA_DIR, "seen_titans.json")
PCACHE_FILE    = os.path.join(DATA_DIR, "player_cache.json")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
TITANS_FILE    = os.path.join(DATA_DIR, "titans.json")

# ---------------------------------------------------------------------------
# SEEN LIST
# ---------------------------------------------------------------------------
SEEN_LOCK = Lock()


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(u.lower() for u in json.load(f).get("seen", []))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen": sorted(seen)}, f, separators=(",", ":"))
    except Exception as e:
        print(f"[SEEN] Save failed: {e}")


SEEN_TITANS: set = load_seen()

# ---------------------------------------------------------------------------
# PLAYER CACHE  — persistent, 7-day TTL.
#
# Stores the FULL profile response so on resume we never re-fetch a fresh
# account, yet can still expand the BFS via their stored friends list.
#
# Schema per entry:
#   {
#     "profile":      <full Pika API profile dict>,
#     "hasCustomSkin": bool,
#     "lastChecked":  float (unix ts)
#   }
# ---------------------------------------------------------------------------
PCACHE_TTL            = 7 * 24 * 3600   # 7 days
PCACHE_LOCK           = Lock()
_PLAYER_CACHE: dict   = {}
_PCACHE_DIRTY         = False
_PCACHE_FLUSH_INTERVAL = 30              # flush to disk every 30 s if dirty


def _load_player_cache():
    global _PLAYER_CACHE
    if os.path.exists(PCACHE_FILE):
        try:
            with open(PCACHE_FILE, "r", encoding="utf-8") as f:
                _PLAYER_CACHE = json.load(f)
        except Exception:
            _PLAYER_CACHE = {}
    else:
        _PLAYER_CACHE = {}


def _save_player_cache():
    global _PCACHE_DIRTY
    try:
        with PCACHE_LOCK:
            snapshot       = dict(_PLAYER_CACHE)
            _PCACHE_DIRTY  = False
        with open(PCACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, separators=(",", ":"))
    except Exception as e:
        print(f"[PCACHE] Save failed: {e}")


def _pcache_get(username_lower: str):
    """Return cached entry dict if fresh (within TTL), else None."""
    with PCACHE_LOCK:
        entry = _PLAYER_CACHE.get(username_lower)
    if not entry:
        return None
    if time.time() - entry.get("lastChecked", 0) > PCACHE_TTL:
        return None
    return entry


def _pcache_set(username_lower: str, profile: dict, has_custom_skin: bool):
    """Store full profile + skin status in persistent cache."""
    global _PCACHE_DIRTY
    with PCACHE_LOCK:
        _PLAYER_CACHE[username_lower] = {
            "profile":       profile,
            "hasCustomSkin": has_custom_skin,
            "lastChecked":   time.time(),
        }
        _PCACHE_DIRTY = True


def _pcache_flush_loop():
    """Background thread: flush to disk every 30 s when dirty."""
    while True:
        time.sleep(_PCACHE_FLUSH_INTERVAL)
        if _PCACHE_DIRTY:
            _save_player_cache()


_load_player_cache()
Thread(target=_pcache_flush_loop, daemon=True).start()

# ---------------------------------------------------------------------------
# WATCHLIST  — right rank, name currently taken on Mojang
# ---------------------------------------------------------------------------
WATCHLIST_LOCK = Lock()
_WATCHLIST: dict = {}


def _load_watchlist():
    global _WATCHLIST
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                _WATCHLIST = json.load(f)
        except Exception:
            _WATCHLIST = {}
    else:
        _WATCHLIST = {}


def _save_watchlist():
    try:
        with WATCHLIST_LOCK:
            snapshot = dict(_WATCHLIST)
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, separators=(",", ":"))
    except Exception as e:
        print(f"[WATCHLIST] Save failed: {e}")


def _watchlist_add(username: str, ranks: list):
    key = username.lower()
    with WATCHLIST_LOCK:
        if key not in _WATCHLIST:
            _WATCHLIST[key] = {
                "username": username,
                "ranks":    ranks,
                "addedAt":  time.time(),
            }
    _save_watchlist()


def _watchlist_remove(username_lower: str):
    with WATCHLIST_LOCK:
        _WATCHLIST.pop(username_lower, None)
    _save_watchlist()


_load_watchlist()

# ---------------------------------------------------------------------------
# TITANS PERSISTENT STORE  — append-only across all crawl sessions.
#
# Starting a new crawl with a different seed NEVER deletes these.
# The only way to wipe them is the explicit "Clear Results" action.
#
# Format: list of titan entry dicts (same shape as crawler.titans).
# ---------------------------------------------------------------------------
TITANS_LOCK = Lock()


def _load_titans() -> list:
    if os.path.exists(TITANS_FILE):
        try:
            with open(TITANS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # One-time migration: if old crawl_state.json has titans, import them
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            migrated = old.get("titans", [])
            if migrated:
                print(f"[TITANS] Migrating {len(migrated)} titans from crawl_state.json")
                _save_titans(migrated)
                return migrated
        except Exception:
            pass
    return []


def _save_titans(titans: list):
    try:
        with open(TITANS_FILE, "w", encoding="utf-8") as f:
            json.dump(titans, f, separators=(",", ":"))
    except Exception as e:
        print(f"[TITANS] Save failed: {e}")


def _titans_add(entry: dict, titans: list) -> bool:
    """
    Add entry to the list if not already present (by username, case-insensitive).
    Returns True if it was actually added.
    Mutates titans in-place and persists to disk.
    """
    key = entry["username"].lower()
    for t in titans:
        if t["username"].lower() == key:
            return False
    titans.append(entry)
    _save_titans(titans)
    return True


# ---------------------------------------------------------------------------
# CRAWL STATE
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "visited": [], "queue": [], "scanned": 0,
        "seed": None, "targetRanks": ["Titan"], "targetRank": "Titan",
    }


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, separators=(",", ":"))
    except Exception as e:
        print(f"[STATE] Save failed: {e}")


# ---------------------------------------------------------------------------
# RANK MATCHING
# ---------------------------------------------------------------------------
DEFAULT_RANK = "Titan"


def rank_matches(ranks: list, target_rank) -> bool:
    if isinstance(target_rank, str):
        needles = {target_rank.lower().strip()}
    else:
        needles = {r.lower().strip() for r in target_rank if r}
    if not needles:
        return False
    return any(r.get("displayName", "").lower().strip() in needles for r in ranks)


# ---------------------------------------------------------------------------
# NETWORK HELPERS
# ---------------------------------------------------------------------------

def fetch_profile_with_retry(username: str, max_retries: int = 4):
    """
    Fetch a player's Pika profile.

    Priority order:
      1. In-memory 20-min cache  (no API call)
      2. Persistent 7-day player cache  (no API call — returns stored profile)
      3. Live Pika API call with exponential backoff on 429

    Returns the profile dict or None if not found.
    """
    username_lower = username.strip().lower()
    if not username_lower:
        return None

    # 1. In-memory cache
    cached = _cache_get(username_lower)
    if cached is not None:
        return cached

    # 2. Persistent player cache — return stored full profile
    pcached = _pcache_get(username_lower)
    if pcached is not None:
        profile = pcached.get("profile")
        if profile:
            _cache_set(username_lower, profile)   # also warm in-memory cache
            return profile

    # 3. Live API call
    url = f"https://stats.pika-network.net/api/profile/{username}"
    for attempt in range(max_retries):
        _pika_api_rl.acquire()                     # respect rate limit
        try:
            r = http_session.get(url, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                data = r.json()
                _cache_set(username_lower, data)
                return data
            elif r.status_code == 404:
                return None
            elif r.status_code == 429:
                # Exponential backoff with jitter: 2s, 4s, 8s …
                wait = (2 ** (attempt + 1)) + random.uniform(0, 1)
                print(f"[API] 429 rate-limited for {username}, waiting {wait:.1f}s (attempt {attempt+1})")
                time.sleep(wait)
            else:
                time.sleep(0.3 + attempt * 0.2)
        except requests.RequestException as e:
            wait = 0.5 * (attempt + 1)
            print(f"[API] Request error for {username}: {e}, retry in {wait}s")
            time.sleep(wait)
    return None


def check_has_custom_skin(username: str) -> bool:
    """Returns True if the player has a custom (non-default) skin."""
    _skin_rl.acquire()
    try:
        r = http_session.get(
            f"https://mc-heads.net/skin/{username}",
            headers=HEADERS, timeout=6,
        )
        if r.status_code == 200:
            return hashlib.sha256(r.content).hexdigest() not in DEFAULT_SKIN_HASHES
    except Exception:
        pass
    return False   # default: assume no custom skin (conservative — will skip)


def check_name_available(username: str) -> str:
    """
    Check Mojang name availability.
    Returns: 'free', 'taken', or 'unknown'
    """
    _mojang_rl.acquire()
    for attempt in range(3):
        try:
            r = http_session.get(
                f"https://api.mojang.com/users/profiles/minecraft/{username}",
                headers={"Accept": "application/json"},
                timeout=6,
            )
            if r.status_code == 404:
                return "free"
            if r.status_code == 200:
                return "taken"
            if r.status_code == 429:
                wait = (2 ** (attempt + 1)) + random.uniform(0, 0.5)
                print(f"[MOJANG] 429 for {username}, waiting {wait:.1f}s")
                time.sleep(wait)
            else:
                return "unknown"
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return "unknown"


def pika_check_account_with_reason(username: str) -> tuple:
    """
    Spawn pika_check.js to bot-connect and determine premium/cracked/banned.
    Returns (result_str, reason_str).
    """
    import subprocess
    script = os.path.join(_HERE, "pika_check.js")
    try:
        proc = subprocess.run(
            ["node", script, username],
            capture_output=True, text=True, timeout=30,
            cwd=_HERE,
        )
        stdout = proc.stdout.strip()
        if not stdout:
            return "unknown", "No output from bot"
        data = json.loads(stdout.splitlines()[-1])
        return data.get("result", "unknown"), data.get("reason", "")
    except Exception as e:
        return "unknown", str(e)


# ---------------------------------------------------------------------------
# HARDCODED CONFIG — seeds, ranks, webhooks
# ---------------------------------------------------------------------------
AUTO_SEEDS = ["SUGARCANDYSC", "Honey1X", "Nixo"]
AUTO_RANKS = ["Baron", "Immortal", "Boss", "Titan", "Omega", "Noble", "Emperor", "Monarch", "Sigma"]

DISCORD_WEBHOOK_ALERT  = os.environ.get(
    "DISCORD_WEBHOOK_ALERT",
    "https://discord.com/api/webhooks/1550763498110189709/y-1OEjWiHThykQY89ey7U8zGfPcEMUmw1kBezuYe7ZJ6SVoGsYaDSLjewbi7I3-5uD3Q"
)
DISCORD_WEBHOOK_DIGEST = os.environ.get(
    "DISCORD_WEBHOOK_DIGEST",
    "https://discord.com/api/webhooks/1551635933177061376/Y337ctcnLvLw0K4DYgljaP-NL2ri9KEi_2BYwOdnBYEoZh6G5zyldbaOZac_Z87kIIh8"
)

# Keep backward compat
DISCORD_WEBHOOK = DISCORD_WEBHOOK_ALERT

# ID of the last digest message (so we can edit it instead of posting new ones)
_digest_message_id: str | None = None
_digest_lock = Lock()


import datetime as _dt

NAME_DROP_DAYS = 37


def cleanstr(s: str) -> str:
    import re
    return re.sub(r'[§&][0-9a-fk-orA-FK-OR]', '', s or '').strip()


def get_exact_drop_time(username: str) -> dict:
    """
    Returns { dropsAt: datetime|None, changedAt: datetime|None,
               available: bool, source: str }
    Priority: Laby.net history → NameMC scrape → Mojang 404 check
    """
    result = {"dropsAt": None, "changedAt": None, "available": False, "source": "none"}

    # Get UUID first (needed for Laby)
    uuid = None
    try:
        mj = http_session.get(
            f"https://api.mojang.com/users/profiles/minecraft/{username}",
            headers={"Accept": "application/json"}, timeout=6,
        )
        if mj.status_code == 200:
            uuid = mj.json().get("id", "")
        # Don't return early on 404 — check Laby/NameMC first for drop history
    except Exception:
        pass

    # Try Laby.net first (most accurate)
    if uuid:
        uuid_fmt = f"{uuid[0:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
        try:
            lb = http_session.get(
                f"https://laby.net/api/v3/user/{uuid_fmt}/profile",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=8,
            )
            if lb.status_code == 200:
                for hist_entry in (lb.json().get("username_history") or []):
                    ts = hist_entry.get("changed_at")
                    if ts:
                        changed = _dt.datetime.fromtimestamp(ts / 1000, tz=_dt.timezone.utc)
                        drops   = changed + _dt.timedelta(days=NAME_DROP_DAYS)
                        result["changedAt"] = changed
                        result["dropsAt"]   = drops
                        result["source"]    = "laby"
                        # Only mark available if drop date has passed
                        if drops <= _dt.datetime.now(_dt.timezone.utc):
                            result["available"] = True
                        return result
        except Exception:
            pass

    # NameMC fallback
    try:
        import re
        nm = http_session.get(
            f"https://namemc.com/name/{username}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"}, timeout=8,
        )
        if nm.status_code == 200:
            html  = nm.text
            times = re.findall(r'<time[^>]+datetime=["\']([^"\']+)["\']', html, re.I)
            if times:
                try:
                    changed = _dt.datetime.fromisoformat(times[0].replace("Z", "+00:00"))
                    drops   = changed + _dt.timedelta(days=NAME_DROP_DAYS)
                    result["changedAt"] = changed
                    result["dropsAt"]   = drops
                    result["source"]    = "namemc"
                    if drops <= _dt.datetime.now(_dt.timezone.utc):
                        result["available"] = True
                    return result
                except Exception:
                    pass
            # NameMC explicitly says available with no history = never registered
            if re.search(r'status-available|>Available<', html, re.I):
                result["available"] = True
                result["source"]    = "namemc"
                return result
    except Exception:
        pass

    # Last resort: Mojang 404 = name is free but we don't know why
    # Don't mark as "available now" — show unknown drop time instead
    # (could be in 37-day hold with no tracked history)
    return result


def _fmt_drop_str(drop_dt, available: bool) -> str:
    if available:
        return "🟢 Available now"
    if drop_dt is None:
        return "❓ Unknown"
    if isinstance(drop_dt, str):
        try:
            drop_dt = _dt.datetime.fromisoformat(drop_dt.replace("Z", "+00:00"))
        except Exception:
            return "❓ Unknown"
    now  = _dt.datetime.now(_dt.timezone.utc)
    diff = (drop_dt - now).total_seconds()
    date_str = drop_dt.strftime("%b %d, %Y at %H:%M UTC")
    if diff <= 0:
        return f"🟢 Claimable now! ({date_str})"
    d = int(diff // 86400); h = int((diff % 86400) // 3600); m = int((diff % 3600) // 60)
    countdown = f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m"
    return f"⏳ {date_str} ({countdown})"


def _send_discord_alert(entry: dict, drop_info: dict | None = None):
    """Immediate alert embed → DISCORD_WEBHOOK_ALERT."""
    if not DISCORD_WEBHOOK_ALERT:
        return

    username   = entry.get("username", "?")
    ranks      = entry.get("ranks", [])
    # Show the player's actual ranks, not the search targets
    rank_names = ", ".join(cleanstr(r.get("displayName", "")) for r in ranks) or "Unknown"
    last_seen  = entry.get("lastSeen")
    clan       = (entry.get("clan") or {}).get("name", "")

    if last_seen:
        try:
            dt     = _dt.datetime.fromtimestamp(last_seen / 1000, tz=_dt.timezone.utc)
            unix_ts = int(dt.timestamp())
            ls_str = f"<t:{unix_ts}:R>"  # Discord relative timestamp e.g. "25 days ago"
        except Exception:
            ls_str = "Unknown"
    else:
        ls_str = "⚠️ Never played on Pika"

    drop_dt    = drop_info.get("dropsAt")    if drop_info else None
    changed_dt = drop_info.get("changedAt")  if drop_info else None
    available  = drop_info.get("available", False) if drop_info else False
    source     = drop_info.get("source", "none")   if drop_info else "none"

    # Format drop time with Discord timestamp
    if available or (drop_dt and drop_dt <= _dt.datetime.now(_dt.timezone.utc)):
        drop_str    = "🟢 **CLAIMABLE NOW**"
        changed_str = changed_dt.strftime("%b %d, %Y at %H:%M UTC") if changed_dt else "–"
    elif drop_dt:
        unix_drop   = int(drop_dt.timestamp())
        drop_str    = f"<t:{unix_drop}:F>\n`<t:{unix_drop}:R>`"
        changed_str = changed_dt.strftime("%b %d, %Y at %H:%M UTC") if changed_dt else "–"
    else:
        drop_str    = f"❓ Unknown — [Check NameMC](https://namemc.com/name/{username})"
        changed_str = changed_dt.strftime("%b %d, %Y at %H:%M UTC") if changed_dt else "–"

    fields = [
        {"name": "🏆 Rank",         "value": rank_names,  "inline": True},
        {"name": "✅ Pika",         "value": "Premium",   "inline": True},
        {"name": "👁 Last Seen",    "value": ls_str,      "inline": True},
        {"name": "🕒 Name Changed", "value": changed_str, "inline": True},
        {"name": "⏳ Claimable",    "value": drop_str,    "inline": True},
    ]
    if clan:
        fields.append({"name": "🏠 Clan", "value": clan, "inline": True})

    embed = {
        "embeds": [{
            "title":     f"⚡ `{username}`",
            "url":       f"https://namemc.com/name/{username}",
            "color":     0xa78bfa,
            "fields":    fields,
            "footer":    {"text": f"Pika Rank Finder • {source}"},
            "thumbnail": {"url": f"https://mc-heads.net/avatar/{username}/64"},
        }],
        "components": [{
            "type": 1,
            "components": [
                {"type": 2, "style": 5, "label": "🏷 NameMC",    "url": f"https://namemc.com/name/{username}"},
                {"type": 2, "style": 5, "label": "📊 Pika Stats", "url": f"https://stats.pika-network.net/player/{username}"},
            ],
        }],
    }

    def _fire():
        try:
            http_session.post(DISCORD_WEBHOOK_ALERT, json=embed, timeout=8)
        except Exception as e:
            print(f"[WEBHOOK-ALERT] {username}: {e}")

    Thread(target=_fire, daemon=True).start()
    _update_digest()


# digest message id — load from file so it survives restarts
_digest_lock = Lock()

def _load_digest_id() -> str | None:
    try:
        p = os.path.join(DATA_DIR, "digest_msg_id.txt")
        if os.path.exists(p):
            return open(p).read().strip() or None
    except Exception:
        pass
    return None

def _save_digest_id(mid: str):
    try:
        p = os.path.join(DATA_DIR, "digest_msg_id.txt")
        open(p, "w").write(mid)
    except Exception:
        pass

_digest_message_id: str | None = _load_digest_id()


def _update_digest():
    """Post or edit the sorted drop-date digest → DISCORD_WEBHOOK_DIGEST."""
    global _digest_message_id
    if not DISCORD_WEBHOOK_DIGEST:
        return

    def _build():
        global _digest_message_id
        with _digest_lock:
            titans = [t for t in crawler.titans if t.get("pikaResult") == "premium"]
            if not titans:
                return

            def sort_key(t):
                di = t.get("dropInfo") or {}
                if di.get("available"):
                    return (0, _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
                drop = di.get("dropsAt")
                if drop:
                    if isinstance(drop, str):
                        try:
                            drop = _dt.datetime.fromisoformat(drop.replace("Z", "+00:00"))
                        except Exception:
                            drop = None
                    if drop:
                        return (1, drop)
                return (2, _dt.datetime.max.replace(tzinfo=_dt.timezone.utc))

            titans.sort(key=sort_key)
            now = _dt.datetime.now(_dt.timezone.utc)

            # Build one embed per account (max 10 embeds per message)
            embeds = []
            for t in titans[:10]:
                uname       = t.get("username", "?")
                actual_ranks = ", ".join(
                    cleanstr(r.get("displayName", "")) for r in t.get("ranks", [])
                ) or "?"
                last_seen   = t.get("lastSeen")
                clan        = (t.get("clan") or {}).get("name", "")
                di          = t.get("dropInfo") or {}
                drop_dt     = di.get("dropsAt")
                changed_dt  = di.get("changedAt")
                available   = di.get("available", False)
                source      = di.get("source", "none")

                if isinstance(drop_dt, str):
                    try:
                        drop_dt = _dt.datetime.fromisoformat(drop_dt.replace("Z", "+00:00"))
                    except Exception:
                        drop_dt = None
                if isinstance(changed_dt, str):
                    try:
                        changed_dt = _dt.datetime.fromisoformat(changed_dt.replace("Z", "+00:00"))
                    except Exception:
                        changed_dt = None

                # Drop status
                if available or (drop_dt and (drop_dt - now).total_seconds() <= 0):
                    drop_str  = "🟢 **CLAIMABLE NOW**"
                    color     = 0x10b981   # green
                elif drop_dt:
                    diff      = (drop_dt - now).total_seconds()
                    d = int(diff // 86400); h = int((diff % 86400) // 3600); m = int((diff % 3600) // 60)
                    date_s    = drop_dt.strftime("%b %d, %Y at %H:%M UTC")
                    countdown = f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m"
                    drop_str  = f"⏳ {date_s}\n`{countdown} remaining`"
                    color     = 0xa78bfa   # purple
                else:
                    drop_str  = f"❓ Unknown — [Check NameMC](https://namemc.com/name/{uname})"
                    color     = 0x64748b   # grey

                changed_str = changed_dt.strftime("%b %d, %Y at %H:%M UTC") if changed_dt else "–"

                if last_seen:
                    try:
                        dt     = _dt.datetime.fromtimestamp(last_seen / 1000, tz=_dt.timezone.utc)
                        diff_s = (now - dt).total_seconds()
                        d2 = int(diff_s // 86400); h2 = int((diff_s % 86400) // 3600)
                        ls_str = f"{d2}d {h2}h ago" if d2 else f"{h2}h ago" if h2 else "Just now"
                    except Exception:
                        ls_str = "Unknown"
                else:
                    ls_str = "⚠️ Never played"

                fields = [
                    {"name": "🏆 Rank",         "value": actual_ranks, "inline": True},
                    {"name": "👁 Last Seen",     "value": ls_str,       "inline": True},
                ]
                if clan:
                    fields.append({"name": "🏠 Clan", "value": clan, "inline": True})
                fields += [
                    {"name": "🕒 Name Changed",  "value": changed_str,  "inline": True},
                    {"name": "⏳ Claimable",      "value": drop_str,     "inline": False},
                ]

                embeds.append({
                    "title":     f"⚡ {uname}",
                    "url":       f"https://namemc.com/name/{uname}",
                    "color":     color,
                    "fields":    fields,
                    "thumbnail": {"url": f"https://mc-heads.net/avatar/{uname}/64"},
                    "footer":    {"text": f"source: {source}"},
                })

            # Header embed
            header = {
                "title":       "🏆 Premium Accounts — Sorted by Drop Date",
                "description": f"**{len(titans)} accounts found** • Updated <t:{int(now.timestamp())}:R>",
                "color":       0x1e293b,
            }
            all_embeds = [header] + embeds

            payload = {"embeds": all_embeds, "flags": 4}  # flags=4 suppresses link previews

            try:
                if _digest_message_id:
                    r = http_session.patch(
                        f"{DISCORD_WEBHOOK_DIGEST}/messages/{_digest_message_id}",
                        json=payload, timeout=8,
                    )
                    if r.status_code in (200, 204):
                        return
                    _digest_message_id = None

                r = http_session.post(
                    f"{DISCORD_WEBHOOK_DIGEST}?wait=true",
                    json=payload, timeout=8,
                )
                if r.status_code in (200, 204):
                    try:
                        mid = r.json().get("id")
                        if mid:
                            _digest_message_id = mid
                            _save_digest_id(mid)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[WEBHOOK-DIGEST] {e}")

    Thread(target=_build, daemon=True).start()


# ---------------------------------------------------------------------------
# CRAWL ENGINE
# ---------------------------------------------------------------------------
class CrawlEngine:

    # ---------------------------------------------------------------- init
    def __init__(self):
        self._lock      = Lock()
        self._running   = False

        s = load_state()
        # Titans come from their own persistent file — not crawl_state.json
        self.titans: list       = _load_titans()
        self.scanned: int       = s.get("scanned", 0)
        self.seed: str | None   = s.get("seed")

        saved = s.get("targetRanks") or s.get("targetRank") or DEFAULT_RANK
        if isinstance(saved, str):
            self.target_ranks: list = [r.strip() for r in saved.split(",") if r.strip()]
        else:
            self.target_ranks: list = list(saved)

        self._queue: deque      = deque(s.get("queue", []))
        self._queued_lower: set = {u.lower() for u in self._queue}

        # visited: union of saved state + every key in the player cache.
        # This prevents re-fetching any account we have a fresh cache for.
        saved_visited = set(s.get("visited", []))
        with PCACHE_LOCK:
            cache_keys = set(_PLAYER_CACHE.keys())
        # Only pre-populate visited from cache entries that are still fresh
        now = time.time()
        fresh_cache = {
            k for k, v in _PLAYER_CACHE.items()
            if now - v.get("lastChecked", 0) <= PCACHE_TTL
        }
        self.visited: set = saved_visited | fresh_cache

        self._sse_clients: list[queue.Queue] = []
        self._clients_lock = Lock()

        # 10 workers is the sweet spot: enough parallelism without hammering.
        # The rate limiter caps actual throughput, not the worker count.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

        self._pika_queue: queue.Queue = queue.Queue()
        # Single bot worker — sequential pika checks with a gap between them
        Thread(target=self._pika_worker, daemon=True).start()

        # Re-queue any titans that are still pending pika verification
        for t in self.titans:
            if t.get("pikaResult") in (None, "pending"):
                self._pika_queue.put((t["username"], t))

        self._pika_scan_running = False

    # ---------------------------------------------------------------- SSE
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._clients_lock:
            self._sse_clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._clients_lock:
            try:
                self._sse_clients.remove(q)
            except ValueError:
                pass

    def _broadcast(self, event: dict):
        msg  = f"data: {json.dumps(event)}\n\n"
        dead = []
        with self._clients_lock:
            for q in self._sse_clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._sse_clients.remove(q)
                except ValueError:
                    pass

    # -------------------------------------------------------------- control
    def start(self, seed: str | None = None, ranks=None):
        with self._lock:
            if ranks:
                if isinstance(ranks, str):
                    self.target_ranks = [r.strip() for r in ranks.split(",") if r.strip()]
                else:
                    self.target_ranks = [r.strip() for r in ranks if r]

            if seed:
                seed_lower = seed.strip().lower()
                # Reset BFS state only — titans are NEVER cleared here.
                # Previously found accounts stay in the results list.
                self._queue.clear()
                self._queued_lower.clear()
                self.scanned  = 0
                self.seed     = seed_lower
                self.visited  = set()
                self._queue.append(seed.strip())
                self._queued_lower.add(seed_lower)
                save_state(self._current_state())
                # Tell the client to reset only the progress counters, not the grid
                self._broadcast({"type": "bfs_reset", "seed": seed_lower})

            if self._running:
                return
            if not self._queue and not self.seed:
                return

            self._running = True
            Thread(target=self._crawl_loop, daemon=True).start()
            self._broadcast({
                "type": "status", "running": True,
                "targetRank":  self.target_ranks[0] if self.target_ranks else "",
                "targetRanks": self.target_ranks,
            })

    def stop(self):
        with self._lock:
            self._running = False
        self._broadcast({"type": "status", "running": False})

    def reset(self):
        with self._lock:
            self._running         = False
            self._pika_scan_running = False
            self._queue.clear()
            self._queued_lower.clear()
            self.titans.clear()
            self.scanned = 0
            self.seed    = None
            self.visited = set()
        while not self._pika_queue.empty():
            try:
                self._pika_queue.get_nowait()
                self._pika_queue.task_done()
            except Exception:
                break
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        if os.path.exists(TITANS_FILE):
            os.remove(TITANS_FILE)
        self._broadcast({"type": "reset", "seed": None})
        self._broadcast({"type": "status", "running": False})

    def clear_results(self):
        """Wipe found accounts only — BFS state (queue/visited) is untouched."""
        with self._lock:
            self.titans.clear()
        if os.path.exists(TITANS_FILE):
            os.remove(TITANS_FILE)
        self._broadcast({"type": "results_cleared"})

    def get_state(self) -> dict:
        with SEEN_LOCK:
            seen_copy = set(SEEN_TITANS)
        with PCACHE_LOCK:
            pcache_size = len(_PLAYER_CACHE)
        with WATCHLIST_LOCK:
            wl_size = len(_WATCHLIST)
        titans_tagged = [
            {**t, "alreadySeen": t["username"].lower() in seen_copy}
            for t in self.titans
        ]
        return {
            "running":          self._running,
            "pikaScanRunning":  self._pika_scan_running,
            "scanned":          self.scanned,
            "queued":           len(self._queue),
            "visited":          len(self.visited),
            "titanCount":       len(self.titans),
            "titans":           titans_tagged,
            "seed":             self.seed,
            "targetRank":       self.target_ranks[0] if self.target_ranks else "",
            "targetRanks":      self.target_ranks,
            "playerCacheSize":  pcache_size,
            "watchlistSize":    wl_size,
        }

    def _current_state(self) -> dict:
        return {
            "visited":    list(self.visited),
            "queue":      list(self._queue)[:5000],
            "scanned":    self.scanned,
            "seed":       self.seed,
            "targetRanks": self.target_ranks,
            "targetRank":  self.target_ranks[0] if self.target_ranks else DEFAULT_RANK,
        }

    # ---------------------------------------------------------- crawl loop
    def _crawl_loop(self):
        BATCH      = 15
        SAVE_EVERY = 100
        QUEUE_CAP  = 3_000
        try:
            self._crawl_loop_inner(BATCH, SAVE_EVERY, QUEUE_CAP)
        except Exception as e:
            import traceback
            print(f"[CRAWL] Fatal error: {e}")
            traceback.print_exc()
            self._running = False
            self._broadcast({"type": "status", "running": False,
                             "message": f"Crawl crashed: {e}"})
            save_state(self._current_state())

    def _crawl_loop_inner(self, BATCH: int, SAVE_EVERY: int, QUEUE_CAP: int = 3000):
        while self._running:
            # ── pull batch ──────────────────────────────────────────────────
            with self._lock:
                batch         = []   # usernames needing a live API call
                cache_hits    = []   # profiles resolved from player cache
                while self._queue and len(batch) < BATCH:
                    uname       = self._queue.popleft()
                    uname_lower = uname.lower()
                    self._queued_lower.discard(uname_lower)
                    if uname_lower in self.visited:
                        continue
                    self.visited.add(uname_lower)
                    # Cap visited set — rely on player cache for older entries
                    if len(self.visited) > 20_000:
                        # Trim oldest 5k — sets aren't ordered but this limits RAM
                        overflow = list(self.visited)[:5000]
                        for k in overflow:
                            self.visited.discard(k)

                    # Check persistent player cache BEFORE queueing an API call
                    pcached = _pcache_get(uname_lower)
                    if pcached is not None:
                        profile = pcached.get("profile")
                        if profile:
                            cache_hits.append((uname, profile, pcached.get("hasCustomSkin", True)))
                            continue   # no API call needed

                    batch.append(uname)

            if not batch and not cache_hits:
                time.sleep(1)
                if not self._queue:
                    self._running = False
                    self._broadcast({
                        "type": "status", "running": False,
                        "message": "Queue exhausted — all reachable players scanned.",
                    })
                    save_state(self._current_state())
                continue

            # ── live API calls for batch ────────────────────────────────────
            futures = {
                self._executor.submit(fetch_profile_with_retry, u): u
                for u in batch
            }
            profile_batch: dict = {}       # username -> profile
            for fut in concurrent.futures.as_completed(futures):
                uname = futures[fut]
                try:
                    prof = fut.result()
                    if prof:
                        profile_batch[uname] = prof
                except Exception:
                    pass

            # ── merge cache hits — expand BFS from their friends ────────────
            for uname, profile, has_skin in cache_hits:
                with self._lock:
                    for f in (profile.get("friends") or []):
                        fname = f.get("username", "").strip()
                        if not fname:
                            continue
                        fname_lower = fname.lower()
                        if fname_lower not in self.visited \
                                and fname_lower not in self._queued_lower \
                                and len(self._queue) < QUEUE_CAP:
                            self._queued_lower.add(fname_lower)
                            self._queue.append(fname)

            # ── enqueue new friends from live-fetched profiles ───────────────
            with self._lock:
                self.scanned += len(batch) + len(cache_hits)
                for prof in profile_batch.values():
                    for f in (prof.get("friends") or []):
                        fname = f.get("username", "").strip()
                        if not fname:
                            continue
                        fname_lower = fname.lower()
                        if fname_lower not in self.visited \
                                and fname_lower not in self._queued_lower \
                                and len(self._queue) < QUEUE_CAP:
                            self._queued_lower.add(fname_lower)
                            self._queue.append(fname)

            # ── store in persistent player cache (placeholder skin=True) ─────
            # Skin truth updated below if we actually check it.
            for uname, prof in profile_batch.items():
                if not _pcache_get(uname.lower()):
                    _pcache_set(uname.lower(), prof, True)

            # ── filter: rank match ───────────────────────────────────────────
            target_rk        = self.target_ranks
            rank_candidates  = [
                (u, p) for u, p in profile_batch.items()
                if rank_matches(p.get("ranks", []), target_rk)
            ]

            if rank_candidates:
                # ── name availability check (parallel, rate-limited) ─────────
                name_futs = {
                    self._executor.submit(check_name_available, u): (u, p)
                    for u, p in rank_candidates
                }
                name_free, name_taken_list = [], []
                for nf in concurrent.futures.as_completed(name_futs):
                    u, p = name_futs[nf]
                    try:
                        status = nf.result()
                    except Exception:
                        status = "unknown"
                    if status == "free":
                        name_free.append((u, p))
                    elif status == "taken":
                        _watchlist_add(p.get("username", u), p.get("ranks", []))
                        print(f"[WATCH] {u} added to watchlist (rank ✓, name taken)")

                if name_free:
                    # ── skin check (parallel, rate-limited) ──────────────────
                    skin_futs = {
                        self._executor.submit(check_has_custom_skin, u): (u, p)
                        for u, p in name_free
                    }
                    no_skin = []
                    for sf in concurrent.futures.as_completed(skin_futs):
                        u, p = skin_futs[sf]
                        try:
                            has_skin = sf.result()
                        except Exception:
                            has_skin = True
                        # Update persistent cache with real skin status
                        _pcache_set(u.lower(), p, has_skin)
                        if not has_skin:
                            no_skin.append((u, p))

                    # ── only no-default-skin accounts go to pika check ────────
                    for u, p in no_skin:
                        entry = {
                            "username":      p.get("username", u),
                            "lastSeen":      p.get("lastSeen"),
                            "ranks":         p.get("ranks", []),
                            "rank":          p.get("rank", {}),
                            "clan":          p.get("clan"),
                            "hasCustomSkin": False,
                            "pikaResult":    "pending",
                            "nameStatus":    "free",
                            "foundForRanks": list(target_rk),  # which ranks were active when found
                        }
                        with self._lock:
                            added = _titans_add(entry, self.titans)
                            if added:
                                ulow = entry["username"].lower()
                                with SEEN_LOCK:
                                    already_seen = ulow in SEEN_TITANS
                                self._broadcast({
                                    "type": "titan_found",
                                    "player": {**entry, "alreadySeen": already_seen},
                                    "scanned":      self.scanned,
                                    "queued":       len(self._queue),
                                    "titanCount":   len(self.titans),
                                    "watchlistSize": len(_WATCHLIST),
                                })
                                self._pika_queue.put((entry["username"], entry))

            self._broadcast({
                "type":          "progress",
                "scanned":       self.scanned,
                "queued":        len(self._queue),
                "visited":       len(self.visited),
                "titanCount":    len(self.titans),
                "watchlistSize": len(_WATCHLIST),
                "batch":         batch,
            })

            if self.scanned % SAVE_EVERY < BATCH:
                save_state(self._current_state())

        save_state(self._current_state())

    # -------------------------------------------------------- pika worker
    def _pika_worker(self):
        """
        Single-threaded pika bot worker.
        Sequential checks with a 3 s gap — avoids 'logging in too fast' kicks
        and keeps Pika's connection endpoint happy.
        """
        while True:
            try:
                username, entry = self._pika_queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                result, reason = pika_check_account_with_reason(username)
            except Exception as e:
                result, reason = "unknown", str(e)

            print(f"[PIKA] {username} -> {result}")

            with self._lock:
                for t in self.titans:
                    if t["username"].lower() == username.lower():
                        if result == "premium":
                            t["pikaResult"] = result
                            t["pikaReason"] = reason
                            _save_titans(self.titans)   # persist updated status
                        else:
                            # cracked / banned / unknown → remove from results
                            try:
                                self.titans.remove(t)
                                _save_titans(self.titans)
                            except ValueError:
                                pass
                        break

            # Fire Discord webhook for premium accounts
            if result == "premium":
                # Run drop time lookup in background — don't block the pika worker
                def _do_drop_and_alert(uname=username, ent=entry):
                    drop_info = get_exact_drop_time(uname)
                    with self._lock:
                        for t in self.titans:
                            if t["username"].lower() == uname.lower():
                                t["dropInfo"] = {
                                    "available": drop_info["available"],
                                    "source":    drop_info["source"],
                                    "dropsAt":   drop_info["dropsAt"].isoformat() if drop_info["dropsAt"] else None,
                                    "changedAt": drop_info["changedAt"].isoformat() if drop_info["changedAt"] else None,
                                }
                                break
                    _save_titans(self.titans)
                    _send_discord_alert(ent, drop_info)
                Thread(target=_do_drop_and_alert, daemon=True).start()

            # Look up the full entry to send to the client (for rank badges etc.)
            with self._lock:
                player_entry = next(
                    (t for t in self.titans if t["username"].lower() == username.lower()),
                    {"username": username, "ranks": [], "foundForRanks": [],
                     "rank": {}, "lastSeen": None, "clan": None,
                     "nameStatus": "free", "pikaResult": result},
                )

            self._broadcast({
                "type":       "pika_update",
                "username":   username,
                "pikaResult": result,
                "pikaReason": reason,
                "player":     player_entry,
            })
            self._pika_queue.task_done()

            # Gap between bot connections — reduces 'logging in too fast' kicks
            time.sleep(1.5)

    # --------------------------------------------------- manual pika scan
    def start_pika_scan(self, revisit=False):
        with self._lock:
            if self._pika_scan_running:
                return {"error": "Pika scan already running"}
            if self._running:
                return {"error": "Stop crawling first"}
            targets = [
                t["username"] for t in self.titans
                if revisit or t.get("pikaResult") in (None, "not_checked", "unknown", "pending")
            ]
            if not targets:
                return {"error": "No accounts to scan"}
            self._pika_scan_running = True
        Thread(target=self._pika_scan_loop, args=(targets,), daemon=True).start()
        self._broadcast({"type": "pika_scan_started", "total": len(targets)})
        return {"ok": True, "total": len(targets)}

    def stop_pika_scan(self):
        with self._lock:
            self._pika_scan_running = False
        self._broadcast({"type": "pika_scan_stopped"})

    def _pika_scan_loop(self, targets: list):
        total = len(targets)
        done  = 0
        for username in targets:
            if not self._pika_scan_running:
                break
            self._broadcast({
                "type": "pika_scan_progress",
                "username": username, "done": done, "total": total,
            })
            try:
                result, reason = pika_check_account_with_reason(username)
            except Exception as e:
                result, reason = "unknown", str(e)
            done += 1
            with self._lock:
                for t in self.titans:
                    if t["username"].lower() == username.lower():
                        if result == "premium":
                            t["pikaResult"] = result
                            t["pikaReason"] = reason
                            _save_titans(self.titans)
                        else:
                            try:
                                self.titans.remove(t)
                                _save_titans(self.titans)
                            except ValueError:
                                pass
                        break
                player_entry = next(
                    (t for t in self.titans if t["username"].lower() == username.lower()),
                    {"username": username, "ranks": [], "foundForRanks": [],
                     "rank": {}, "lastSeen": None, "clan": None,
                     "nameStatus": "free", "pikaResult": result},
                )
            self._broadcast({
                "type":       "pika_update",
                "username":   username,
                "pikaResult": result,
                "pikaReason": reason,
                "player":     player_entry,
                "done":       done,
                "total":      total,
            })
            time.sleep(1.5)   # gap between bot checks

        with self._lock:
            self._pika_scan_running = False
        self._broadcast({"type": "pika_scan_done", "done": done, "total": total})

    # ----------------------------------------------- watchlist recheck
    def recheck_watchlist(self) -> dict:
        """
        Re-check name availability for all watchlist entries.
        Accounts whose name is now free → skin checked → if clean, promoted to titans.
        """
        with WATCHLIST_LOCK:
            entries = list(_WATCHLIST.values())
        if not entries:
            return {"ok": True, "checked": 0, "promoted": 0}

        total    = len(entries)
        promoted = 0

        def check_entry(entry):
            u      = entry["username"]
            status = check_name_available(u)
            return u, entry, status

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for u, entry, status in ex.map(check_entry, entries):
                self._broadcast({
                    "type": "watchlist_progress",
                    "username": u, "status": status,
                })
                if status != "free":
                    continue

                has_skin = check_has_custom_skin(u)
                _pcache_set(u.lower(), entry.get("profile", {}), has_skin)

                if has_skin:
                    print(f"[WATCH] {u} name free but has custom skin — skip")
                    continue

                new_entry = {
                    "username":      u,
                    "lastSeen":      None,
                    "ranks":         entry.get("ranks", []),
                    "rank":          {},
                    "clan":          None,
                    "hasCustomSkin": False,
                    "pikaResult":    "pending",
                    "nameStatus":    "free",
                    "foundForRanks": entry.get("ranks", []),
                }
                with self._lock:
                    added = _titans_add(new_entry, self.titans)
                    if added:
                        promoted += 1
                        with SEEN_LOCK:
                            already_seen = u.lower() in SEEN_TITANS
                        self._broadcast({
                            "type":          "titan_found",
                            "player":        {**new_entry, "alreadySeen": already_seen},
                            "scanned":       self.scanned,
                            "queued":        len(self._queue),
                            "titanCount":    len(self.titans),
                            "watchlistSize": len(_WATCHLIST),
                            "fromWatchlist": True,
                        })
                        self._pika_queue.put((u, new_entry))

                _watchlist_remove(u.lower())

        self._broadcast({
            "type":          "watchlist_done",
            "checked":       total,
            "promoted":      promoted,
            "watchlistSize": len(_WATCHLIST),
        })
        return {"ok": True, "checked": total, "promoted": promoted}


# Singleton
crawler = CrawlEngine()


# ---------------------------------------------------------------------------
# AUTO-RESTART WATCHDOG
# Starts crawl automatically on boot and restarts it if it ever stops.
# Rotates through AUTO_SEEDS so if one graph is exhausted it moves to next.
# ---------------------------------------------------------------------------
def _watchdog_loop():
    seed_index = 0
    time.sleep(10)
    # Post initial digest on startup with existing titans
    _update_digest()
    while True:
        if not crawler._running:
            seed  = AUTO_SEEDS[seed_index % len(AUTO_SEEDS)]
            print(f"[WATCHDOG] Starting crawl — seed: {seed}, ranks: {AUTO_RANKS}")
            crawler.target_ranks = list(AUTO_RANKS)
            crawler.start(seed=seed, ranks=AUTO_RANKS)
            seed_index += 1
        time.sleep(30)


Thread(target=_watchdog_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# FLASK ROUTES
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


KNOWN_RANKS = [
    # Minigames
    "VIP", "Elite", "Titan", "Champion",
    # OP Factions / Factions
    "Warrior", "Conqueror", "Legend", "God", "Celestial",
    # KitPvP
    "Squire", "Knight", "Baron", "Earl", "Duke", "King",
    # Practice
    "Silver", "Gold", "Diamond",
    # OneBlock
    "Peasant", "Lord",
    # Additional ranks
    "Immortal", "Boss", "Omega", "Noble", "Emperor", "Monarch", "Sigma",
]

@app.route("/api/ranks", methods=["GET"])
def api_ranks():
    return jsonify({"ranks": sorted(set(KNOWN_RANKS))})


# ── Crawl ──────────────────────────────────────────────────────────────────
@app.route("/api/crawl/start", methods=["POST"])
def crawl_start():
    data  = request.get_json(force=True) or {}
    seed  = data.get("seed", "").strip()
    if not seed:
        return jsonify({"error": "Seed username required"}), 400
    ranks = data.get("ranks") or data.get("rank") or None
    crawler.start(seed=seed, ranks=ranks)
    return jsonify({"ok": True, "state": crawler.get_state()})


@app.route("/api/crawl/resume", methods=["POST"])
def crawl_resume():
    if not crawler.seed:
        return jsonify({"error": "No previous session to resume"}), 400
    crawler.start()
    return jsonify({"ok": True, "state": crawler.get_state()})


@app.route("/api/crawl/stop", methods=["POST"])
def crawl_stop():
    crawler.stop()
    return jsonify({"ok": True, "state": crawler.get_state()})


@app.route("/api/crawl/reset", methods=["POST"])
def crawl_reset():
    crawler.reset()
    return jsonify({"ok": True})


@app.route("/api/crawl/clear-results", methods=["POST"])
def crawl_clear_results():
    """Wipe the found accounts list without touching BFS state."""
    if crawler._running:
        return jsonify({"error": "Stop crawling before clearing results"}), 400
    crawler.clear_results()
    return jsonify({"ok": True})


@app.route("/api/crawl/state", methods=["GET"])
def crawl_state_route():
    return jsonify(crawler.get_state())


# ── Pika scan ──────────────────────────────────────────────────────────────
@app.route("/api/pika/scan/start", methods=["POST"])
def pika_scan_start():
    result = crawler.start_pika_scan()
    return (jsonify(result), 400) if "error" in result else jsonify(result)


@app.route("/api/pika/scan/revisit", methods=["POST"])
def pika_scan_revisit():
    result = crawler.start_pika_scan(revisit=True)
    return (jsonify(result), 400) if "error" in result else jsonify(result)


@app.route("/api/pika/scan/stop", methods=["POST"])
def pika_scan_stop():
    crawler.stop_pika_scan()
    return jsonify({"ok": True})


# ── Watchlist ──────────────────────────────────────────────────────────────
@app.route("/api/watchlist", methods=["GET"])
def watchlist_get():
    with WATCHLIST_LOCK:
        items = list(_WATCHLIST.values())
    return jsonify({"watchlist": items, "count": len(items)})


@app.route("/api/watchlist/recheck", methods=["POST"])
def watchlist_recheck():
    if crawler._running:
        return jsonify({"error": "Stop crawling before rechecking watchlist"}), 400
    with WATCHLIST_LOCK:
        if not _WATCHLIST:
            return jsonify({"error": "Watchlist is empty"}), 400
        wl_size = len(_WATCHLIST)
    Thread(target=crawler.recheck_watchlist, daemon=True).start()
    return jsonify({"ok": True, "checking": wl_size})


@app.route("/api/watchlist/clear", methods=["POST"])
def watchlist_clear():
    with WATCHLIST_LOCK:
        _WATCHLIST.clear()
    _save_watchlist()
    return jsonify({"ok": True})


# ── Player cache ───────────────────────────────────────────────────────────
@app.route("/api/player-cache/stats", methods=["GET"])
def player_cache_stats():
    with PCACHE_LOCK:
        total = len(_PLAYER_CACHE)
        now   = time.time()
        fresh = sum(
            1 for v in _PLAYER_CACHE.values()
            if now - v.get("lastChecked", 0) <= PCACHE_TTL
        )
    return jsonify({"total": total, "fresh": fresh, "ttlDays": PCACHE_TTL // 86400})


@app.route("/api/player-cache/clear", methods=["POST"])
def player_cache_clear():
    with PCACHE_LOCK:
        _PLAYER_CACHE.clear()
    _save_player_cache()
    with crawler._lock:
        crawler.visited.clear()
    return jsonify({"ok": True})


# ── Export ─────────────────────────────────────────────────────────────────
@app.route("/api/crawl/export", methods=["GET"])
def crawl_export():
    import datetime
    lines = ["Username,LastSeen,Clan,Ranks,PikaResult"]
    for t in crawler.titans:
        ranks     = " | ".join(
            r.get("displayName", "").replace(",", "") for r in t.get("ranks", [])
        )
        last_seen = t.get("lastSeen") or ""
        if last_seen:
            try:
                last_seen = datetime.datetime.fromtimestamp(
                    last_seen / 1000
                ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
        clan       = (t.get("clan") or {}).get("name", "")
        pika_res   = t.get("pikaResult", "")
        lines.append(f"{t['username']},{last_seen},{clan},{ranks},{pika_res}")
    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


# ── Seen list ──────────────────────────────────────────────────────────────
@app.route("/api/seen/add", methods=["POST"])
def seen_add():
    data      = request.get_json(force=True) or {}
    usernames = data.get("usernames", [])
    if isinstance(usernames, str):
        usernames = [usernames]
    with SEEN_LOCK:
        for u in usernames:
            SEEN_TITANS.add(u.strip().lower())
        save_seen(SEEN_TITANS)
    return jsonify({"ok": True, "seenCount": len(SEEN_TITANS)})


@app.route("/api/seen/remove", methods=["POST"])
def seen_remove():
    data     = request.get_json(force=True) or {}
    username = data.get("username", "").strip().lower()
    with SEEN_LOCK:
        SEEN_TITANS.discard(username)
        save_seen(SEEN_TITANS)
    return jsonify({"ok": True, "seenCount": len(SEEN_TITANS)})


@app.route("/api/seen/clear", methods=["POST"])
def seen_clear():
    with SEEN_LOCK:
        SEEN_TITANS.clear()
        save_seen(SEEN_TITANS)
    return jsonify({"ok": True})


@app.route("/api/seen/list", methods=["GET"])
def seen_list():
    with SEEN_LOCK:
        return jsonify({"seen": sorted(SEEN_TITANS), "count": len(SEEN_TITANS)})


# ── SSE stream ─────────────────────────────────────────────────────────────
@app.route("/api/crawl/stream")
def crawl_stream():
    client_q = crawler.subscribe()

    def generate():
        yield f"data: {json.dumps({'type': 'init', **crawler.get_state()})}\n\n"
        try:
            while True:
                try:
                    yield client_q.get(timeout=25)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            crawler.unsubscribe(client_q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Premium check (manual) ─────────────────────────────────────────────────
@app.route("/api/check/premium", methods=["GET"])
def check_premium():
    import subprocess
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "Username required"}), 400
    script = os.path.join(_HERE, "pika_check.js")
    if not os.path.exists(script):
        return jsonify({"error": "pika_check.js not found"}), 500
    try:
        proc = subprocess.run(
            ["node", script, username],
            capture_output=True, text=True, timeout=90, cwd=_HERE,
        )
        stdout = proc.stdout.strip()
        if not stdout:
            return jsonify({"error": "Bot produced no output",
                            "stderr": proc.stderr[:200]}), 502
        data   = json.loads(stdout.splitlines()[-1])
        if "error" in data:
            return jsonify({"error": data["error"]}), 502
        result = data.get("result", "unknown")
        return jsonify({
            "username": username,
            "result":   result,
            "label":    {
                "premium": "Premium — Microsoft account",
                "cracked": "Cracked — password login (/login on Pika)",
                "unknown": "Unknown — couldn't determine",
            }.get(result, result),
            "reason": data.get("reason", ""),
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Bot timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Name availability ──────────────────────────────────────────────────────
@app.route("/api/check/name", methods=["GET"])
def api_check_name_available():
    username = request.args.get("username", "").strip()
    if not username:
        return jsonify({"error": "Username required"}), 400
    return jsonify({
        "username":  username,
        "available": check_name_available(username) == "free",
    })


@app.route("/api/check/names-batch", methods=["POST"])
def check_names_batch():
    data      = request.get_json(force=True) or {}
    usernames = data.get("usernames", [])
    if not usernames:
        return jsonify({"error": "No usernames"}), 400
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for uname, available in ex.map(
            lambda u: (u, check_name_available(u) == "free"),
            usernames[:200],
        ):
            results[uname] = available
    return jsonify({"results": results})


# ── Single-player scan ─────────────────────────────────────────────────────
@app.route("/api/scan", methods=["GET"])
def scan_player():
    try:
        username = request.args.get("username", "").strip()
        if not username:
            return jsonify({"error": "Username is required"}), 400
        t0      = time.time()
        profile = fetch_profile_with_retry(username)
        if not profile:
            return jsonify({"error": f"Player '{username}' not found on Pika Network."}), 404

        friends_list               = profile.get("friends") or []
        titan_friends, other_friends = [], []

        if friends_list:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                fut_map = {
                    ex.submit(fetch_profile_with_retry, f["username"]): f["username"]
                    for f in friends_list
                }
                for fut in concurrent.futures.as_completed(fut_map):
                    fname = fut_map[fut]
                    try:
                        fp = fut.result()
                        if fp:
                            ranks    = fp.get("ranks", [])
                            is_match = passes_rank_filter(ranks)
                            info     = {
                                "username": fp.get("username", fname),
                                "lastSeen": fp.get("lastSeen"),
                                "ranks":    ranks,
                                "rank":     fp.get("rank", {}),
                                "clan":     fp.get("clan"),
                            }
                            (titan_friends if is_match else other_friends).append(info)
                    except Exception:
                        pass

        all_friends = titan_friends + other_friends
        if all_friends:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                for fd, has_skin in zip(
                    all_friends,
                    ex.map(lambda f: check_has_custom_skin(f["username"]), all_friends),
                ):
                    fd["hasCustomSkin"] = has_skin

        target_uname  = profile.get("username", username)
        target_player = {
            "username":      target_uname,
            "lastSeen":      profile.get("lastSeen"),
            "ranks":         profile.get("ranks", []),
            "rank":          profile.get("rank", {}),
            "clan":          profile.get("clan"),
            "hasCustomSkin": check_has_custom_skin(target_uname),
        }
        titan_friends.sort(key=lambda x: x["username"].lower())
        other_friends.sort(key=lambda x: x["username"].lower())
        print(
            f"[SCAN] {username} — {len(friends_list)} friends, "
            f"{len(titan_friends)} matches, {time.time()-t0:.2f}s"
        )
        return jsonify({
            "target":       target_player,
            "friendCount":  len(friends_list),
            "titanFriends": titan_friends,
            "otherFriends": other_friends,
            "targetRanks":  crawler.target_ranks,
            "targetRank":   crawler.target_ranks[0] if crawler.target_ranks else "",
        })
    except Exception as e:
        print("[ERROR]", e)
        return jsonify({"error": str(e)}), 500


# ── NameMC name-drop info ──────────────────────────────────────────────────
@app.route("/api/namedrop/<username>", methods=["GET"])
def namedrop_info(username: str):
    """
    Scrape NameMC for name change history and drop time.
    Returns:
      { changedAt: <ISO str or null>, dropsAt: <ISO str or null>, available: bool }
    NameMC shows "Available" when the name is free, or a drop countdown when
    a player changed it within the last 37 days.
    """
    username = username.strip()
    if not username:
        return jsonify({"error": "Username required"}), 400
    try:
        r = http_session.get(
            f"https://namemc.com/name/{username}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return jsonify({"changedAt": None, "dropsAt": None, "available": False,
                            "error": f"NameMC returned {r.status_code}"}), 502

        html = r.text

        # ── Is name currently available? ────────────────────────────────────
        # NameMC shows a green "Available" badge when the name is free
        import re
        available = bool(
            re.search(r'class=["\'][^"\']*status-available[^"\']*["\']', html, re.I) or
            re.search(r'>Available<', html, re.I) or
            re.search(r'searches/available', html, re.I)
        )

        # ── Drop time — NameMC embeds an ISO timestamp in a <time> element ──
        # Pattern: <time datetime="2024-01-15T10:30:00.000Z" ...>
        drops_at   = None
        changed_at = None

        time_matches = re.findall(
            r'<time[^>]+datetime=["\']([^"\']+)["\'][^>]*>',
            html, re.I
        )

        if time_matches:
            # NameMC shows times in order: [drop_time, ...change_times...]
            # The first <time> on the name page is the availability/drop date
            drops_at = time_matches[0] if time_matches else None
            # Second time element is usually when the name was last changed
            changed_at = time_matches[1] if len(time_matches) > 1 else None

        # Fallback: look for data-timestamp attributes
        if not drops_at:
            ts_match = re.search(
                r'data-(?:timestamp|time|countdown)=["\']([^"\']+)["\']',
                html, re.I
            )
            if ts_match:
                drops_at = ts_match.group(1)

        return jsonify({
            "username":  username,
            "changedAt": changed_at,
            "dropsAt":   drops_at,
            "available": available,
        })

    except Exception as e:
        return jsonify({"error": str(e), "changedAt": None,
                        "dropsAt": None, "available": False}), 500


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Only auto-open browser when running locally (not on Render)
    if not os.environ.get("RENDER"):
        Timer(1.2, lambda: webbrowser.open_new(f"http://127.0.0.1:{port}")).start()
    print(f"Starting Pika Rank Finder on http://0.0.0.0:{port} ...")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
