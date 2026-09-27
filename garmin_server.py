#!/usr/bin/env python3
"""
HYROX Coaching Dashboard — proxy server (deployed on Render).

Local:   python garmin_server.py            -> http://localhost:8765/
Render:  uvicorn garmin_server:app --host 0.0.0.0 --port $PORT

Memory controls (optional env vars):
  MAX_CACHED_RACES   races kept in memory, least-recently-used evicted first (default 100)
  SEARCH_WORKERS     races loaded at the same time during a search (default 3)
(glibc arena cap + malloc_trim are applied in code, so no MALLOC_ARENA_MAX env var is needed)
"""

import asyncio
import gc
import json
import math
import os
import re
import threading
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

VERSION = "3.5.0"
_HERE = os.path.dirname(os.path.abspath(__file__))

MAX_CACHED_RACES = int(os.environ.get("MAX_CACHED_RACES", "100"))
SEARCH_WORKERS   = int(os.environ.get("SEARCH_WORKERS", "3"))


# ─────────────────────────────────────────────────────────────────────────────
# Memory hygiene. Measured on Render: a cold 106-race search peaked at 535 MB
# while the race cache itself was only 78 MB — the rest was freed parser memory
# that Arrow's allocator and glibc's per-thread arenas never handed back.
# ─────────────────────────────────────────────────────────────────────────────

_libc = None
try:
    import ctypes
    _libc = ctypes.CDLL("libc.so.6")
    _libc.mallopt(-8, 2)          # M_ARENA_MAX = 2 (same effect as MALLOC_ARENA_MAX=2)
except Exception:
    _libc = None                  # not glibc (e.g. local Windows run)

try:
    import pyarrow as _pa
    _pa.set_memory_pool(_pa.system_memory_pool())   # use malloc so trimming works
except Exception:
    _pa = None


def _release_memory():
    gc.collect()
    if _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass


MAX_LOCATIONS_PER_SEARCH = 150

