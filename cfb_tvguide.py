#!/usr/bin/env python3
"""College football TV guide: a week of ESPN games laid out as networks by time.

    ./cfb_tvguide.py --serve --open          # live guide at http://localhost:8080
    ./cfb_tvguide.py --week 5 -o w5.html     # one-off page
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from string import Template

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
CORE_API = "http://sports.core.api.espn.com/v2/sports/football/leagues/college-football/seasons"
# site.api 403s on most explicit User-Agents but serves urllib's default; keep bare first.
UA_CHAIN = (None, "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cfb-tvguide"
CHANNELS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cfb-tvguide" / "channels.json"

# Adaptive cache lifetimes, chosen per response from what it contains.
LIVE_TTL = 300                          # a game is on
IDLE_TTL = 3600                         # games still to come
DONE_TTL = 43200                        # every game final
WEEK_TTL = 86400                        # core-API week bounds
LIVE_WINDOW = dt.timedelta(hours=5)     # a stale 'pre' this long past kickoff stops counting as on
POLL_SECONDS = 60                       # how often a served page asks the server for a fresh copy

DIVISIONS = {"fbs": ["80"], "fcs": ["81"], "all": ["80", "81"]}

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:                                    # no tzdata: close enough for a date
    EASTERN = dt.timezone(dt.timedelta(hours=-4))

# Lineup order: over-the-air, national cable, conference networks, streaming, then unknown.
NETWORK_TIERS = (
    ("ABC", "CBS", "FOX", "NBC", "CW", "The CW"),
    ("ESPN", "ESPN2", "ESPNU", "ESPNEWS", "FS1", "FS2", "TNT", "TBS", "truTV",
     "USA", "USA Net", "USA Network", "CBSSN", "CBS Sports Network"),
    ("SEC Network", "SECN", "BTN", "Big Ten Network", "ACC Network", "ACCN",
     "Pac-12 Network", "MW Network", "Longhorn Network"),
    ("ESPN+", "SECN+", "ACCNX", "ACC Network Extra", "Peacock", "Paramount+",
     "MW+", "B1G+", "FloSports", "FloCollege", "Stadium", "ESPN3", "Prime Video"),
)
STREAMING_HINTS = ("+", "Peacock", "Paramount", "Flo", "ESPN3", "Prime", "Netflix", "YouTube", "Extra")
# Many streams under one name: on today's grid, games in progress get a row of their own.
SPLIT_LIVE = ("ESPN+",)
CLOSE_MARGIN = 8                        # one score: touchdown plus two-point conversion


class NoGames(Exception):
    def __init__(self, msg, offset=0):
        super().__init__(msg)
        self.offset = offset


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def _open(url: str, timeout: int = 30):
    last = None
    for ua in UA_CHAIN:
        req = urllib.request.Request(url, headers={"User-Agent": ua} if ua else {})
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as ex:
            last = ex
            if ex.code not in (403, 429):
                raise
    raise last


def _shelf_life(data: dict, now: dt.datetime) -> int:
    """Seconds a cached response stays fresh: short while a game is on, long otherwise."""
    events = data.get("events")
    if events is None:
        return WEEK_TTL
    states = []
    for ev in events:
        try:
            comp = ev["competitions"][0]
            state = comp["status"]["type"]["state"]
            start = parse_utc(ev["date"])
        except (KeyError, IndexError, ValueError):
            continue
        if state == "in":
            return LIVE_TTL
        if state == "pre" and comp.get("timeValid", True) and start <= now < start + LIVE_WINDOW:
            return LIVE_TTL                          # kicked off; ESPN has not caught up yet
        states.append(state)
    return DONE_TTL if states and all(s == "post" for s in states) else IDLE_TTL


def _get_json(url: str, ttl: int | None, quiet: bool, now: dt.datetime) -> dict:
    """Cached GET. ttl=None sizes freshness from the response; a failed fetch falls back to cache."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    cached = None
    if key.exists():
        try:
            cached = json.loads(key.read_text())
        except ValueError:
            cached = None
    if cached is not None:
        age = time.time() - key.stat().st_mtime
        if age < (_shelf_life(cached, now) if ttl is None else ttl):
            return cached
    if not quiet:
        print(f"  fetch {url}", file=sys.stderr)
    try:
        data = json.loads(_open(url))
    except (urllib.error.URLError, OSError, ValueError) as ex:
        if cached is None:
            raise
        if not quiet:
            print(f"  ! {ex}; using cached copy", file=sys.stderr)
        return cached
    key.write_text(json.dumps(data))
    return data


def espn_week_bounds(year: int, week: int, seasontype: int, ttl, quiet: bool, now):
    """Real start/end timestamps for an ESPN week, from the core API."""
    url = f"{CORE_API}/{year}/types/{seasontype}/weeks/{week}?lang=en&region=us"
    item = _get_json(url, ttl, quiet, now)
    start = parse_utc(item["startDate"])
    end = parse_utc(item["endDate"])
    return start, end, item.get("text", f"Week {week}")


def fetch_range(start_utc: dt.datetime, end_utc: dt.datetime, groups, ttl, quiet: bool, now):
    """Scoreboard events over a UTC window, one query per day and group, merged by id.

    Ranges now 400 and a large `limit` re-triggers the 25-event cap, so omit both.
    """
    merged, week = {}, None
    day, last = start_utc.date(), end_utc.date()
    while day <= last:
        for gid in groups or [""]:
            q = f"dates={day:%Y%m%d}"
            if gid:
                q += f"&groups={gid}"
            data = _get_json(f"{SITE_API}?{q}", ttl, quiet, now)
            week = week or data.get("week", {}).get("number")
            for ev in data.get("events", []):
                merged.setdefault(ev["id"], ev)
        day += dt.timedelta(days=1)
    return list(merged.values()), week


# --------------------------------------------------------------------------- #
# normalising
# --------------------------------------------------------------------------- #
def parse_utc(s: str) -> dt.datetime:
    s = s.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)


class Team:
    __slots__ = ("id", "abbr", "name", "full", "rank", "score", "winner", "color", "logo", "record", "fav")

    def __init__(self, comp):
        t = comp.get("team", {})
        self.id = str(t.get("id") or "")
        self.abbr = t.get("abbreviation") or t.get("shortDisplayName") or "?"
        self.name = t.get("shortDisplayName") or t.get("location") or self.abbr
        self.full = t.get("displayName") or self.name
        self.fav = False
        rank = (comp.get("curatedRank") or {}).get("current", 99)
        self.rank = rank if isinstance(rank, int) and 1 <= rank <= 25 else None
        self.score = comp.get("score")
        self.winner = bool(comp.get("winner"))
        self.color = "#" + (t.get("color") or "777777")
        self.logo = t.get("logo") or ""
        recs = comp.get("records") or []
        self.record = next((r.get("summary") for r in recs if r.get("type") == "total"), None)


