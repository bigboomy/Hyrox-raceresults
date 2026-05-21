#!/usr/bin/env python3
"""
HYROX Coaching Dashboard — Local Proxy Server
Port 8765.  Handles HYROX race lookups.

HYROX race data cached in memory per (season, location) to avoid repeat fetches.

Start:  python garmin_server.py
         (or double-click start_garmin_server.bat on Windows)
"""

import json
import os
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

_HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="HYROX Coaching Proxy", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── HYROX ────────────────────────────────────────────────────────────────────

_pyrox_client = None
_race_cache: dict = {}   # key: (season, location, gender, division) → DataFrame

def get_pyrox():
    global _pyrox_client
    if _pyrox_client is None:
        import pyrox
        _pyrox_client = pyrox.PyroxClient()
    return _pyrox_client


def get_race_df(season: int, location: str, gender=None, division=None):
    key = (season, location, gender or "", division or "")
    if key not in _race_cache:
        client = get_pyrox()
        _race_cache[key] = client.get_race(
            season=season, location=location, gender=gender, division=division
        )
    return _race_cache[key]


STATION_KEYWORDS = ["ski", "sled", "burpee", "row", "farmer", "sandbag", "wall", "run"]


def detect_columns(df):
    cols = {c.lower(): c for c in df.columns}
    result = {}
    for candidate in ["total_time", "total", "time", "finish_time"]:
        if candidate in cols:
            result["total_time"] = cols[candidate]
            break
    for candidate in ["rank", "place", "position", "overall_rank"]:
        if candidate in cols:
            result["rank"] = cols[candidate]
            break
    for candidate in ["athlete_name", "name", "athlete", "full_name"]:
        if candidate in cols:
            result["athlete_name"] = cols[candidate]
            break
    for candidate in ["gender", "sex"]:
        if candidate in cols:
            result["gender"] = cols[candidate]
            break
    for candidate in ["division", "category", "cat"]:
        if candidate in cols:
            result["division"] = cols[candidate]
            break
    result["splits"] = [c for c in df.columns if any(k in c.lower() for k in STATION_KEYWORDS)]
    return result


def fmt_time(minutes):
    if minutes is None:
        return "N/A"
    try:
        import math
        if isinstance(minutes, float) and math.isnan(minutes):
            return "N/A"
        total_seconds = int(round(float(minutes) * 60))
        h, rem = divmod(total_seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return "N/A"


def fmt_delta(minutes):
    if minutes is None:
        return "N/A"
    try:
        val = float(minutes)
        sign = "+" if val >= 0 else "-"
        return f"{sign}{fmt_time(abs(val))}"
    except Exception:
        return "N/A"


def build_result(athlete_row, df, cols, season, location):
    name_col     = cols.get("athlete_name", "")
    total_col    = cols.get("total_time", "")
    rank_col     = cols.get("rank", "")
    gender_col   = cols.get("gender", "")
    division_col = cols.get("division", "")
    split_cols   = cols.get("splits", [])

    name     = str(athlete_row[name_col].iloc[0]) if name_col else "Unknown"
    total    = athlete_row[total_col].iloc[0]     if total_col else None
    rank     = athlete_row[rank_col].iloc[0]      if rank_col else None
    gender   = str(athlete_row[gender_col].iloc[0])   if gender_col else ""
    division = str(athlete_row[division_col].iloc[0]) if division_col else ""
    field_size = len(df)

    splits = []
    medians = df[split_cols].median() if split_cols else {}
    top10   = df[split_cols].quantile(0.10) if split_cols else {}

    for col in split_cols:
        av  = athlete_row[col].iloc[0] if col in athlete_row.columns else None
        med = medians.get(col)
        top = top10.get(col)
        vs_med = (float(av) - float(med)) if (av is not None and med is not None) else None
        vs_top = (float(av) - float(top)) if (av is not None and top is not None) else None
        splits.append({
            "station":           col.replace("_time", "").replace("_", " ").title(),
            "time":              fmt_time(av),
            "median":            fmt_time(med),
            "vs_median":         fmt_delta(vs_med),
            "top_10_pct":        fmt_time(top),
            "vs_top_10":         fmt_delta(vs_top),
            "faster_than_median": bool(vs_med is not None and vs_med < 0),
        })

    benchmarks = {}
    if total_col and total is not None:
        faster_count    = (df[total_col] < float(total)).sum()
        percentile_rank = round((faster_count / field_size) * 100) if field_size else 0
        benchmarks = {
            "top_percent":  100 - percentile_rank,
            "median":       fmt_time(df[total_col].median()),
            "top_25_pct":   fmt_time(df[total_col].quantile(0.25)),
            "top_10_pct":   fmt_time(df[total_col].quantile(0.10)),
            "top_5_pct":    fmt_time(df[total_col].quantile(0.05)),
            "gap_to_median": fmt_delta(float(total) - float(df[total_col].median())),
            "gap_to_top_10": fmt_delta(float(total) - float(df[total_col].quantile(0.10))),
        }

    return {
        "athlete":    name,
        "race":       f"Season {season} — {location.title()}",
        "total_time": fmt_time(total),
        "rank":       int(rank) if rank is not None else None,
        "field_size": field_size,
        "gender":     gender,
        "division":   division,
        "splits":     splits,
        "benchmarks": benchmarks,
    }


class HyroxRequest(BaseModel):
    last_name:  str
    first_name: Optional[str] = None
    season:     int
    location:   str
    gender:     Optional[str] = None
    division:   Optional[str] = None


@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    path = os.path.join(_HERE, "hyrox_coaching_dashboard.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
def health():
    return {"status": "ok", "service": "hyrox-coaching-proxy", "port": 8765}


@app.post("/hyrox/lookup")
async def hyrox_lookup(req: HyroxRequest):
    try:
        df   = get_race_df(req.season, req.location, req.gender, req.division)
        cols = detect_columns(df)
        name_col = cols.get("athlete_name")

        if not name_col:
            return {"success": False, "error": "Could not detect athlete name column in dataset."}

        # Search each name part independently — handles "Last First", "First Last",
        # "LAST FIRST", doubles entries like "Williams, Kirstee Hoath", etc.
        col  = df[name_col].str.lower()
        mask = col.str.contains(req.last_name.lower(), na=False)
        if req.first_name:
            mask = mask & col.str.contains(req.first_name.lower(), na=False)

        athlete_row = df[mask]

        if athlete_row.empty:
            name_str = f"{req.first_name} {req.last_name}" if req.first_name else req.last_name
            return {"success": False, "error": f"No athlete matching '{name_str}' found at {req.location} S{req.season}."}

        results = []
        for i in range(len(athlete_row)):
            row = athlete_row.iloc[[i]]
            results.append(build_result(row, df, cols, req.season, req.location))

        return {"success": True, "results": results if len(results) > 1 else [results[0]]}

    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    print("=" * 55)
    print("  HYROX Coaching Dashboard — Local Proxy Server v3")
    print("  Dashboard: http://localhost:8765/")
    print("  Health:    GET  /health")
    print("  HYROX:     POST /hyrox/lookup")
    print("  Press Ctrl+C to stop")
    print("=" * 55)
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
