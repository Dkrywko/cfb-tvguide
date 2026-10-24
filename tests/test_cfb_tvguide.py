"""Offline tests. Run from the repo root: python3 -m unittest"""

import colorsys
import copy
import datetime as dt
import json
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cfb_tvguide as c  # noqa: E402

TZ = ZoneInfo("America/New_York")
SAT = dt.date(2026, 10, 3)
# Trimmed ESPN scoreboard for Saturday 2026-10-03, FBS. All games final as captured.
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "scoreboard-20261003.json").read_text())


def events():
    return copy.deepcopy(FIXTURE["events"])


def games(evs=None):
    return [c.Game(e, TZ, 210) for e in (evs or events())]


def find(gs, abbr):
    return next(g for g in gs if abbr in (g.home.abbr, g.away.abbr))


def at(hour, minute=0, day=SAT):
    return dt.datetime.combine(day, dt.time(hour, minute), TZ)


def set_state(ev, state, period=0, home=None, away=None):
    comp = ev["competitions"][0]
    comp["status"]["type"]["state"] = state
    comp["status"]["period"] = period
    for x in comp["competitors"]:
        score = home if x["homeAway"] == "home" else away
        if score is not None:
            x["score"] = str(score)


def at_three():
    """3:00pm: noon games on, 3:30+ to come, one ESPN+ 11am game final."""
    evs = events()
    for ev in evs:
        g = c.Game(ev, TZ, 210)
        if g.start < at(12):
            set_state(ev, "post")
        elif g.start <= at(15):
            set_state(ev, "in", 3, 14, 10)
        else:
            set_state(ev, "pre")
    return evs


class TestGame(unittest.TestCase):
    def test_parses_fixture(self):
        gs = games()
        self.assertEqual(len(gs), len(FIXTURE["events"]))
        uga = find(gs, "UGA")
        self.assertEqual(uga.network, "SEC Network")
        self.assertEqual(uga.state, "post")
        self.assertEqual(uga.start.tzinfo, TZ)
        self.assertEqual(uga.end - uga.start, dt.timedelta(minutes=210))
        self.assertTrue(uga.ranked)
        self.assertEqual(uga.home.id, "61")

    def test_after_midnight_stays_on_saturday(self):
        haw = find(games(), "HAW")
        self.assertEqual(haw.start.date(), SAT)
        self.assertEqual(haw.tv_day(4), SAT)
        late = haw.start + dt.timedelta(hours=1)          # 00:59 Sunday
        haw.start = late
        self.assertEqual(haw.tv_day(4), SAT)

    def test_tba_uses_eastern_date(self):
        ev = events()[0]
        ev["competitions"][0]["timeValid"] = False
        g = c.Game(ev, ZoneInfo("America/Los_Angeles"), 210)
        self.assertTrue(g.time_tba)
        self.assertEqual(g.tv_day(4), SAT)