class Game:
    def __init__(self, event, tz, game_len):
        comp = event["competitions"][0]
        self.id = event["id"]
        # timeValid=false: kickoff unannounced, timestamp is midnight Eastern, only the date counts.
        self.time_tba = not comp.get("timeValid", True)
        self.utc = parse_utc(event["date"])
        self.start = self.utc.astimezone(tz)
        self.end = self.start + dt.timedelta(minutes=game_len)
        self.network = (comp.get("broadcast") or "").strip() or self._geo(comp) or "TBD"
        self.channel = ""
        self.venue = (comp.get("venue") or {}).get("fullName") or ""
        city = ((comp.get("venue") or {}).get("address") or {})
        self.city = ", ".join(x for x in (city.get("city"), city.get("state") or city.get("country")) if x)
        self.neutral = bool(comp.get("neutralSite"))
        notes = comp.get("notes") or []
        self.note = notes[0].get("headline", "") if notes else ""
        odds = comp.get("odds") or []
        self.odds = odds[0].get("details", "") if odds else ""
        self.over_under = odds[0].get("overUnder") if odds else None

        self.period = comp.get("status", {}).get("period") or 0
        status = comp.get("status", {}).get("type", {})
        self.state = status.get("state", "pre")          # pre | in | post
        self.detail = status.get("shortDetail", "")
        self.short_status = status.get("shortDetail", "") if self.state == "in" else ""

        cs = comp.get("competitors", [])
        home = next((c for c in cs if c.get("homeAway") == "home"), cs[0] if cs else {})
        away = next((c for c in cs if c.get("homeAway") == "away"), cs[-1] if cs else {})
        self.home, self.away = Team(home), Team(away)
        self.link = f"https://www.espn.com/college-football/game/_/gameId/{self.id}"

    @staticmethod
    def _geo(comp):
        for g in comp.get("geoBroadcasts") or []:
            name = (g.get("media") or {}).get("shortName")
            if name:
                return name
        return None

    @property
    def ranked(self):
        return self.home.rank is not None or self.away.rank is not None

    @property
    def close(self):
        """Live, 4th quarter or overtime, within one score."""
        if self.state != "in" or self.period < 4:
            return False
        try:
            return abs(int(self.home.score) - int(self.away.score)) <= CLOSE_MARGIN
        except (TypeError, ValueError):
            return False

    @property
    def fav(self):
        return self.home.fav or self.away.fav

    @property
    def top_rank(self):
        ranks = [t.rank for t in (self.home, self.away) if t.rank]
        return min(ranks) if ranks else 99

    def tv_day(self, day_rollover: int) -> dt.date:
        """Games kicking off after midnight belong to the previous TV day."""
        if self.time_tba:
            return self.utc.astimezone(EASTERN).date()
        return (self.start - dt.timedelta(hours=day_rollover)).date()

    def matchup(self):
        def side(t):
            return (f"#{t.rank} " if t.rank else "") + t.abbr
        joiner = " vs " if self.neutral else " @ "
        return side(self.away) + joiner + side(self.home)


# --------------------------------------------------------------------------- #
# colour -- accessibility requirement: keep team accents out of narrow-band red
# --------------------------------------------------------------------------- #
def red_safe(hexstr: str, dark: bool = True) -> str:
    """Desaturate and shift hues within 45 degrees of red; clamp lightness for contrast."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hexstr or "")
    if not m:
        return "#8a8a86"
    r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    hue = h * 360
    dist = min(hue, 360 - hue)
    if dist < 45:
        pen = 1 - dist / 45              # 1.0 at pure red, 0 at 45 degrees out
        s *= 1 - 0.60 * pen
        hue = (hue + 14 * pen) % 360     # toward salmon/orange
        h = hue / 360
    s = min(s, 0.62)
    l = min(max(l, 0.42), 0.72) if dark else min(max(l, 0.30), 0.55)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in (r, g, b))


# --------------------------------------------------------------------------- #
# logos
# --------------------------------------------------------------------------- #
# Combiner returns a scaled PNG: ~1KB at 48px versus ~13KB for the raw asset.
COMBINER = "https://a.espncdn.com/combiner/i?img={path}&w=48&h=48"


def _thumb_url(url: str) -> str:
    m = re.search(r"(/i/teamlogos/.*?\.png)", url)
    return COMBINER.format(path=m.group(1)) if m else url


def _data_uri(url: str, quiet: bool) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / ("logo-" + hashlib.sha1(url.encode()).hexdigest() + ".b64")
    if not cached.exists():
        try:
            blob = _open(_thumb_url(url), timeout=20)
            cached.write_text("data:image/png;base64," + base64.b64encode(blob).decode())
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            if not quiet:
                print(f"  ! logo failed: {url}", file=sys.stderr)
            cached.write_text("")
    return cached.read_text()


class Logos:
    """Emits each distinct logo once, as a CSS class, instead of per game."""

    def __init__(self, mode: str, quiet: bool):
        self.mode, self.quiet = mode, quiet
        self.slots: dict[str, str] = {}

    def tag(self, url: str) -> str:
        if not url or self.mode == "none":
            return ""
        if url not in self.slots:
            self.slots[url] = f"g{len(self.slots)}"
        return f'<i class="lg {self.slots[url]}"></i>'

    def css(self) -> str:
        if self.mode == "none" or not self.slots:
            return ""
        out = []
        for url, cls in self.slots.items():
            src = _data_uri(url, self.quiet) if self.mode == "embed" else url
            if src:
                out.append(f".lg.{cls}{{background-image:url({src})}}")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def network_sort_key(name: str):
    for tier, names in enumerate(NETWORK_TIERS):
        if name in names:
            return (tier, name.lower())
    if name == "TBD":
        return (9, "")
    if any(h in name for h in STREAMING_HINTS):
        return (3, name.lower())
    return (2, name.lower())


def live_sort_key(net: str, lanes):
    """On now (close games first), then soonest kickoff, then finished; lineup order breaks ties."""
    games = [g for lane in lanes for g in lane]
    tier = network_sort_key(net)
    if any(g.state == "in" for g in games):
        return (0, not any(g.close for g in games), tier)
    upcoming = [g.start for g in games if g.state == "pre"]
    if upcoming:
        return (1, min(upcoming), tier)
    return (2, tier)


def pack_lanes(games, slot_min):
    """Interval-partition one network's games into non-overlapping lanes."""
    lanes = []
    for g in sorted(games, key=lambda x: (x.start, x.matchup())):
        for lane in lanes:
            if lane[-1].end <= g.start:
                lane.append(g)
                break
        else:
            lanes.append([g])
    return lanes


