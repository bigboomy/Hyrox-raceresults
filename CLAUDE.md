# HYROX Coaching Dashboard

A local web app for searching HYROX athlete results, comparing race splits, and overlaying Garmin biometrics.

## Architecture

- **dashboard.html** — single-file frontend, opened directly in Chrome via `file://`
- **server.py** — FastAPI proxy on `localhost:8765`, uses `pyrox-client` to fetch race data
- No build step, no dev server, no bundler

## server.py

FastAPI app on port 8765 with CORS open to all origins (required for `file://` requests).

**Endpoints:**
- `GET /health` → `{"status":"ok","service":"hyrox-coaching-proxy","port":8765}`
- `POST /hyrox/lookup` → accepts `HyroxRequest`, returns `{success, results[]}`

**HyroxRequest fields:**
| Field | Type | Required |
|---|---|---|
| last_name | str | yes |
| first_name | str | no |
| season | int | yes |
| location | str | yes (slug, e.g. "london-excel") |
| gender | str | no ("male"/"female") |
| division | str | no ("open"/"pro"/etc.) |

**Race cache:** results are cached in `_race_cache` dict keyed by `(season, location, gender, division)`. The pyrox client is lazy-initialised on first use. First lookup for a race fetches the full field; subsequent lookups for the same race are instant.

**Column detection:** `detect_columns()` handles varied column naming across seasons (e.g. `total_time` vs `time` vs `finish_time`). Split columns are identified by station keywords: ski, sled, burpee, row, farmer, sandbag, wall, run.

**Result shape** (one entry per matching athlete row):
```json
{
  "athlete": "Smith John",
  "race": "Season 7 — London",
  "total_time": "1:12:34",
  "rank": 42,
  "field_size": 850,
  "gender": "male",
  "division": "open",
  "splits": [{"station":"Ski Erg","time":"4:12","median":"4:45","vs_median":"-0:33","top_10_pct":"3:58","vs_top_10":"+0:14","faster_than_median":true}],
  "benchmarks": {"top_percent":85,"median":"1:18:00","top_10_pct":"1:05:00","gap_to_top_10":"+7:26",...}
}
```

## dashboard.html

Opened via `file://` — no server needed to serve it. Calls `localhost:8765` for all data.

**Key flows:**
1. User enters last name (+ optional first name), picks season, selects race locations from chip picker
2. `doSearch()` fires parallel `lookupAthlete()` calls (one per selected location)
3. `lookupAthlete()` POSTs to `POST /hyrox/lookup` — returns null on miss, results array on hit
4. Client-side name validation filters results: returned athlete name must contain the searched last name (and first name if provided) — guards against pyrox returning near-matches
5. Results render as cards; user pins up to 6 races
6. Pinned races render: split heatmap vs division median, gap-to-top-10% bar chart, optional Garmin biometrics overlay

**Pin system:** up to 6 pinned races stored in `pinned[]`. Each has a colour from `PALETTE`. Comparison views re-render on every pin/unpin.

**Server health badge:** polls `GET /health` every 15 seconds, shows green "online" / amber "offline" badge.

**Garmin modal:** calls `POST /hyrox/garmin/data` (not yet implemented in server.py — placeholder for future Garmin Connect integration).

## Running

```
# Start server
start_server.bat          (Windows double-click)
# or
python server.py

# Open dashboard
# In Chrome: File > Open File > dashboard.html
# or drag dashboard.html onto Chrome
```

## Known constraints

- **dashboard.html must be opened via `file://`**, not served by a dev server or live-reload tool. CORS is open on the server specifically to allow `file://` origin.
- **Server must be running before searching.** The health badge shows current status.
- **Race data fetches are slow on first lookup** (pyrox downloads full field data). Subsequent lookups for the same race are cached in memory for the server's lifetime.
- **Garmin biometrics overlay** requires `POST /garmin/data` — not yet implemented in this version of server.py. The UI button and modal are present but will return an error until implemented.
