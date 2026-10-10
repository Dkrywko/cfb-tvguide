# cfb-tvguide

A week of college football laid out like a TV guide: rows are networks, columns are
time, each game a block spanning its kickoff window. Data from ESPN's public scoreboard
API. Python 3.9+ standard library only.

```
./cfb_tvguide.py --serve --tz America/New_York --open    # live guide, http://localhost:8080
./cfb_tvguide.py --tz America/New_York -o week.html      # one-off page
```

## Live mode

`--serve` runs a local web server that rebuilds the page on every load. The open page
fetches a fresh copy every 60s and swaps it in place, keeping scroll position.
Prev / Next links under the date range step through weeks (`?offset=N`, or `?week=N`
when started with `--week`).

Today's grid is ordered for "what's on now":

1. Networks with a game in progress, close games first, in lineup order
2. Networks with a game still to come, soonest kickoff first, lineup order on ties
3. Networks finished for the day, in lineup order

A game is **close** when it is in the 4th quarter or overtime and within 8 points. It gets
a badge, and its network moves to the front of group 1.

ESPN+ carries many games at once, so on today's grid it splits in two: the games in
progress get their own ESPN+ row in group 1, and the rest stay in a second ESPN+ row.

Other days keep plain lineup order: over-the-air, national cable, conference networks,
streaming, TBD.

Favorite teams get a star, a highlight, and a "Your teams" row at the top of each day.
On a served page, pick them with the Teams button; they're kept in a cookie. For a
written file, use `--team`.

During a week in progress, today's grid comes first, then the days still to come, then
earlier days newest-first. Past and future weeks stay in date order.

ESPN is re-checked every 5 minutes while a game is on, hourly when games are still to
come, and twice a day once a day's games are final. Each cached response sets its own
lifetime from its contents. A cold start is ~9 queries (FBS); while served, finished days
cost nothing, upcoming days one query an hour each, and only the day with a game on is
polled every 5 minutes.

## Flags

| Flag | What it does |
|---|---|
| *(none)* | The current Tue–Mon football week; rolls forward if that week is empty |
| `--serve` | Run the local server instead of writing a file |
| `--host` / `--port` | Server bind, default `127.0.0.1:8080` |
| `--week 5` | ESPN week 5, using ESPN's own week boundaries |
| `--date 2026-11-28` | The football week containing that date |
| `--offset 1` | Shift the default week (`-1` = last week). Only an empty current or future week rolls forward |
| `--tz America/Denver` | Kickoff times in this zone (default: system, or `$CFB_TZ`) |
| `--division fbs\|fcs\|all` | Default `fbs` |
| `--groups 8` | Raw ESPN group id, e.g. a single conference (8 = SEC) |
| `--ranked-only` | Only games with an AP/CFP top-25 team |
| `--network ESPN` | Keep only matching networks (repeatable, substring match) |
| `--channels FILE` | Network-to-channel map (default `~/.config/cfb-tvguide/channels.json`, if present) |
| `--team UGA` | Favorite team by ESPN id, abbreviation or name (repeatable, exact match) |
| `--hide-streaming` | Drop streaming-only games and TBD |
| `--logos none\|link\|embed` | `embed` inlines logos so a written file works offline |
| `--game-len 210` | Assumed block width in minutes |
| `--slot 15` | Grid granularity in minutes |
| `--day-rollover 4` | Hour a TV day ends, so a 12:30am kickoff stays on Saturday |
| `--cache-ttl N` | Fixed cache lifetime in seconds instead of adaptive (`0` = always fetch) |
| `--now 2026-09-26T15:30` | Pretend it is this local time (testing) |
| `-o out.html` / `--open` | Output path, and whether to open a browser |

## Channel numbers

An optional JSON object maps network names to your provider's channels. Keys match
case-insensitively; values are shown as written. It is read once at startup.

```json
{"ABC": "7.1", "ESPN": 206, "ESPN2": 209, "SEC Network": 611}
```

## Notes

- Team accent colors are pushed out of the narrow-band red range before use.
- Scoreboard queries go one day at a time with no `limit`; ESPN rejects date ranges and a
  large `limit` re-imposes a 25-event cap.
- A failed fetch falls back to the cached copy, so a flaky connection degrades to stale
  data rather than an error page.
- Responses and logos cache under `~/.cache/cfb-tvguide/`.