def day_start(day, tz):
    """Local midnight opening a TV day. Grid offsets are measured from here."""
    return dt.datetime.combine(day, dt.time(), tz)


def shared_window(by_day, tz, slot_min):
    """One time-of-day window for every day, as (minutes from TV-day midnight, n_slots).

    A 2am kickoff on Saturday's grid is 1560, not 120.
    """
    lo = hi = None
    for day, games in by_day.items():
        mid = day_start(day, tz)
        for g in games:
            if g.time_tba:
                continue
            s = (g.start - mid).total_seconds() // 60
            e = (g.end - mid).total_seconds() // 60
            lo = s if lo is None else min(lo, s)
            hi = e if hi is None else max(hi, e)
    if lo is None:
        return 0, 0
    lo = int(lo // 60) * 60                       # back to the hour
    hi = int(-(-hi // 60)) * 60                   # up to the next hour
    return lo, int((hi - lo) // slot_min)


def load_channels(path, required: bool):
    """Network -> channel label from a local JSON object, keys matched case-insensitively."""
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except FileNotFoundError:
        if required:
            sys.exit(f"channel map not found: {path}")
        return {}
    except ValueError as ex:
        sys.exit(f"channel map {path}: {ex}")
    if not isinstance(data, dict):
        sys.exit(f"channel map {path}: expected a JSON object of network -> channel")
    return {str(k).lower(): str(v) for k, v in data.items()}


def mark_favs(games, keys):
    """Flag teams matching any key: ESPN id, abbreviation, short or full name (case-insensitive)."""
    keys = {k.strip().lower() for k in keys if k.strip()}
    for g in games:
        for t in (g.home, g.away):
            t.fav = bool(keys & {t.id, t.abbr.lower(), t.name.lower(), t.full.lower()})


def stretch_live(games, now, slot_min):
    """A game still on past its assumed window keeps its block a slot beyond now."""
    for g in games:
        if g.state == "in":
            g.end = max(g.end, now + dt.timedelta(minutes=slot_min))


def day_order(days, today, win_start, win_end):
    """Week in progress: today, then later days, then earlier days newest-first.
    Past or future week: chronological."""
    if not win_start <= today <= win_end:
        return sorted(days)
    ahead = sorted(d for d in days if d >= today)
    return ahead + sorted((d for d in days if d < today), reverse=True)


def build_day(day_games, slot_min, live=False):
    """Return [(network, [lane, ...]), ...] for one day; live=True uses on-now ordering
    and splits SPLIT_LIVE networks into on-now and everything-else rows."""
    by_net = defaultdict(list)
    for g in day_games:
        by_net[g.network].append(g)
    rows = []
    for net, gs in by_net.items():
        parts = [gs]
        if live and net in SPLIT_LIVE:
            parts = [[g for g in gs if g.state == "in"], [g for g in gs if g.state != "in"]]
        rows.extend((net, pack_lanes(p, slot_min)) for p in parts if p)
    if live:
        rows.sort(key=lambda r: live_sort_key(*r))
    else:
        rows.sort(key=lambda r: network_sort_key(r[0]))
    return rows


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def esc(s):
    return html.escape(str(s or ""), quote=True)


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def render_game(g, grid_start, slot_min, row, logos, show_net=False):
    col = int((g.start - grid_start).total_seconds() // 60 // slot_min) + 2  # +1 network col, +1 1-based
    span = max(2, int((g.end - g.start).total_seconds() // 60 // slot_min))
    accent = red_safe(g.home.color)
    classes = ["game", f"st-{g.state}"]
    if g.ranked:
        classes.append("ranked")
    if g.fav:
        classes.append("fav")

    def side(t, at=""):
        # empty spacer on the away line keeps both team names aligned
        bits = [f'<span class="at">{at}</span>']
        bits.append(logos.tag(t.logo))
        if t.rank:
            bits.append(f'<span class="rk">{t.rank}</span>')
        cls = "tm" + (" win" if g.state == "post" and t.winner else "") + (" mine" if t.fav else "")
        bits.append(f'<span class="{cls}">{esc(t.name)}</span>')
        if g.state != "pre" and t.score is not None:
            bits.append(f'<span class="sc">{esc(t.score)}</span>')
        return f'<span class="side">{"".join(bits)}</span>'

    if g.state == "in":
        tag = f'<span class="live">LIVE &middot; {esc(g.short_status)}</span>'
        if g.close:
            tag += '<span class="close">Close</span>'
    elif g.state == "post":
        tag = '<span class="final">FINAL</span>'
    else:
        tag = f'<span class="tm-lbl">{g.start:%-I:%M}</span>'

    meta = []
    if g.note:
        meta.append(g.note)
    if g.odds:
        meta.append(g.odds)
    if g.over_under:
        meta.append(f"O/U {g.over_under}")
    if g.venue:
        meta.append(g.venue + (f" ({g.city})" if g.city else ""))
    title = f"{g.away.name} at {g.home.name} - {g.start:%a %-I:%M %p} on {g.network}"
    if meta:
        title += "\n" + " | ".join(meta)

    note = f'<span class="note">{esc(g.note)}</span>' if g.note else ""
    if show_net:                                 # "Your teams" row: say where it's on
        where = g.network + (f" {g.channel}" if g.channel else "")
        note = f'<span class="fnet">{esc(where)}</span>' + note
    return (
        f'<a class="{" ".join(classes)}" style="grid-row:{row};grid-column:{col}/span {span};'
        f'--accent:{accent}" href="{esc(g.link)}" target="_blank" rel="noopener" title="{esc(title)}">'
        f'<span class="hd">{tag}{note}</span>'
        f'{side(g.away)}{side(g.home, "vs" if g.neutral else "@")}</a>'
    )


def render_tba(games):
    """Games with no announced kickoff get listed, not placed on the grid."""
    chips = []
    for g in sorted(games, key=lambda x: (x.network, x.matchup())):
        accent = red_safe(g.home.color)
        chips.append(
            f'<a class="chip{" fav" if g.fav else ""}" style="--accent:{accent}" href="{esc(g.link)}" target="_blank" '
            f'rel="noopener" title="{esc(g.venue)}">'
            f'<span class="chip-net">{esc(g.network)}{" " + esc(g.channel) if g.channel else ""}</span>'
            f'<span class="chip-mu">{esc(g.matchup())}</span></a>'
        )
    return (f'<div class="tba"><span class="tba-lbl">Time TBA</span>'
            f'<div class="chips">{"".join(chips)}</div></div>')


def render_day(day, day_games, slot_min, logos, tzname, is_today, grid_start, n_slots, collapsed=False):
    timed = [g for g in day_games if not g.time_tba]
    tba = [g for g in day_games if g.time_tba]

    nets = len({g.network for g in day_games})
    parts = [
        f'<details class="day" data-day="{day.isoformat()}"{"" if collapsed else " open"}>'
        f'<summary><h2><span class="dow">{day:%A}</span>'
        f'<span class="dt">{day:%B %-d}</span>'
        f'<span class="ct">{plural(len(day_games), "game")} &middot; '
        f'{plural(nets, "network")}</span></h2></summary>'
    ]
    if not timed:
        parts.append(render_tba(tba))
        parts.append("</details>")
        return "".join(parts)

    rows = build_day(timed, slot_min, live=is_today)
    mine = [g for g in timed if g.fav]
    if mine:                                     # duplicated above the networks, not moved
        rows.insert(0, (None, pack_lanes(mine, slot_min)))
    per_hour = 60 // slot_min
    parts.append(
        f'<div class="scroller"><div class="grid" style="--slots:{n_slots}" '
        f'data-start="{grid_start.isoformat()}" data-slot="{slot_min}" '
        f'data-today="{"1" if is_today else "0"}">'
    )
    parts.append(f'<div class="corner">{esc(tzname)}</div>')

    # hour header
    for h in range(0, n_slots, per_hour):
        t = grid_start + dt.timedelta(minutes=h * slot_min)
        span = min(per_hour, n_slots - h)
        parts.append(
            f'<div class="hour" style="grid-row:1;grid-column:{h + 2}/span {span}">'
            f'{t:%-I}<span class="ap">{t:%p}</span></div>'
        )

    row = 2
    for net, lanes in rows:
        label = "&#9733; Your teams" if net is None else esc(net)
        ch = "" if net is None else lanes[0][0].channel
        ch = f'<span class="ch">{esc(ch)}</span>' if ch else ""
        parts.append(
            f'<div class="net{" mine" if net is None else ""}" style="grid-row:{row}/span {len(lanes)}">'
            f'<span>{label}</span>{ch}</div>'
        )
        for i, lane in enumerate(lanes):
            edge = " last" if i == len(lanes) - 1 else ""   # heavier rule under the network
            parts.append(f'<div class="lanebg{edge}" style="grid-row:{row};grid-column:2/span {n_slots}"></div>')
            for g in lane:
                parts.append(render_game(g, grid_start, slot_min, row, logos, show_net=net is None))
            row += 1

    parts.append('<div class="nowline" hidden></div>')
    parts.append("</div></div>")
    if tba:
        parts.append(render_tba(tba))
    parts.append("</details>")
    return "".join(parts)


PAGE = Template("""<title>$title</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Saira+Condensed:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
/* Turf-biased neutrals, amber for ranked, teal for live. No pure red, no pure black. */
:root{
  --slotw:48px; --netw:118px; --rowh:64px; --hdrh:36px;

  --ground:#eceee7; --panel:#fbfcf9; --sunk:#e3e6dc;
  --rule:#d3d8c9; --rule-soft:#e6e9de;
  --ink:#22261e; --ink-2:#4c5347; --ink-3:#727a6b;
  --gold:#7d611a; --gold-ink:#5d4712;
  --live:#0f6f5b; --fav:#3d63a6;
  --shadow:0 1px 2px rgba(34,38,30,.07);

  --display:"Saira Condensed",ui-sans-serif,"Helvetica Neue Condensed","Arial Narrow",sans-serif;
  --body:"IBM Plex Sans",ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --ground:#1b1e19; --panel:#252921; --sunk:#20241d;
  --rule:#3a4034; --rule-soft:#2d3229;
  --ink:#e8eade; --ink-2:#b3b9aa; --ink-3:#868d7d;
  --gold:#d5a94f; --gold-ink:#e8c884;
  --live:#63cfb0; --fav:#8fb1e8;
  --shadow:0 1px 2px rgba(0,0,0,.3);
}}
:root[data-theme=dark]{
  --ground:#1b1e19; --panel:#252921; --sunk:#20241d;
  --rule:#3a4034; --rule-soft:#2d3229;
  --ink:#e8eade; --ink-2:#b3b9aa; --ink-3:#868d7d;
  --gold:#d5a94f; --gold-ink:#e8c884;
  --live:#63cfb0; --fav:#8fb1e8;
  --shadow:0 1px 2px rgba(0,0,0,.3);
}

*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--body);
  font-size:14px;line-height:1.4;-webkit-font-smoothing:antialiased}
a{color:inherit}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px;border-radius:3px}

/* ---- masthead ------------------------------------------------------------ */
.top{display:flex;flex-wrap:wrap;align-items:flex-end;gap:20px 28px;
  padding:26px 24px 20px;border-bottom:2px solid var(--ink);background:var(--panel)}
.brand{display:flex;flex-direction:column;gap:2px;min-width:0}
.eyebrow{font-family:var(--display);font-weight:600;font-size:12px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--ink-3)}
.top h1{margin:0;font-family:var(--display);font-weight:700;line-height:.9;
  font-size:clamp(38px,7vw,58px);letter-spacing:-.01em;display:flex;align-items:baseline;gap:10px}
.top h1 .wknum{color:var(--gold);font-variant-numeric:tabular-nums}
.range{font-size:13px;color:var(--ink-2);margin-top:6px;font-variant-numeric:tabular-nums;
  display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px}
.wnav{display:inline-flex;gap:6px}
.wnav a{font-family:var(--display);font-size:11px;font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-2);text-decoration:none;
  border:1px solid var(--rule);border-radius:2px;padding:1px 7px}
.wnav a:hover{color:var(--ink);border-color:var(--ink-3)}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:4px}
.stat{display:flex;flex-direction:column;gap:1px}
.stat b{font-family:var(--display);font-size:24px;font-weight:600;line-height:1;
  font-variant-numeric:tabular-nums}
.stat span{font-family:var(--display);font-size:10.5px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--ink-3)}
.spacer{flex:1 1 40px}
.tg{align-self:flex-end;margin-bottom:4px;background:transparent;color:var(--ink-2);
  border:1px solid var(--rule);border-radius:2px;padding:6px 11px;cursor:pointer;
  font-family:var(--display);font-size:11px;font-weight:600;letter-spacing:.12em;
  text-transform:uppercase}
.tg:hover{color:var(--ink);border-color:var(--ink-3)}
.tg + .tg{margin-left:-18px}

/* ---- favorite teams ------------------------------------------------------ */
.game.fav{border-color:var(--fav);outline:1.5px solid var(--fav);outline-offset:-1.5px}
.tm.mine::before{content:"\\2605 ";color:var(--fav)}
.fnet{font-family:var(--display);font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--ink-2)}
.net.mine{color:var(--fav)}
.chip.fav{border-color:var(--fav)}
.picker{position:fixed;top:12px;right:12px;z-index:20;width:min(340px,calc(100vw - 24px));
  background:var(--panel);border:1px solid var(--rule);border-top:3px solid var(--fav);
  box-shadow:0 6px 24px rgba(0,0,0,.18);padding:12px 14px 14px;font-size:13px}
.picker label{display:block;font-family:var(--display);font-size:11px;font-weight:600;
  letter-spacing:.12em;text-transform:uppercase;color:var(--ink-3);margin-bottom:6px}
.picker input{width:100%;padding:7px 9px;font:inherit;color:var(--ink);background:var(--ground);
  border:1px solid var(--rule);border-radius:2px}
.favlist{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.favlist:empty::before{content:"None yet. Starred teams get a row at the top of each day.";
  color:var(--ink-3);font-size:12px}
.fchip{font:inherit;font-size:12px;color:var(--ink);background:var(--sunk);cursor:pointer;
  border:1px solid var(--fav);border-radius:2px;padding:3px 8px}
.fchip::after{content:" \\00d7";color:var(--ink-3)}
.fchip:hover{background:color-mix(in srgb,var(--fav) 15%,var(--panel))}

/* ---- legend -------------------------------------------------------------- */
.legend{display:flex;gap:8px 20px;flex-wrap:wrap;padding:11px 24px;font-size:12px;
  color:var(--ink-3);background:var(--sunk);border-bottom:1px solid var(--rule)}
.legend i{font-style:normal;color:var(--ink-2)}
.legend .swatch{display:inline-block;width:9px;height:9px;border:1.5px solid var(--gold);
  margin-right:5px;vertical-align:baseline;border-radius:1px}

/* ---- day sections -------------------------------------------------------- */
.day{padding:26px 24px 2px}
.day summary{list-style:none;cursor:pointer}
.day summary::-webkit-details-marker{display:none}
.day h2{margin:0 0 9px;display:flex;align-items:baseline;gap:10px;font-weight:400}
.day h2::before{content:"";align-self:center;width:6px;height:6px;flex:none;
  border:solid var(--ink-3);border-width:0 2px 2px 0;transform:rotate(45deg) translateY(-2px);
  transition:transform .12s ease}
.day:not([open]) h2::before{transform:rotate(-45deg)}
.day:not([open]) h2{opacity:.75}
.day summary:hover h2{opacity:1}
.day h2 .dow{font-family:var(--display);font-weight:700;font-size:22px;letter-spacing:.02em;
  text-transform:uppercase}
.day h2 .dt{font-size:13px;color:var(--ink-2)}
.day h2 .ct{font-family:var(--display);font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);margin-left:auto;white-space:nowrap}

.scroller{overflow-x:auto;overflow-y:visible;border:1px solid var(--rule);
  background:var(--panel);box-shadow:var(--shadow)}
.grid{position:relative;display:grid;
  grid-template-columns:var(--netw) repeat(var(--slots),var(--slotw));
  grid-template-rows:var(--hdrh);grid-auto-rows:var(--rowh);width:max-content;min-width:100%}

.corner{position:sticky;left:0;top:0;z-index:6;grid-row:1;grid-column:1;background:var(--sunk);
  border-right:2px solid var(--rule);border-bottom:2px solid var(--rule);
  display:flex;align-items:center;justify-content:center;
  font-family:var(--display);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);padding:0 4px;text-align:center}
.hour{position:sticky;top:0;z-index:4;background:var(--sunk);
  border-bottom:2px solid var(--rule);border-left:1px solid var(--rule);
  display:flex;align-items:center;gap:3px;padding-left:8px;
  font-family:var(--display);font-size:14px;font-weight:600;color:var(--ink-2);
  font-variant-numeric:tabular-nums}
.hour .ap{font-size:9px;font-weight:500;color:var(--ink-3);letter-spacing:.06em}

.net{position:sticky;left:0;z-index:3;grid-column:1;background:var(--sunk);
  border-right:2px solid var(--rule);border-bottom:2px solid var(--rule);
  display:flex;flex-direction:column;justify-content:center;padding:0 10px;overflow:hidden;
  font-family:var(--display);font-size:14px;font-weight:600;letter-spacing:.02em}
.net span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.net .ch{font-size:11px;font-weight:500;color:var(--ink-3);font-variant-numeric:tabular-nums}
.lanebg{z-index:0;border-bottom:1px solid var(--rule-soft);
  background-image:linear-gradient(to right,var(--rule) 0 1px,transparent 1px);
  background-size:calc(var(--slotw)*4) 100%}
/* lanes within a network: light rule; between networks: heavy */
.lanebg.last{border-bottom:2px solid var(--rule)}

/* ---- game blocks --------------------------------------------------------- */
.game{z-index:1;margin:3px 2px;padding:5px 8px;border-radius:2px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:center;gap:2px;
  text-decoration:none;color:inherit;min-width:0;
  background:color-mix(in srgb,var(--accent) 11%,var(--panel));
  border:1px solid color-mix(in srgb,var(--accent) 30%,var(--rule));
  border-left:4px solid var(--accent);transition:background .12s ease,border-color .12s ease}
.game:hover{background:color-mix(in srgb,var(--accent) 22%,var(--panel));border-color:var(--accent)}
.game.ranked{border-color:var(--gold);box-shadow:inset 0 2px 0 var(--gold)}

.hd{display:flex;gap:7px;align-items:baseline;font-size:10px;line-height:1.2;
  color:var(--ink-3);white-space:nowrap;overflow:hidden}
.tm-lbl{font-family:var(--display);font-size:12px;font-weight:600;letter-spacing:.03em;
  color:var(--ink-2);font-variant-numeric:tabular-nums}
.note{overflow:hidden;text-overflow:ellipsis;font-style:italic}
.live{font-family:var(--display);font-size:11px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--live);display:flex;align-items:center;gap:5px}
.live::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--live);
  animation:blip 1.6s ease-in-out infinite}
@keyframes blip{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.live::before{animation:none}.game,.day h2::before{transition:none}}
.close{font-family:var(--display);font-size:10px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--panel);background:var(--live);padding:0 5px;border-radius:2px}
.final{font-family:var(--display);font-size:11px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--ink-3)}

.side{display:flex;align-items:center;gap:5px;font-size:12.5px;line-height:1.25;
  white-space:nowrap;overflow:hidden;min-width:0}
.at{font-family:var(--display);font-size:10px;color:var(--ink-3);width:13px;flex:none;
  text-transform:uppercase;letter-spacing:.04em}
.lg{width:15px;height:15px;flex:none;background:center/contain no-repeat}
.tm{overflow:hidden;text-overflow:ellipsis}
.tm.win{font-weight:700}
.rk{font-family:var(--display);font-size:11px;font-weight:700;color:var(--gold-ink);
  font-variant-numeric:tabular-nums;flex:none}
.sc{margin-left:auto;padding-left:6px;font-weight:700;font-variant-numeric:tabular-nums}

.nowline{position:absolute;top:0;bottom:0;width:2px;background:var(--gold);z-index:2;
  pointer-events:none}
.nowline::before{content:"";position:absolute;top:0;left:-3px;border:4px solid transparent;
  border-top-color:var(--gold)}

.tba{display:flex;gap:12px;align-items:flex-start;margin-top:10px;padding:11px 13px;
  background:var(--sunk);border:1px solid var(--rule);border-left:3px solid var(--ink-3)}
.tba-lbl{font-family:var(--display);font-size:11px;font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--ink-3);flex:none;padding-top:4px;width:66px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:flex;align-items:baseline;gap:7px;padding:4px 9px;text-decoration:none;
  background:var(--panel);border:1px solid var(--rule);border-left:3px solid var(--accent);
  border-radius:2px;font-size:12px}
.chip:hover{border-color:var(--accent)}
.chip-net{font-family:var(--display);font-size:10.5px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-3)}
footer{padding:22px 24px 44px;color:var(--ink-3);font-size:12px;
  font-variant-numeric:tabular-nums}
@media (max-width:640px){
  :root{--netw:88px;--slotw:42px}
  .top,.legend,.day,footer{padding-left:14px;padding-right:14px}
  .day h2 .ct{margin-left:0}
}
$logocss
</style>
<main id="guide" data-now="$now" data-poll="$poll">
<header class="top">
  <div class="brand">
    <span class="eyebrow">College Football</span>
    <h1>$heading</h1>
    <span class="range">$range$nav</span>
  </div>
  <div class="spacer"></div>
  <div class="stats">$stats</div>
  $teamsbtn<button class="tg" id="theme" type="button">Theme</button>
</header>
<div class="legend">
  <span><i>Rows</i> $rows</span>
  <span><i>Columns</i> kickoff, $tzname</span>
  <span><span class="swatch"></span>ranked team playing</span>
  <span>Blocks assume a $gamelen-minute window &mdash; hover for the line and venue, click for the gamecast</span>
</div>
$days
<footer>$ngames games &middot; built $generated$refresh &middot; schedule and scores from ESPN</footer>
</main>
$picker
<script>
(function(){
  var root=document.documentElement, k='cfb-theme', skew=0;
  try{var saved=localStorage.getItem(k); if(saved) root.setAttribute('data-theme',saved);}catch(e){}
  // delegated: the button is replaced on every refresh
  document.addEventListener('click',function(e){
    if(!e.target.closest('#theme')) return;
    var cur=root.getAttribute('data-theme');
    if(!cur) cur=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
    var next=cur==='dark'?'light':'dark';
    root.setAttribute('data-theme',next);
    try{localStorage.setItem(k,next);}catch(e){}
  });
  // favorite teams: [[espn id, name], ...] in a cookie the server reads on every build
  var ck='cfb-teams', picker=document.getElementById('picker');
  function favs(){
    var m=document.cookie.split('; ').filter(function(c){return c.indexOf(ck+'=')===0;})[0];
    try{return m?JSON.parse(decodeURIComponent(m.slice(ck.length+1))):[];}catch(e){return [];}
  }
  function drawFavs(){
    var box=document.getElementById('favlist'); if(!box) return;
    box.textContent='';
    favs().forEach(function(f){
      var b=document.createElement('button');
      b.type='button'; b.className='fchip'; b.dataset.id=f[0]; b.textContent=f[1];
      b.title='Remove '+f[1]; box.appendChild(b);
    });
  }
  function saveFavs(list){
    document.cookie=ck+'='+encodeURIComponent(JSON.stringify(list))+'; max-age=31536000; path=/; SameSite=Lax';
    drawFavs(); refresh();
  }
  if(picker){
    drawFavs();
    document.addEventListener('click',function(e){
      if(e.target.closest('#teams')){
        picker.hidden=!picker.hidden;
        if(!picker.hidden) document.getElementById('teamq').focus();
        return;
      }
      var chip=e.target.closest('.fchip');
      if(chip){
        saveFavs(favs().filter(function(f){return f[0]!==chip.dataset.id;}));
        return;
      }
      if(!picker.hidden&&!e.target.closest('#picker')) picker.hidden=true;
    });
    document.addEventListener('keydown',function(e){if(e.key==='Escape') picker.hidden=true;});
    document.getElementById('teamq').addEventListener('input',function(){
      var q=this, opt=[].filter.call(document.getElementById('teamopts').options,
        function(o){return o.value===q.value;})[0];
      if(!opt) return;
      var list=favs();
      if(!list.some(function(f){return f[0]===opt.dataset.id;})) list.push([opt.dataset.id,opt.value]);
      q.value=''; saveFavs(list);
    });
  }
  // "now" comes from the server so --now testing lines up
  function sync(){
    var g=document.getElementById('guide');
    skew=g&&g.dataset.now?new Date(g.dataset.now)-Date.now():0;
  }
  function tick(jump){
    document.querySelectorAll('.grid').forEach(function(g){
      var line=g.querySelector('.nowline'); if(!line) return;
      if(g.dataset.today!=='1'){line.hidden=true;return;}
      var start=new Date(g.dataset.start), slot=+g.dataset.slot;
      var slots=(Date.now()+skew-start)/60000/slot;
      var cs=getComputedStyle(g), cols=cs.gridTemplateColumns.split(' ');
      var total=cols.length-1, w=parseFloat(cols[1]);
      if(slots<0||slots>total){line.hidden=true;return;}
      line.style.left=(parseFloat(cols[0])+slots*w)+'px';
      line.hidden=false;
      // first load only: bring the now-line into view with an hour of lead-in
      if(jump) g.parentNode.scrollLeft=Math.max(0,(slots-60/slot)*w);
    });
  }
  // swap in a fresh copy, keeping each day's horizontal scroll and open/closed state
  function refresh(){
    if(document.hidden) return;
    fetch(location.href,{cache:'no-store'}).then(function(r){
      if(!r.ok) throw new Error(r.status); return r.text();
    }).then(function(text){
      var next=new DOMParser().parseFromString(text,'text/html').getElementById('guide');
      var cur=document.getElementById('guide'); if(!next||!cur) return;
      var keep={}, open={};
      cur.querySelectorAll('.day').forEach(function(d){
        open[d.dataset.day]=d.open;
        var s=d.querySelector('.scroller'); if(s) keep[d.dataset.day]=s.scrollLeft;
      });
      next.querySelectorAll('.day').forEach(function(d){
        if(d.dataset.day in open) d.open=open[d.dataset.day];
      });
      cur.replaceWith(next);
      next.querySelectorAll('.day').forEach(function(d){
        var s=d.querySelector('.scroller');
        if(s&&keep[d.dataset.day]) s.scrollLeft=keep[d.dataset.day];
      });
      sync(); tick();
    }).catch(function(){});
  }
  sync(); tick(true); setInterval(tick,60000);
  var poll=+document.getElementById('guide').dataset.poll;
  if(poll>0&&location.protocol.indexOf('http')===0){
    setInterval(refresh,poll*1000);
    document.addEventListener('visibilitychange',function(){if(!document.hidden) refresh();});
  }
})();
</script>
""")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def local_tz(name):
    if not name:
        return dt.datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except Exception as ex:
        sys.exit(f"unknown timezone {name!r}: {ex}")


def default_window(today: dt.date, offset: int):
    """Tuesday-through-Monday football week containing (or following) today."""
    start = today - dt.timedelta(days=(today.weekday() - 1) % 7)   # back to Tuesday
    start += dt.timedelta(weeks=offset)
    return start, start + dt.timedelta(days=6)


def week_nav(args, shown):
    """Prev / next links for a served page: ESPN week numbers in --week mode, else offsets."""
    if args.week:
        links = [("&lsaquo; Prev", f"?week={args.week - 1}") if args.week > 1 else None,
                 ("Next &rsaquo;", f"?week={args.week + 1}")]
    else:
        links = [("&lsaquo; Prev", f"?offset={shown - 1}"), ("Next &rsaquo;", f"?offset={shown + 1}")]
    if getattr(args, "navigated", False):
        links.insert(1, ("This week", "/"))
    return '<span class="wnav">' + "".join(f'<a href="{h}">{t}</a>' for t, h in filter(None, links)) + "</span>"


def nav_args(args, query: str):
    """Copy of args for one request, with ?offset=N or ?week=N applied."""
    req = argparse.Namespace(**vars(args))
    req.navigated = False
    q = urllib.parse.parse_qs(query)
    try:
        if "week" in q:
            req.week, req.date, req.navigated = max(1, int(q["week"][0])), None, True
        elif "offset" in q:
            req.offset, req.navigated = max(-52, min(52, int(q["offset"][0]))), True
    except ValueError:
        pass
    return req


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build a TV-guide page of the week's college football games.")
    w = p.add_mutually_exclusive_group()
    w.add_argument("--week", type=int, help="ESPN week number (uses ESPN's own week boundaries)")
    w.add_argument("--date", help="YYYY-MM-DD; show the football week containing this date")
    p.add_argument("--offset", type=int, default=0, help="shift the default week by N weeks (-1 = last week)")
    p.add_argument("--year", type=int, help="season year (default: current)")
    p.add_argument("--seasontype", type=int, default=2, choices=[1, 2, 3],
                   help="1=preseason 2=regular 3=postseason (default 2)")
    p.add_argument("--division", default="fbs", choices=sorted(DIVISIONS), help="default fbs")
    p.add_argument("--groups", help="raw ESPN group id, overrides --division")
    p.add_argument("--tz", help="IANA timezone, e.g. America/Denver (default: system)")
    p.add_argument("--game-len", type=int, default=210, help="assumed game window in minutes (default 210)")
    p.add_argument("--slot", type=int, default=15, choices=[10, 15, 20, 30], help="grid granularity (default 15)")
    p.add_argument("--day-rollover", type=int, default=4,
                   help="hour at which a TV day ends, so 12:30am kickoffs stay on Saturday (default 4)")
    p.add_argument("--hide-streaming", action="store_true", help="drop ESPN+/SECN+/Peacock-only games")
    p.add_argument("--ranked-only", action="store_true", help="only games involving an AP/CFP top-25 team")
    p.add_argument("--network", action="append", default=[], help="only these networks (repeatable, substring)")
    p.add_argument("--channels", help=f"JSON map of network to channel number (default {CHANNELS}, if present)")
    p.add_argument("--team", action="append", default=[],
                   help="favorite team: ESPN id, abbreviation or name (repeatable, exact match)")
    p.add_argument("--logos", default="link", choices=["none", "link", "embed"],
                   help="team logos: link to ESPN CDN (default), embed as data URIs, or omit")
    p.add_argument("--cache-ttl", type=int, default=None,
                   help="seconds to reuse cached API responses (default: adaptive, 5 min while a game "
                        "is on; 0 = never)")
    p.add_argument("--now", help="pretend it is this local time, YYYY-MM-DDTHH:MM (testing)")
    p.add_argument("-o", "--out", default="cfb-week.html", help="output HTML path")
    p.add_argument("--serve", action="store_true", help="run a local web server that rebuilds on each load")
    p.add_argument("--host", default="127.0.0.1", help="address to bind with --serve (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port for --serve (default 8080)")
    p.add_argument("--open", action="store_true", help="open the page in a browser when done")
    p.add_argument("-q", "--quiet", action="store_true")
    return p.parse_args(argv)


def current_time(args, tz):
    if not args.now:
        return dt.datetime.now(tz)
    t = dt.datetime.fromisoformat(args.now)
    return t.replace(tzinfo=tz) if t.tzinfo is None else t.astimezone(tz)


def build_page(args, tz, tzname, now, poll=0, favs=()):
    """Fetch, filter and lay out one week; return (html, n_games, n_days). Raises NoGames.

    favs: extra favorite-team keys on top of --team, from the served page's cookie.
    """
    ttl = args.cache_ttl
    today = (now - dt.timedelta(hours=args.day_rollover)).date()     # TV day, not calendar day
    year = args.year or (today.year if today.month >= 2 else today.year - 1)
    groups = [args.groups] if args.groups else DIVISIONS[args.division]

    if args.week:
        s, e, label = espn_week_bounds(year, args.week, args.seasontype, ttl, args.quiet, now)
        win_start, win_end = s.astimezone(tz).date(), e.astimezone(tz).date()
        week_label = label
    else:
        base = dt.date.fromisoformat(args.date) if args.date else today
        win_start, win_end = default_window(base, args.offset)
        week_label = None

    def collect(ws, we):
        # widen a day each side in UTC, then clip by local TV day
        fs = dt.datetime.combine(ws, dt.time(), tz).astimezone(dt.timezone.utc) - dt.timedelta(days=1)
        fe = dt.datetime.combine(we, dt.time(23, 59), tz).astimezone(dt.timezone.utc) + dt.timedelta(days=1)
        if not args.quiet and not poll:          # served pages rebuild every minute
            print(f"window {ws} .. {we} ({tzname}), division={args.division}", file=sys.stderr)
        events, wk = fetch_range(fs, fe, groups, ttl, args.quiet, now)
        out = []
        for ev in events:
            try:
                g = Game(ev, tz, args.game_len)
            except (KeyError, IndexError, ValueError):
                continue
            if not (ws <= g.tv_day(args.day_rollover) <= we):
                continue
            if args.ranked_only and not g.ranked:
                continue
            if args.hide_streaming and (g.network == "TBD" or any(h in g.network for h in STREAMING_HINTS)):
                continue
            if args.network and not any(n.lower() in g.network.lower() for n in args.network):
                continue
            out.append(g)
        return out, wk

    games, espn_week = collect(win_start, win_end)

    # offseason or bye-heavy stretch: roll forward to the next week with games
    # never for a week in the past: "last week" should not quietly become next week
    shown = args.offset
    if not games and not args.week and not args.date and args.offset >= 0:
        for ahead in range(1, 5):
            ws, we = default_window(today, args.offset + ahead)
            games, espn_week = collect(ws, we)
            if games:
                win_start, win_end, shown = ws, we, args.offset + ahead
                if not args.quiet:
                    print(f"  (nothing scheduled this week; showing {ws})", file=sys.stderr)
                break

    if not games:
        raise NoGames("No games found. Try --week N, --offset 1, or --division all.", shown)

    stretch_live(games, now, args.slot)
    mark_favs(games, [*args.team, *favs])
    channels = getattr(args, "channel_map", {})
    for g in games:
        g.channel = channels.get(g.network.lower(), "")

    by_day = defaultdict(list)
    for g in games:
        by_day[g.tv_day(args.day_rollover)].append(g)

    logos = Logos(args.logos, args.quiet)
    grid_offset, n_slots = shared_window(by_day, tz, args.slot)
    in_week = win_start <= today <= win_end
    days_html = "".join(
        render_day(day, by_day[day], args.slot, logos, tzname, day == today,
                   day_start(day, tz) + dt.timedelta(minutes=grid_offset), n_slots,
                   collapsed=in_week and day < today)
        for day in day_order(by_day, today, win_start, win_end)
    )
    logo_css = logos.css()

    if week_label is None and espn_week:
        week_label = f"Week {espn_week}"
    if week_label and week_label.lower().startswith("week "):
        heading = f'<span class="wk">Week</span><span class="wknum">{esc(week_label[5:])}</span>'
        title = f"College Football {week_label}"
    else:
        heading = f'<span class="wk">{esc(week_label or "Schedule")}</span>'
        title = f"College Football {week_label or 'Schedule'}"

    ranked = sum(1 for g in games if g.ranked)
    networks = len({g.network for g in games})
    stats = "".join(
        f'<div class="stat"><b>{v}</b><span>{k}</span></div>'
        for k, v in (("games", len(games)), ("ranked", ranked),
                     ("networks", networks), ("days", len(by_day)))
    )
    rows = "networks" + (" &mdash; today: on now, then next kickoff" if today in by_day else "")

    teamsbtn = picker = ""
    if poll:                                     # favorites ride a cookie, so served pages only
        teams = {t.id: t.full for g in games for t in (g.home, g.away) if t.id}
        opts = "".join(f'<option value="{esc(n)}" data-id="{esc(i)}">'
                       for i, n in sorted(teams.items(), key=lambda x: x[1]))
        teamsbtn = '<button class="tg" id="teams" type="button">Teams</button>'
        picker = (f'<div class="picker" id="picker" hidden><label for="teamq">Your teams</label>'
                  f'<input id="teamq" list="teamopts" placeholder="Add a team playing this week" '
                  f'autocomplete="off"><datalist id="teamopts">{opts}</datalist>'
                  f'<div class="favlist" id="favlist"></div></div>')

    page = PAGE.substitute(
        title=title,
        heading=heading,
        range=f"{win_start:%b %-d} &ndash; {win_end:%b %-d, %Y}",
        nav=week_nav(args, shown) if poll else "",
        stats=stats,
        rows=rows,
        tzname=esc(tzname),
        gamelen=args.game_len,
        days=days_html,
        teamsbtn=teamsbtn,
        picker=picker,
        logocss=logo_css,
        ngames=len(games),
        generated=now.strftime("%b %-d, %Y at %-I:%M %p %Z"),
        refresh=" &middot; refreshes every 5 min while games are on" if poll else "",
        now=now.isoformat(),
        poll=poll,
    )
    return page, len(games), len(by_day)


def cookie_favs(header: str):
    """ESPN team ids from the served page's cfb-teams cookie: [[id, name], ...]."""
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k != "cfb-teams":
            continue
        try:
            items = json.loads(urllib.parse.unquote(v))
        except ValueError:
            return []
        return [str(f[0]) for f in items if isinstance(f, list) and f] if isinstance(items, list) else []
    return []


def serve(args, tz, tzname):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path not in ("/", "/index.html"):
                self.send_error(404)
                return
            req = nav_args(args, query)
            code = 200
            try:
                favs = cookie_favs(self.headers.get("Cookie", ""))
                body, _, _ = build_page(req, tz, tzname, current_time(req, tz), poll=POLL_SECONDS, favs=favs)
            except NoGames as ex:
                body = f"<title>College Football</title><p>{esc(ex)}</p><p>{week_nav(req, ex.offset)}</p>"
            except (urllib.error.URLError, OSError, ValueError) as ex:
                code, body = 502, f"<title>College Football</title><p>ESPN fetch failed: {esc(ex)}</p>"
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *a):           # page polls every minute; keep stderr for fetches
            pass

    srv = HTTPServer((args.host, args.port), Handler)
    host = "localhost" if args.host in ("127.0.0.1", "::1", "") else args.host
    url = f"http://{host}:{args.port}/"
    print(f"serving {url}  (ctrl-c to stop)", file=sys.stderr)
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def main(argv=None):
    args = parse_args(argv)
    tzname_arg = args.tz or os.environ.get("CFB_TZ")
    tz = local_tz(tzname_arg)
    tzname = tzname_arg or (dt.datetime.now(tz).tzname() or "local")
    if not tzname_arg and dt.datetime.now(tz).utcoffset() == dt.timedelta(0) and not args.quiet:
        print("note: this machine is on UTC; kickoff times will look shifted. "
              "Use --tz America/New_York (or set CFB_TZ).", file=sys.stderr)

    # read once at startup, so a bad map fails here rather than inside a served request
    args.channel_map = load_channels(args.channels or CHANNELS, required=bool(args.channels))

    if args.serve:
        return serve(args, tz, tzname)

    try:
        page, n_games, n_days = build_page(args, tz, tzname, current_time(args, tz))
    except NoGames as ex:
        print(ex, file=sys.stderr)
        return 1
    out = Path(args.out).expanduser().resolve()
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}  ({n_games} games, {n_days} days)")
    if args.open:
        webbrowser.open(out.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