class TestRedSafe(unittest.TestCase):
    def hls(self, hexstr):
        r, g, b = (int(hexstr[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        return h * 360, l, s

    def test_pure_red_is_moved_and_muted(self):
        for red in ("#ff0000", "#e00000", "#cc0000"):
            hue, _, sat = self.hls(c.red_safe(red))
            self.assertGreaterEqual(hue, 10, red)          # toward salmon
            self.assertLessEqual(sat, 0.41, red)           # 1 - 0.60 at pure red, plus rounding

    def test_crimson_is_muted(self):
        # crimsons stay near 0 degrees, desaturated: kept by decision, see CLAUDE.md
        for red in ("#ba0c2f", "#9e1b32", "#c5050c", "#841617"):
            _, _, sat = self.hls(c.red_safe(red))
            self.assertLessEqual(sat, 0.52, red)

    def test_every_output_is_clamped(self):
        for hue in range(0, 360, 5):
            r, g, b = colorsys.hls_to_rgb(hue / 360, 0.5, 1.0)
            src = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
            for dark in (True, False):
                _, l, s = self.hls(c.red_safe(src, dark))
                self.assertLessEqual(s, 0.63)                # 0.62 cap, plus 8-bit rounding
                lo, hi = (0.42, 0.72) if dark else (0.30, 0.55)
                self.assertTrue(lo - 0.01 <= l <= hi + 0.01, (src, dark, l))

    def test_bad_input_falls_back(self):
        self.assertEqual(c.red_safe(""), "#8a8a86")
        self.assertEqual(c.red_safe("#12345"), "#8a8a86")


class TestNetworks(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(c.network_sort_key("Fox")[0], 0)            # ESPN's spelling
        self.assertEqual(c.network_sort_key("ESPN/Disney+")[0], 1)   # simulcast: best tier
        self.assertEqual(c.network_sort_key("SEC Network")[0], 2)
        self.assertEqual(c.network_sort_key("ESPN+")[0], 3)
        self.assertEqual(c.network_sort_key("Some New Net")[0], 2)
        self.assertEqual(c.network_sort_key("TBD")[0], 9)

    def test_streaming_only(self):
        self.assertTrue(c.streaming_only("ESPN+"))
        self.assertTrue(c.streaming_only("Peacock"))
        self.assertFalse(c.streaming_only("ESPN/Disney+"))
        self.assertFalse(c.streaming_only("NBC/Peacock"))


class TestLiveOrdering(unittest.TestCase):
    def day(self):
        return games(at_three())

    def test_groups_in_then_pre_then_post(self):
        rows = c.build_day(self.day(), 15, live=True)
        groups = [c.live_sort_key(*r)[0] for r in rows]
        self.assertEqual(groups, sorted(groups))
        self.assertEqual(set(groups), {0, 1, 2})

    def test_pre_sorted_by_kickoff(self):
        rows = c.build_day(self.day(), 15, live=True)
        starts = [c.live_sort_key(*r)[1] for r in rows if c.live_sort_key(*r)[0] == 1]
        self.assertEqual(starts, sorted(starts))

    def test_live_lineup_order(self):
        rows = c.build_day(self.day(), 15, live=True)
        live = [n for n, lanes in rows if c.live_sort_key(n, lanes)[0] == 0]
        self.assertEqual(live[:2], ["ABC", "CBS"])

    def test_espn_plus_splits(self):
        rows = c.build_day(self.day(), 15, live=True)
        plus = [lanes for n, lanes in rows if n == "ESPN+"]
        self.assertEqual(len(plus), 2)
        on_now = [g for lane in plus[0] for g in lane]
        self.assertTrue(all(g.state == "in" for g in on_now))

    def test_not_split_off_today(self):
        rows = c.build_day(self.day(), 15, live=False)
        self.assertEqual(sum(1 for n, _ in rows if n == "ESPN+"), 1)

    def test_close_game_leads_live_group(self):
        evs = events()
        for ev in evs:
            g = c.Game(ev, TZ, 210)
            set_state(ev, "in", 4, 35, 10) if g.start <= at(12) else set_state(ev, "pre")
        set_state(next(e for e in evs if "VAN" in json.dumps(e)), "in", 4, 21, 17)
        rows = c.build_day(games(evs), 15, live=True)
        self.assertEqual(rows[0][0], "SEC Network")


class TestClose(unittest.TestCase):
    def game(self, state, period, home, away):
        ev = events()[0]
        set_state(ev, state, period, home, away)
        return c.Game(ev, TZ, 210)

    def test_rule(self):
        self.assertTrue(self.game("in", 4, 21, 14).close)
        self.assertTrue(self.game("in", 5, 24, 24).close)       # overtime
        self.assertFalse(self.game("in", 4, 30, 21).close)      # 9 points
        self.assertFalse(self.game("in", 3, 21, 20).close)      # too early
        self.assertFalse(self.game("post", 4, 21, 20).close)

    def test_badge(self):
        html = c.render_game(self.game("in", 4, 21, 14), at(10), 15, 2, c.Logos("none", True))
        self.assertIn('class="close"', html)


class TestLayout(unittest.TestCase):
    def test_stretch_live(self):
        gs = games()
        g = find(gs, "UGA")
        g.state = "in"
        now = g.end + dt.timedelta(minutes=40)
        c.stretch_live(gs, now, 15)
        self.assertEqual(g.end, now + dt.timedelta(minutes=15))
        other = find(gs, "ALA")
        self.assertEqual(other.end - other.start, dt.timedelta(minutes=210))

    def test_pack_lanes_no_overlap(self):
        for lanes in (c.pack_lanes([g for g in games() if g.network == n], 15) for n in ("ESPN+", "MW+")):
            for lane in lanes:
                for a, b in zip(lane, lane[1:]):
                    self.assertLessEqual(a.end, b.start)

    def test_shared_window_spans_late_game(self):
        by_day = {SAT: games()}
        lo, n = c.shared_window(by_day, TZ, 15)
        self.assertEqual(lo % 60, 0)
        self.assertGreater(lo + n * 15, 24 * 60)       # 11:59pm kickoff runs past midnight

    def test_day_order(self):
        week = [dt.date(2026, 10, d) for d in (6, 7, 8, 9, 10)]
        tue, mon = dt.date(2026, 10, 6), dt.date(2026, 10, 12)
        day = lambda d: dt.date(2026, 10, d)
        self.assertEqual(c.day_order(week, day(8), tue, mon), [day(8), day(9), day(10), day(7), day(6)])
        self.assertEqual(c.day_order(week, day(10), tue, mon), [day(10), day(9), day(8), day(7), day(6)])
        self.assertEqual(c.day_order(week, day(11), tue, mon), [day(10), day(9), day(8), day(7), day(6)])
        self.assertEqual(c.day_order(week, day(20), tue, mon), week)      # past week
        self.assertEqual(c.day_order(week, day(1), tue, mon), week)       # future week


class TestFavorites(unittest.TestCase):
    def test_mark_by_any_key(self):
        for key in ("61", "uga", "Georgia", "georgia bulldogs"):
            gs = games()
            c.mark_favs(gs, [key])
            self.assertEqual([g.home.abbr for g in gs if g.fav], ["UGA"], key)
            self.assertTrue(find(gs, "UGA").home.fav)
            self.assertFalse(find(gs, "UGA").away.fav)

    def test_no_substring_match(self):
        gs = games()
        c.mark_favs(gs, ["Geo"])
        self.assertFalse(any(g.fav for g in gs))

    def test_cookie(self):
        v = urllib.parse.quote(json.dumps([["61", "Georgia Bulldogs"], ["333", "Alabama"]]))
        self.assertEqual(c.cookie_favs(f"cfb-theme=dark; cfb-teams={v}"), ["61", "333"])
        self.assertEqual(c.cookie_favs("cfb-teams=junk"), [])
        self.assertEqual(c.cookie_favs("cfb-teams=%7B%7D"), [])
        self.assertEqual(c.cookie_favs(""), [])

    def test_cookie_sort(self):
        self.assertEqual(c.cookie_sort("cfb-teams=%5B%5D; cfb-sort=channel", "now"), "channel")
        self.assertEqual(c.cookie_sort("cfb-sort=now", "channel"), "now")
        self.assertEqual(c.cookie_sort("cfb-sort=junk", "now"), "now")
        self.assertEqual(c.cookie_sort("", "channel"), "channel")


class TestServeHelpers(unittest.TestCase):
    def test_nav_args(self):
        args = c.parse_args([])
        self.assertEqual(c.nav_args(args, "offset=-2").offset, -2)
        self.assertEqual(c.nav_args(args, "offset=999").offset, 52)
        self.assertEqual(c.nav_args(args, "offset=x").offset, 0)
        req = c.nav_args(args, "week=0")
        self.assertEqual((req.week, req.navigated), (1, True))
        self.assertFalse(c.nav_args(args, "").navigated)

    def test_week_nav(self):
        args = c.parse_args([])
        self.assertNotIn("This week", c.week_nav(c.nav_args(args, ""), 0))
        nav = c.week_nav(c.nav_args(args, "offset=1"), 1)
        self.assertIn('href="?offset=0"', nav)
        self.assertIn('href="?offset=2"', nav)
        self.assertIn("This week", nav)
        self.assertNotIn("Prev", c.week_nav(c.nav_args(args, "week=1"), 0))

    def test_load_channels(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "channels.json"
            p.write_text('{"ESPN": 206, "SEC Network": "611"}')
            self.assertEqual(c.load_channels(p, True), {"espn": "206", "sec network": "611"})
            self.assertEqual(c.load_channels(Path(d) / "missing.json", False), {})
            with self.assertRaises(SystemExit):
                c.load_channels(Path(d) / "missing.json", True)
            p.write_text("[1, 2]")
            with self.assertRaises(SystemExit):
                c.load_channels(p, False)


class TestShelfLife(unittest.TestCase):
    def test_ttl(self):
        data = {"events": events()}
        self.assertEqual(c._shelf_life(data, at(13)), c.DONE_TTL)
        set_state(data["events"][0], "in", 2)
        self.assertEqual(c._shelf_life(data, at(13)), c.LIVE_TTL)
        data = {"events": events()}
        for ev in data["events"]:
            set_state(ev, "pre")
        self.assertEqual(c._shelf_life(data, at(6)), c.IDLE_TTL)
        self.assertEqual(c._shelf_life(data, at(13)), c.LIVE_TTL)      # kicked off, ESPN behind
        self.assertEqual(c._shelf_life({}, at(13)), c.WEEK_TTL)


class TestBuildPage(unittest.TestCase):
    """End to end on the fixture, with fetching stubbed out."""

    def build(self, now, argv=(), evs=None, **kw):
        args = c.parse_args(["--tz", "America/New_York", "--logos", "none", "-q", *argv])
        args.channel_map = kw.pop("channels", {})
        with mock.patch.object(c, "fetch_range", return_value=(evs or events(), 6)):
            return c.build_page(args, TZ, "America/New_York", now, **kw)

    def test_renders(self):
        html, n_games, n_days = self.build(at(13))
        self.assertEqual((n_games, n_days), (12, 1))
        self.assertIn("College Football Week 6", html)
        self.assertNotIn("$", html.split("<script>")[0].split("</style>")[1])   # every placeholder filled
        self.assertIn('data-today="1"', html)

    def test_day_order_on_page(self):
        evs = events()
        tue = copy.deepcopy(evs[0])
        tue["id"], tue["date"] = "1", "2026-09-29T23:00Z"
        sun = copy.deepcopy(evs[1])
        sun["id"], sun["date"] = "2", "2026-10-04T23:00Z"
        html, _, _ = self.build(at(13), evs=[tue, *evs, sun])
        days = [p.split('"')[0] for p in html.split('<details class="day" data-day="')[1:]]
        self.assertEqual(days, ["2026-10-03", "2026-10-04", "2026-09-29"])
        self.assertIn('data-day="2026-09-29">', html)          # past day collapsed
        self.assertIn('data-day="2026-10-04" open>', html)

    def test_file_has_no_picker_or_nav(self):
        html, _, _ = self.build(at(13))
        self.assertNotIn('id="picker"', html)
        self.assertNotIn('class="wnav"', html)

    def test_served_has_picker_and_nav(self):
        html, _, _ = self.build(at(13), poll=60, favs=["61"])
        self.assertIn('id="picker"', html)
        self.assertIn('class="wnav"', html)
        self.assertIn("Your teams", html)
        self.assertIn('data-id="61"', html)

    def test_team_flag_and_channels(self):
        html, _, _ = self.build(at(13), ["--team", "UGA"], channels={"sec network": "611"})
        self.assertIn('<span class="tm win mine">Georgia', html)
        self.assertIn('class="fnet">SEC Network 611<', html)
        self.assertIn('<span>SEC Network</span><span class="ch">611</span>', html)

    def test_hide_streaming_keeps_simulcast(self):
        html, n, _ = self.build(at(13), ["--hide-streaming"])
        self.assertIn("ESPN/Disney+", html)
        self.assertNotIn("<span>ESPN+</span>", html)
        self.assertEqual(n, 12 - 3 - 2)                         # three ESPN+, two MW+

    @staticmethod
    def nets(html):
        """Row labels of the first grid on the page, top to bottom."""
        grid = html.split('<div class="grid"')[1].split('class="nowline"')[0]
        return re.findall(r'<div class="net[^"]*"[^>]*><span>([^<]*)</span>', grid)

    def test_sort_channel_is_lineup_order(self):
        now_html, _, _ = self.build(at(15), evs=at_three())
        chan_html, _, _ = self.build(at(15), ["--sort", "channel"], evs=at_three())
        on_now, lineup = self.nets(now_html), self.nets(chan_html)
        self.assertEqual(on_now.count("ESPN+"), 2)              # split into on-now and the rest
        self.assertEqual(lineup.count("ESPN+"), 1)
        self.assertEqual(lineup, sorted(lineup, key=c.network_sort_key))
        self.assertEqual(set(on_now), set(lineup))
        self.assertNotEqual(list(dict.fromkeys(on_now)), lineup)   # same networks, reordered
        self.assertIn("today: on now", now_html)
        self.assertNotIn("today: on now", chan_html)

    def test_sort_toggle_served_with_today_only(self):
        html, _, _ = self.build(at(13), ["--sort", "channel"], poll=60)
        self.assertIn('name="sort" value="channel" aria-pressed="true"', html)
        self.assertIn('name="sort" value="now" aria-pressed="false"', html)
        written, _, _ = self.build(at(13))
        self.assertNotIn('name="sort"', written)                # needs a rebuild: served only
        past, _, _ = self.build(at(13, day=dt.date(2026, 10, 10)), ["--offset", "-1"], poll=60)
        self.assertNotIn('name="sort"', past)                   # no today grid to sort


if __name__ == "__main__":
    unittest.main()