app = FastAPI(title="HYROX Coaching Proxy", version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Bounded pool: at most SEARCH_WORKERS full races are being downloaded/parsed at once,
# which caps peak memory during a "select all" search.
_executor = ThreadPoolExecutor(max_workers=SEARCH_WORKERS)


# ─────────────────────────────────────────────────────────────────────────────
# pyrox client + slim LRU race cache
# ─────────────────────────────────────────────────────────────────────────────

_pyrox_client = None
_client_lock  = threading.Lock()

_race_cache: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
_cache_lock  = threading.Lock()
_fetch_locks: dict = {}


def get_pyrox():
    global _pyrox_client
    with _client_lock:
        if _pyrox_client is None:
            import pyrox
            _pyrox_client = pyrox.PyroxClient()
    return _pyrox_client


STATION_KEYWORDS = ["ski", "sled", "burpee", "row", "farmer", "sandbag", "wall", "run"]


def detect_columns(df):
    cols = {c.lower(): c for c in df.columns}
    result = {}
    for key, candidates in {
        "total_time":   ["total_time", "total", "time", "finish_time"],
        "athlete_name": ["athlete_name", "name", "athlete", "full_name"],
        "gender":       ["gender", "sex"],
        "division":     ["division", "category", "cat"],
    }.items():
        for c in candidates:
            if c in cols:
                result[key] = cols[c]
                break
    result["splits"] = [c for c in df.columns if any(k in c.lower() for k in STATION_KEYWORDS)]
    return result


def slim_df(raw: pd.DataFrame) -> pd.DataFrame:
    """Keep only the columns the dashboard uses, as compact dtypes.
    Typically cuts a race from several MB to a few hundred KB."""
    cols = detect_columns(raw)
    keep = [cols[k] for k in ("athlete_name", "total_time", "gender", "division") if cols.get(k)]
    keep = list(dict.fromkeys(keep + cols["splits"]))
    out = raw[keep].copy()
    numeric = cols["splits"] + ([cols["total_time"]] if cols.get("total_time") else [])
    for c in numeric:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float32")
    for k in ("gender", "division"):
        if cols.get(k):
            out[cols[k]] = out[cols[k]].astype(str).astype("category")
    return out.reset_index(drop=True)


def get_race_df(season: int, location: str) -> pd.DataFrame:
    """Full race (all divisions) as a slim DataFrame, via the LRU cache.
    Filtering by gender/division happens in pandas, so each race is cached once."""
    key = (int(season), location.lower())
    with _cache_lock:
        if key in _race_cache:
            _race_cache.move_to_end(key)
            return _race_cache[key]
        key_lock = _fetch_locks.setdefault(key, threading.Lock())

    with key_lock:  # stops two threads downloading the same race at once
        with _cache_lock:
            if key in _race_cache:
                _race_cache.move_to_end(key)
                return _race_cache[key]
        print(f"[pyrox] fetching S{season} {location}")
        raw  = get_pyrox().get_race(season=int(season), location=location)
        slim = slim_df(raw)
        del raw
        _release_memory()
        with _cache_lock:
            _race_cache[key] = slim
            while len(_race_cache) > MAX_CACHED_RACES:
                _race_cache.popitem(last=False)
            _fetch_locks.pop(key, None)
        print(f"[pyrox] cached S{season} {location}: {len(slim)} rows")
        return slim


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def fmt_time(minutes):
    m = _num(minutes)
    if m is None:
        return "N/A"
    total_seconds = int(round(m * 60))
    h, rem = divmod(total_seconds, 3600)
    mm, s = divmod(rem, 60)
    return f"{h}:{mm:02d}:{s:02d}" if h else f"{mm}:{s:02d}"


def fmt_delta(minutes):
    m = _num(minutes)
    if m is None:
        return "N/A"
    return ("+" if m >= 0 else "-") + fmt_time(abs(m))


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# ─────────────────────────────────────────────────────────────────────────────
# Result building — benchmarks scoped to the athlete's own division + gender
# ─────────────────────────────────────────────────────────────────────────────

def cohort_for(df, cols, row):
    """Athletes in the same division (and gender, where available) as `row`."""
    cohort = df
    for k in ("division", "gender"):
        col = cols.get(k)
        if not col:
            continue
        val = str(row[col]).lower()
        mask = cohort[col].astype(str).str.lower() == val
        if mask.any():
            cohort = cohort[mask]
    return cohort


def build_result(row, df, cols, season, location):
    name_col, total_col = cols.get("athlete_name"), cols.get("total_time")
    gender_col, division_col = cols.get("gender"), cols.get("division")
    split_cols = cols.get("splits", [])

    total  = _num(row[total_col]) if total_col else None
    cohort = cohort_for(df, cols, row)
    finish = cohort[total_col].dropna().astype(float) if total_col else pd.Series(dtype=float)
    field_size = int(len(finish)) or int(len(cohort))

    rank = None
    if total is not None and len(finish):
        rank = int((finish < total).sum()) + 1

    splits = []
    if split_cols:
        medians = cohort[split_cols].median()
        top10   = cohort[split_cols].quantile(0.10)
        for col in split_cols:
            av, med, top = _num(row[col]), _num(medians[col]), _num(top10[col])
            vs_med = av - med if av is not None and med is not None else None
            vs_top = av - top if av is not None and top is not None else None
            splits.append({
                "station":            col.replace("_time", "").replace("_", " ").title(),
                "time":               fmt_time(av),
                "median":             fmt_time(med),
                "vs_median":          fmt_delta(vs_med),
                "top_10_pct":         fmt_time(top),
                "vs_top_10":          fmt_delta(vs_top),
                "faster_than_median": bool(vs_med is not None and vs_med < 0),
            })

    benchmarks = {}
    if total is not None and len(finish):
        faster = int((finish < total).sum())
        median = float(finish.median())
        q10    = float(finish.quantile(0.10))
        benchmarks = {
            "top_percent":   100 - round(faster / len(finish) * 100),
            "median":        fmt_time(median),
            "top_25_pct":    fmt_time(finish.quantile(0.25)),
            "top_10_pct":    fmt_time(q10),
            "top_5_pct":     fmt_time(finish.quantile(0.05)),
            "gap_to_median": fmt_delta(total - median),
            "gap_to_top_10": fmt_delta(total - q10),
        }

    return {
        "athlete":    str(row[name_col]) if name_col else "Unknown",
        "race":       f"Season {season} — {location.replace('-', ' ').title()}",
        "location":   location,
        "total_time": fmt_time(total),
        "rank":       rank,            # within division + gender
        "field_size": field_size,      # finishers in division + gender
        "gender":     str(row[gender_col]) if gender_col else "",
        "division":   str(row[division_col]) if division_col else "",
        "splits":     splits,
        "benchmarks": benchmarks,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

def apply_filters(df, cols, gender, division):
    # Exact match on normalised values: data uses open / pro / doubles / pro_doubles,
    # so "pro" must not also pick up "pro_doubles".
    if gender and cols.get("gender"):
        df = df[df[cols["gender"]].astype(str).map(_norm) == _norm(gender)]
    if division and cols.get("division"):
        df = df[df[cols["division"]].astype(str).map(_norm) == _norm(division)]
    return df


def name_mask(names_lower: pd.Series, last: str, first: Optional[str]):
    """Coarse filter: whole-word last name, first name at a word start, anywhere in the string."""
    mask = names_lower.str.contains(rf"\b{re.escape(last.lower().strip())}\b", regex=True, na=False)
    if first:
        mask &= names_lower.str.contains(rf"\b{re.escape(first.lower().strip())}", regex=True, na=False)
    return mask


def person_match(name: str, division: str, last: str, first: Optional[str]) -> bool:
    """Precise check on a coarse match.
    Team rows hold both athletes ("Mitch Williams, Darko Radakovic"), so first and last
    must sit in the same comma-separated person — otherwise "Gabi Mitchem, Lewis Williams"
    would match Mitch Williams. Individual rows are "Last, First" and match as a whole."""
    if not first:
        return True
    is_team = any(k in str(division).lower() for k in ("doubles", "relay"))
    if not is_team:
        return True
    parts = [p.strip() for p in str(name).lower().split(",")]
    if all(" " not in p for p in parts):
        return True   # single-person "Last, First" form (some doubles rows list one athlete)
    last_re  = re.compile(rf"\b{re.escape(last.lower().strip())}\b")
    first_re = re.compile(rf"\b{re.escape(first.lower().strip())}")
    return any(last_re.search(p) and first_re.search(p) for p in parts)


def lookup_one_sync(loc, season, last, first=None, gender=None, division=None,
                    partner_last=None, partner_first=None):
    """One location. Returns [] if the race can't be loaded; a bad row is skipped, not the race."""
    try:
        df = get_race_df(season, loc)
    except Exception as e:
        print(f"[search] S{season} {loc}: could not load — {e}")
        return []

    cols = detect_columns(df)
    name_col = cols.get("athlete_name")
    if not name_col:
        return []

    candidates = apply_filters(df, cols, gender, division)
    names = candidates[name_col].astype(str).str.lower()
    mask  = name_mask(names, last, first)
    if partner_last:
        mask |= name_mask(names, partner_last, partner_first)

    div_col = cols.get("division")
    results, seen = [], set()
    for _, row in candidates[mask].iterrows():
        name = str(row[name_col])
        div  = str(row[div_col]) if div_col else ""
        if not (person_match(name, div, last, first)
                or (partner_last and person_match(name, div, partner_last, partner_first))):
            continue
        try:
            r = build_result(row, df, cols, season, loc)
        except Exception as e:
            print(f"[search] S{season} {loc}: skipped row — {e}")
            continue
        key = (r["athlete"], r["division"], r["total_time"])   # source data has some duplicate rows
        if key not in seen:
            seen.add(key)
            results.append(r)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────────

class HyroxRequest(BaseModel):
    last_name:  str
    first_name: Optional[str] = None
    season:     int
    location:   str
    gender:     Optional[str] = None
    division:   Optional[str] = None


class HyroxSearchAllRequest(BaseModel):
    last_name:          str
    first_name:         Optional[str] = None
    season:             int
    locations:          list
    gender:             Optional[str] = None
    division:           Optional[str] = None
    partner_last_name:  Optional[str] = None   # doubles are stored under one partner's name
    partner_first_name: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Memory reporting
# ─────────────────────────────────────────────────────────────────────────────

def _memory_mb():
    info = {"rss_mb": None, "peak_rss_mb": None}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    info["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                elif line.startswith("VmHWM:"):
                    info["peak_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass  # not Linux (e.g. local Windows run)
    with _cache_lock:
        frames = list(_race_cache.values())
    info["cache_mb"] = round(sum(int(d.memory_usage(deep=True).sum()) for d in frames) / 1e6, 1)
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open(os.path.join(_HERE, "hyrox_coaching_dashboard.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
def health():
    with _cache_lock:
        cached = len(_race_cache)
    return {
        "status": "ok", "service": "hyrox-coaching-proxy", "version": VERSION,
        "cached_races": cached, "max_cached_races": MAX_CACHED_RACES,
        "search_workers": SEARCH_WORKERS, "memory": _memory_mb(),
    }


@app.get("/locations")
def list_locations(season: Optional[int] = None):
    """Every race in the pyrox index (?season=9 to filter). The dashboard builds its race list from this."""
    try:
        df = get_pyrox().list_races(season=season)
        races = [{"season": int(r["season"]), "location": str(r["location"])} for _, r in df.iterrows()]
        return {"status": "ok", "count": len(races), "filter_season": season, "races": races}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/race-check")
def race_check(season: int = 8, location: str = "shanghai"):
    """Is a race loaded, and how big is it? e.g. /race-check?season=8&location=shanghai"""
    try:
        df = get_race_df(season, location)
        cols = detect_columns(df)
        name_col = cols.get("athlete_name")
        return {
            "status": "ok", "season": season, "location": location, "rows": len(df),
            "sample_names": df[name_col].head(10).astype(str).tolist() if name_col else [],
            "memory_kb": round(int(df.memory_usage(deep=True).sum()) / 1024, 1),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "note": "Location may not be in the pyrox index yet."}


@app.get("/debug")
def debug_lookup(season: int = 8, location: str = "sydney"):
    """Raw column names, real division/gender values and slim-vs-raw memory for one race (not cached)."""
    try:
        raw  = get_pyrox().get_race(season=season, location=location)
        cols = detect_columns(raw)
        slim = slim_df(raw)
        values = {}
        for k in ("division", "gender"):
            if cols.get(k):
                vc = raw[cols[k]].astype(str).value_counts()
                values[k] = {str(i): int(n) for i, n in vc.items()}
        return {
            "status": "ok", "season": season, "location": location, "rows": len(raw),
            "columns": [str(c) for c in raw.columns],
            "detected": {k: v for k, v in cols.items()},
            "values": values,
            "raw_mb":  round(int(raw.memory_usage(deep=True).sum()) / 1e6, 2),
            "slim_mb": round(int(slim.memory_usage(deep=True).sum()) / 1e6, 2),
            # to_json turns NaN into null (pandas 3 keeps NaN even after astype(str))
            "sample": json.loads(raw.head(2).to_json(orient="records", date_format="iso")),
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


@app.get("/search-test")
def search_test(last: str = "Williams", first: str = "Mitch", season: int = 8, location: str = "sydney"):
    """What the dashboard would receive for one location."""
    try:
        results = lookup_one_sync(location, season, last, first)
        return {"found": bool(results), "count": len(results), "results": results}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


@app.post("/hyrox/search-all")
async def hyrox_search_all(req: HyroxSearchAllRequest):
    """Search many locations in one request. Work runs on a bounded thread pool."""
    locs = [str(l) for l in req.locations][:MAX_LOCATIONS_PER_SEARCH]
    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(
            _executor, lookup_one_sync,
            loc, req.season, req.last_name, req.first_name, req.gender, req.division,
            req.partner_last_name, req.partner_first_name,
        )
        for loc in locs
    ]
    all_lists = await asyncio.gather(*tasks)
    results = [r for lst in all_lists for r in lst]
    return {"success": True, "results": results, "count": len(results)}


@app.post("/hyrox/lookup")
def hyrox_lookup(req: HyroxRequest):
    """Single-location lookup (kept for backward compatibility)."""
    try:
        results = lookup_one_sync(req.location, req.season, req.last_name, req.first_name,
                                  req.gender, req.division)
        if not results:
            name = f"{req.first_name} {req.last_name}" if req.first_name else req.last_name
            return {"success": False, "error": f"No athlete matching '{name}' found at {req.location} S{req.season}."}
        return {"success": True, "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print(f"  HYROX Coaching Dashboard — proxy server v{VERSION}")
    print("  Dashboard:  http://localhost:8765/")
    print("  Health:     GET  /health          (includes memory use)")
    print("  Locations:  GET  /locations?season=9")
    print("  Race check: GET  /race-check?season=8&location=shanghai")
    print("  Debug:      GET  /debug?season=8&location=sydney")
    print("  Search:     POST /hyrox/search-all")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
