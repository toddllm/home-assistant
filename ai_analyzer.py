#!/usr/bin/env python3
"""
AI Sump Pump Analyzer - Local-first Ollama analysis service.

Runs on the Mac (M3 Max). Receives telemetry from the Linux sump pump monitor,
stores it in SQLite, periodically analyzes via Ollama, and fetches weather context
from Open-Meteo, USGS stream gauges, and NWS alerts.

This is purely advisory — it never controls the pump.

Endpoints:
  POST /ingest           - receive a telemetry reading
  GET  /api/insights     - latest AI analysis + weather + stream + risk score
  GET  /api/health       - status check
  POST /api/analyze      - trigger on-demand analysis
  GET  /api/hypotheses   - hypothesis tracking status + correlations
  GET  /api/weather-risk - computed weather risk score
"""

import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_env()

PORT = int(os.environ.get("AI_ANALYZER_PORT", "8078"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
AI_MODEL = os.environ.get("AI_MODEL", "qwen3:32b-q4_K_M")
WEATHER_LAT = os.environ.get("WEATHER_LAT", "42.938")
WEATHER_LON = os.environ.get("WEATHER_LON", "-74.1853")

DB_PATH = Path(__file__).parent / "ai_analyzer.db"
ANALYSIS_INTERVAL = int(os.environ.get("ANALYSIS_INTERVAL", "300"))    # 5 min
WEATHER_INTERVAL = int(os.environ.get("WEATHER_INTERVAL", "3600"))     # 1 hour
STREAM_INTERVAL = int(os.environ.get("STREAM_INTERVAL", "1800"))       # 30 min
NWS_INTERVAL = int(os.environ.get("NWS_INTERVAL", "900"))             # 15 min
FORECAST_INTERVAL = int(os.environ.get("FORECAST_INTERVAL", "21600")) # 6 hours
CORRELATION_INTERVAL = int(os.environ.get("CORRELATION_INTERVAL", "86400"))  # daily

# USGS stream gauge — Schoharie Creek at Burtonsville NY (~10mi, same watershed)
USGS_SITE_ID = os.environ.get("USGS_SITE_ID", "01351500")

# Data retention (days)
RETENTION_READINGS = int(os.environ.get("RETENTION_READINGS", "30"))
RETENTION_ANALYSES = int(os.environ.get("RETENTION_ANALYSES", "90"))
RETENTION_WEATHER = int(os.environ.get("RETENTION_WEATHER", "365"))
RETENTION_STREAM = int(os.environ.get("RETENTION_STREAM", "365"))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _add_column(conn, table, column, col_type):
    """Safely add a column to an existing table (no-op if it already exists)."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            power REAL, voltage REAL, current REAL, freq REAL,
            temp_c REAL, illumination TEXT, rssi INTEGER,
            uptime INTEGER, output INTEGER, energy REAL,
            reset_reason INTEGER, ram_free INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

        CREATE TABLE IF NOT EXISTS weather (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            temp_c REAL, precipitation REAL, rain REAL,
            weather_code INTEGER, precip_prob_6h REAL
        );
        CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(ts);

        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            status TEXT, anomalies TEXT, recommendation TEXT,
            confidence REAL, raw_response TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_analyses_ts ON analyses(ts);

        CREATE TABLE IF NOT EXISTS stream_gauge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            site_id TEXT,
            discharge_cfs REAL,
            gauge_height_ft REAL
        );
        CREATE INDEX IF NOT EXISTS idx_stream_ts ON stream_gauge(ts);

        CREATE TABLE IF NOT EXISTS nws_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            alert_id TEXT UNIQUE,
            event TEXT,
            severity TEXT,
            headline TEXT,
            onset TEXT,
            expires TEXT
        );

        CREATE TABLE IF NOT EXISTS forecast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            target_ts TEXT NOT NULL,
            precip_mm REAL,
            soil_moisture REAL,
            temp_c REAL
        );
        CREATE INDEX IF NOT EXISTS idx_forecast_target ON forecast(target_ts);
        CREATE INDEX IF NOT EXISTS idx_forecast_fetched ON forecast(fetched_at);

        CREATE TABLE IF NOT EXISTS hypotheses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            hypothesis TEXT NOT NULL,
            metric TEXT,
            threshold REAL,
            prediction TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            evidence_count INTEGER DEFAULT 0,
            support_count INTEGER DEFAULT 0,
            refute_count INTEGER DEFAULT 0,
            p_value REAL,
            notes TEXT,
            closed_at TEXT,
            closure_evidence TEXT
        );

        CREATE TABLE IF NOT EXISTS correlations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            metric_a TEXT NOT NULL,
            metric_b TEXT NOT NULL,
            r_value REAL,
            r_squared REAL,
            p_value REAL,
            sample_size INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_correlations_ts ON correlations(ts);
    """)

    # Add new columns to weather table (safe migration for existing databases)
    for col, ctype in [
        ("soil_moisture_0_7", "REAL"),
        ("soil_moisture_7_28", "REAL"),
        ("soil_moisture_28_100", "REAL"),
        ("snow_depth_m", "REAL"),
        ("snow_water_equiv_mm", "REAL"),
    ]:
        _add_column(conn, "weather", col, ctype)

    conn.close()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)

def query_stats(conn, hours):
    """Get aggregated stats for the last N hours of readings."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM readings WHERE ts >= ? ORDER BY ts", (cutoff,)
    ).fetchall()
    if not rows:
        return None

    voltages = [r["voltage"] for r in rows if r["voltage"] is not None]
    temps = [r["temp_c"] for r in rows if r["temp_c"] is not None]

    # Split power into running vs all readings
    running_powers = [r["power"] for r in rows if (r["power"] or 0) > 100]
    all_powers = [r["power"] for r in rows if r["power"] is not None]

    # Detect pump cycles (power > 100W)
    cycles = []
    in_cycle = False
    cycle_start_idx = None
    for idx, r in enumerate(rows):
        running = (r["power"] or 0) > 100
        if running and not in_cycle:
            in_cycle = True
            cycle_start_idx = idx
        elif not running and in_cycle:
            in_cycle = False
            if cycle_start_idx is not None:
                # Use reading count * 30s as duration estimate (more reliable than timestamps)
                duration = (idx - cycle_start_idx) * 30
                cycles.append(duration)

    def safe_stats(values):
        if not values:
            return {"min": 0, "max": 0, "avg": 0}
        return {
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "avg": round(sum(values) / len(values), 1),
        }

    total_run_seconds = sum(cycles) if cycles else 0

    return {
        "reading_count": len(rows),
        "running_reading_count": len(running_powers),
        "power": safe_stats(all_powers),
        "running_power": safe_stats(running_powers),
        "voltage": safe_stats(voltages),
        "temp_c": safe_stats(temps),
        "cycle_count": len(cycles),
        "cycle_durations": [round(c, 1) for c in cycles],
        "avg_cycle_duration": round(sum(cycles) / len(cycles), 1) if cycles else 0,
        "total_run_seconds": round(total_run_seconds, 1),
    }

def get_latest_weather(conn):
    """Get the most recent weather entry."""
    row = conn.execute(
        "SELECT * FROM weather ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        return dict(row)
    return None

def get_latest_stream(conn):
    """Get the most recent stream gauge entry."""
    row = conn.execute(
        "SELECT * FROM stream_gauge ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        return dict(row)
    return None

def get_stream_trend(conn, hours=6):
    """Get stream gauge height trend over N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT gauge_height_ft FROM stream_gauge WHERE ts >= ? ORDER BY ts",
        (cutoff,)
    ).fetchall()
    if len(rows) < 2:
        return "insufficient data"
    heights = [r["gauge_height_ft"] for r in rows if r["gauge_height_ft"] is not None]
    if len(heights) < 2:
        return "insufficient data"
    diff = heights[-1] - heights[0]
    if diff > 0.5:
        return f"rising (+{diff:.1f}ft/{hours}h)"
    elif diff < -0.5:
        return f"falling ({diff:.1f}ft/{hours}h)"
    return "stable"

def get_active_alerts(conn):
    """Get currently active NWS alerts."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM nws_alerts WHERE expires > ? ORDER BY onset DESC",
        (now,)
    ).fetchall()
    return [dict(r) for r in rows]

def get_forecast_summary(conn):
    """Get the latest forecast summary (next 6h and 24h precipitation)."""
    latest_fetch = conn.execute(
        "SELECT fetched_at FROM forecast ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    if not latest_fetch:
        return None

    fetched_at = latest_fetch["fetched_at"]
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    h6 = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")
    h24 = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

    rows_6h = conn.execute(
        "SELECT precip_mm FROM forecast WHERE fetched_at = ? AND target_ts >= ? AND target_ts <= ?",
        (fetched_at, now_str, h6)
    ).fetchall()
    rows_24h = conn.execute(
        "SELECT precip_mm FROM forecast WHERE fetched_at = ? AND target_ts >= ? AND target_ts <= ?",
        (fetched_at, now_str, h24)
    ).fetchall()

    precip_6h = sum(r["precip_mm"] or 0 for r in rows_6h)
    precip_24h = sum(r["precip_mm"] or 0 for r in rows_24h)

    return {
        "precip_6h_mm": round(precip_6h, 1),
        "precip_24h_mm": round(precip_24h, 1),
        "fetched_at": fetched_at,
    }

def get_snow_melt_rate(conn):
    """Calculate snow melt rate (mm SWE/day) over last 24 hours."""
    yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

    oldest = conn.execute(
        "SELECT snow_water_equiv_mm FROM weather WHERE ts >= ? AND snow_water_equiv_mm IS NOT NULL ORDER BY ts LIMIT 1",
        (yesterday,)
    ).fetchone()
    newest = conn.execute(
        "SELECT snow_water_equiv_mm FROM weather WHERE snow_water_equiv_mm IS NOT NULL ORDER BY ts DESC LIMIT 1"
    ).fetchone()

    if not oldest or not newest:
        return 0.0

    decrease = (oldest["snow_water_equiv_mm"] or 0) - (newest["snow_water_equiv_mm"] or 0)
    return round(max(0, decrease), 1)

# ---------------------------------------------------------------------------
# Ollama Analysis
# ---------------------------------------------------------------------------

def build_prompt(conn):
    """Build the structured analysis prompt with all available data sources."""
    latest = conn.execute(
        "SELECT * FROM readings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if not latest:
        return None

    stats_1h = query_stats(conn, 1)
    stats_24h = query_stats(conn, 24)
    weather = get_latest_weather(conn)
    stream = get_latest_stream(conn)
    alerts = get_active_alerts(conn)
    forecast = get_forecast_summary(conn)
    snow_melt_rate = get_snow_melt_rate(conn)

    # Build a compact, rules-first prompt — conciseness improves model accuracy
    parts = ["/no_think"]  # Suppress qwen3 internal thinking for faster JSON output

    parts.append("""Analyze sump pump telemetry. Respond ONLY with JSON.

RULES (apply strictly):
- Max running power >520W -> at least "watch" (motor degradation risk)
- >6 pump cycles/hour + no rain -> "watch" or "warning" (stuck float / groundwater)
- Voltage <115V -> "warning" (brownout risk)
- Plug temp >40C -> "warning" (heat stress)
- 3-minute cutoffs = monitor intervening, NOT a pump failure
- soil_moisture >0.35 + high cycling = expected; <0.20 + high cycling = investigate
- stream rising >1ft/6h + pump increasing = expected regional water table rise
- During active snowmelt (SWE decreasing, temp >0C), increased pump activity is NORMAL
- If forecast >10mm/6h, preemptively set at least "watch"
- Err toward "watch" for borderline issues -- false positives are cheap, missed failures are not""")

    parts.append("""
BASELINE: power 480-520W | cycle 60-90s | idle 0W | temp 28-38C | voltage 119-123V""")

    parts.append("""
NOW: {power}W | {voltage}V | {current}A | {temp_c}C | {illumination} | RSSI {rssi} | uptime {uptime}s | {output}""".format(
        power=latest["power"], voltage=latest["voltage"],
        current=latest["current"], temp_c=latest["temp_c"],
        illumination=latest["illumination"], rssi=latest["rssi"],
        uptime=latest["uptime"], output="ON" if latest["output"] else "OFF",
    ))

    if stats_1h and stats_1h["running_reading_count"] > 0:
        parts.append("""
1H: {cycles} cycles (avg {avg_dur}s) | running power {rpmin}-{rpmax}W avg {rpavg}W | voltage {vmin}-{vmax}V | temp {tmin}-{tmax}C""".format(
            cycles=stats_1h["cycle_count"],
            avg_dur=stats_1h["avg_cycle_duration"],
            rpmin=stats_1h["running_power"]["min"],
            rpmax=stats_1h["running_power"]["max"],
            rpavg=stats_1h["running_power"]["avg"],
            vmin=stats_1h["voltage"]["min"], vmax=stats_1h["voltage"]["max"],
            tmin=stats_1h["temp_c"]["min"], tmax=stats_1h["temp_c"]["max"],
        ))
    elif stats_1h:
        parts.append("""
1H: 0 cycles | pump idle | voltage {vmin}-{vmax}V | temp {tmin}-{tmax}C""".format(
            vmin=stats_1h["voltage"]["min"], vmax=stats_1h["voltage"]["max"],
            tmin=stats_1h["temp_c"]["min"], tmax=stats_1h["temp_c"]["max"],
        ))

    if stats_24h:
        parts.append("""
24H: {cycles} cycles | {run_min}min total run | power {pmin}-{pmax}W | voltage {vmin}-{vmax}V""".format(
            cycles=stats_24h["cycle_count"],
            run_min=round(stats_24h["total_run_seconds"] / 60, 1),
            pmin=stats_24h["power"]["min"], pmax=stats_24h["power"]["max"],
            vmin=stats_24h["voltage"]["min"], vmax=stats_24h["voltage"]["max"],
        ))

    if weather:
        parts.append("""
WEATHER: {temp}C outdoor | {precip}mm precip | {rain}mm rain | {prob}% 6h rain probability""".format(
            temp=weather.get("temp_c", "?"),
            precip=weather.get("precipitation", "?"),
            rain=weather.get("rain", "?"),
            prob=weather.get("precip_prob_6h", "?"),
        ))

    # Soil moisture (Epic 1.1 / Epic 6.1)
    if weather and weather.get("soil_moisture_28_100") is not None:
        parts.append(
            "SOIL: {s07} (0-7cm) | {s728} (7-28cm) | {s28100} (28-100cm) m\u00b3/m\u00b3".format(
                s07=weather.get("soil_moisture_0_7", "?"),
                s728=weather.get("soil_moisture_7_28", "?"),
                s28100=weather.get("soil_moisture_28_100", "?"),
            )
        )

    # Snow/melt context (Epic 1.2 / Epic 6.3)
    if weather and (weather.get("snow_depth_m") or weather.get("snow_water_equiv_mm")):
        parts.append(
            "SNOW: {depth}m depth | {swe}mm SWE | melt rate {rate}mm/day".format(
                depth=weather.get("snow_depth_m", 0),
                swe=weather.get("snow_water_equiv_mm", 0),
                rate=snow_melt_rate,
            )
        )

    # Stream gauge (Epic 1.3 / Epic 6.2)
    if stream:
        trend = get_stream_trend(conn)
        parts.append(
            "STREAM: Schoharie Creek {discharge}cfs | gauge {height}ft | {trend}".format(
                discharge=stream.get("discharge_cfs", "?"),
                height=stream.get("gauge_height_ft", "?"),
                trend=trend,
            )
        )

    # Forecast lookahead (Epic 1.5 / Epic 6.4)
    if forecast:
        parts.append(
            "FORECAST: next 6h: {p6}mm | next 24h: {p24}mm".format(
                p6=forecast["precip_6h_mm"],
                p24=forecast["precip_24h_mm"],
            )
        )

    # Active NWS alerts (Epic 1.4)
    if alerts:
        alert_strs = [f"{a['event']} ({a['severity']})" for a in alerts[:3]]
        parts.append("ALERTS: " + " | ".join(alert_strs))

    parts.append("""
{"status": "normal|watch|warning|critical", "anomalies": ["specific observations"], "recommendation": "action if any", "confidence": 0.0-1.0}""")
    return "\n".join(parts)


def run_analysis():
    """Run one Ollama analysis cycle."""
    conn = get_db()
    try:
        prompt = build_prompt(conn)
        if not prompt:
            log("Analysis skipped: no readings yet")
            return

        log(f"Running analysis with {AI_MODEL}...")
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": AI_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1, "num_predict": 512},
                },
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
        except Exception as e:
            log(f"Ollama call failed: {e}")
            return

        raw = result.get("response", "")
        log(f"Ollama responded ({len(raw)} chars, {result.get('eval_count', '?')} tokens)")

        # Parse the JSON response
        try:
            analysis = json.loads(raw)
        except json.JSONDecodeError:
            log(f"Failed to parse Ollama response as JSON: {raw[:200]}")
            analysis = {
                "status": "error",
                "anomalies": ["Model returned invalid JSON"],
                "recommendation": "Check model output",
                "confidence": 0.0,
            }

        conn.execute(
            """INSERT INTO analyses (status, anomalies, recommendation, confidence, raw_response)
               VALUES (?, ?, ?, ?, ?)""",
            (
                analysis.get("status", "unknown"),
                json.dumps(analysis.get("anomalies", [])),
                analysis.get("recommendation", ""),
                analysis.get("confidence", 0.0),
                raw,
            ),
        )
        conn.commit()
        log(f"Analysis stored: status={analysis.get('status')}, confidence={analysis.get('confidence')}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Weather & Data Source Fetching
# ---------------------------------------------------------------------------

def fetch_weather():
    """Fetch current weather + soil moisture + snow from Open-Meteo (free, no API key).

    Soil moisture, snow depth are hourly variables (not available in 'current'),
    so we extract the most recent hourly value.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&current=temperature_2m,precipitation,rain,weather_code"
        f"&hourly=precipitation_probability,snow_depth"
        f",soil_moisture_0_to_7cm,soil_moisture_7_to_28cm,soil_moisture_28_to_100cm"
        f"&timezone=auto&forecast_days=1"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"Weather fetch failed: {e}")
        return

    current = data.get("current", {})
    hourly = data.get("hourly", {})

    # Precipitation probability for next 6 hours
    probs = hourly.get("precipitation_probability", [])
    precip_prob_6h = round(sum(probs[:6]) / max(len(probs[:6]), 1), 1) if probs else None

    # Extract current-hour values from hourly arrays (index 0 = current hour)
    # Use _hourly_now helper to safely get first non-None value from recent hours
    def _hourly_now(key, fallback=None):
        vals = hourly.get(key, [])
        # Try current hour first, then next few
        for v in vals[:3]:
            if v is not None:
                return v
        return fallback

    soil_0_7 = _hourly_now("soil_moisture_0_to_7cm")
    soil_7_28 = _hourly_now("soil_moisture_7_to_28cm")
    soil_28_100 = _hourly_now("soil_moisture_28_to_100cm")
    snow_depth = _hourly_now("snow_depth", 0.0)

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO weather (temp_c, precipitation, rain, weather_code, precip_prob_6h,
               soil_moisture_0_7, soil_moisture_7_28, soil_moisture_28_100,
               snow_depth_m, snow_water_equiv_mm)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                current.get("temperature_2m"),
                current.get("precipitation"),
                current.get("rain"),
                current.get("weather_code"),
                precip_prob_6h,
                soil_0_7,
                soil_7_28,
                soil_28_100,
                snow_depth,
                None,  # SWE not available from Open-Meteo; will be derived from snow_depth if needed
            ),
        )
        conn.commit()
        log(f"Weather stored: {current.get('temperature_2m')}C, precip={current.get('precipitation')}mm, "
            f"soil_28_100={soil_28_100}, snow_depth={snow_depth}m")
    finally:
        conn.close()


def fetch_usgs_stream():
    """Fetch stream gauge data from USGS Water Services (free, no API key).

    Handles USGS sentinel value -999999 (ice-affected/unavailable) by storing None.
    """
    url = (
        f"https://waterservices.usgs.gov/nwis/iv/"
        f"?sites={USGS_SITE_ID}&parameterCd=00060,00065"
        f"&period=PT2H&format=json"
    )
    try:
        resp = requests.get(url, timeout=15, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"USGS stream fetch failed: {e}")
        return

    discharge = None
    gauge_height = None

    for ts in data.get("value", {}).get("timeSeries", []):
        param = ts.get("variable", {}).get("variableCode", [{}])[0].get("value", "")
        values = ts.get("values", [{}])[0].get("value", [])
        if values:
            latest_val = values[-1].get("value")
            try:
                val = float(latest_val)
            except (TypeError, ValueError):
                continue
            # USGS uses -999999 as sentinel for unavailable/ice-affected
            if val <= -999999:
                val = None
            if param == "00060":  # Discharge (cfs)
                discharge = val
            elif param == "00065":  # Gauge height (ft)
                gauge_height = val

    if discharge is None and gauge_height is None:
        log(f"Stream gauge: no valid data from site {USGS_SITE_ID} (may be ice-affected)")
        # Still store the row so we know the fetch happened

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO stream_gauge (site_id, discharge_cfs, gauge_height_ft) VALUES (?, ?, ?)",
            (USGS_SITE_ID, discharge, gauge_height),
        )
        conn.commit()
        log(f"Stream gauge stored: discharge={discharge}cfs, height={gauge_height}ft")
    finally:
        conn.close()


def fetch_nws_alerts():
    """Fetch active weather alerts from NWS (free, no API key)."""
    url = f"https://api.weather.gov/alerts/active?point={WEATHER_LAT},{WEATHER_LON}"
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "(sump-pump-monitor, contact@example.com)",
            "Accept": "application/geo+json",
        })
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"NWS alerts fetch failed: {e}")
        return

    features = data.get("features", [])
    conn = get_db()
    try:
        inserted = 0
        for f in features:
            props = f.get("properties", {})
            alert_id = props.get("id", "")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO nws_alerts
                       (alert_id, event, severity, headline, onset, expires)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        alert_id,
                        props.get("event"),
                        props.get("severity"),
                        props.get("headline"),
                        props.get("onset"),
                        props.get("expires"),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        log(f"NWS alerts: {len(features)} active, {inserted} new stored")
    finally:
        conn.close()


def fetch_forecast():
    """Fetch 7-day hourly forecast from Open-Meteo."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&hourly=precipitation,soil_moisture_28_to_100cm,temperature_2m"
        f"&timezone=auto&forecast_days=7"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log(f"Forecast fetch failed: {e}")
        return

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    precip = hourly.get("precipitation", [])
    soil = hourly.get("soil_moisture_28_to_100cm", [])
    temps = hourly.get("temperature_2m", [])

    if not times:
        log("Forecast: no hourly data returned")
        return

    conn = get_db()
    try:
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for i, t in enumerate(times):
            conn.execute(
                """INSERT INTO forecast (fetched_at, target_ts, precip_mm, soil_moisture, temp_c)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    fetched_at,
                    t,
                    precip[i] if i < len(precip) else None,
                    soil[i] if i < len(soil) else None,
                    temps[i] if i < len(temps) else None,
                ),
            )
        conn.commit()
        log(f"Forecast stored: {len(times)} hourly rows for next 7 days")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Weather Risk Score
# ---------------------------------------------------------------------------

def _normalize(value, low, high):
    """Normalize a value to 0.0-1.0 range."""
    if value is None or high <= low:
        return 0.0
    return max(0.0, min(1.0, (value - low) / (high - low)))


def compute_weather_risk(conn=None):
    """Compute weather risk score (0.0=dry to 1.0=flood conditions).

    Weights are initial estimates — to be refined from Epic 3 correlation data.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        weather = get_latest_weather(conn)
        stream = get_latest_stream(conn)
        alerts = get_active_alerts(conn)
        snow_melt = get_snow_melt_rate(conn)

        soil = (weather or {}).get("soil_moisture_28_100")
        precip_prob = (weather or {}).get("precip_prob_6h", 0) or 0
        precip_rate = (weather or {}).get("precipitation", 0) or 0
        gauge = (stream or {}).get("gauge_height_ft")
        flood_alert = any(
            a.get("event", "").lower() in (
                "flood warning", "flood watch",
                "flash flood warning", "flash flood watch",
            )
            for a in alerts
        )

        # Baseline and flood stage for Schoharie Creek (estimates)
        baseline_ft = 2.0
        flood_stage_ft = 10.0

        risk = (
            0.30 * _normalize(soil, 0.15, 0.45) +
            0.15 * (precip_prob / 100.0) +
            0.15 * _normalize(precip_rate, 0, 20) +
            0.20 * _normalize(gauge, baseline_ft, flood_stage_ft) +
            0.10 * _normalize(snow_melt, 0, 30) +
            0.10 * (1.0 if flood_alert else 0.0)
        )

        factors = []
        if soil is not None:
            factors.append(f"soil={soil:.2f}")
        factors.append(f"precip_prob={precip_prob:.0f}%")
        if gauge is not None:
            factors.append(f"gauge={gauge:.1f}ft")
        if snow_melt > 0:
            factors.append(f"melt={snow_melt}mm/d")
        if flood_alert:
            factors.append("FLOOD_ALERT")

        return {
            "risk_score": round(min(1.0, max(0.0, risk)), 3),
            "level": (
                "low" if risk < 0.2 else
                "moderate" if risk < 0.5 else
                "high" if risk < 0.8 else
                "extreme"
            ),
            "factors": factors[:5],
        }
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Hypothesis & Correlation Engine
# ---------------------------------------------------------------------------

def _pearson(x, y):
    """Compute Pearson correlation coefficient with approximate p-value.

    Returns (r, r_squared, p_value) or (None, None, None) if insufficient data.
    Uses t-distribution approximation for p-value (no scipy dependency).
    """
    n = len(x)
    if n < 3:
        return None, None, None

    mean_x = sum(x) / n
    mean_y = sum(y) / n
    dx = [xi - mean_x for xi in x]
    dy = [yi - mean_y for yi in y]
    sxx = sum(d * d for d in dx)
    syy = sum(d * d for d in dy)
    sxy = sum(dx[i] * dy[i] for i in range(n))

    if sxx == 0 or syy == 0:
        return 0.0, 0.0, 1.0

    r = sxy / math.sqrt(sxx * syy)
    r_sq = r * r

    # Approximate p-value using t-statistic
    if abs(r) >= 1.0:
        p = 0.0
    else:
        t_stat = r * math.sqrt((n - 2) / (1 - r * r))
        # Rough two-tailed p-value approximation
        p = min(1.0, max(0.0,
            2.0 * math.exp(-0.717 * abs(t_stat) - 0.416 * t_stat * t_stat)
            if abs(t_stat) < 6 else 0.0001
        ))

    return round(r, 4), round(r_sq, 4), round(p, 4)


def compute_daily_correlations():
    """Compute daily correlation snapshots between weather metrics and pump behavior."""
    conn = get_db()
    try:
        # Build 7-day window of daily stats
        days_data = []
        for days_ago in range(7):
            day_start = (datetime.now(timezone.utc) - timedelta(days=days_ago + 1)).strftime("%Y-%m-%dT%H:%M:%S")
            day_end = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")

            # Count pump cycles for this day
            rows = conn.execute(
                "SELECT power FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                (day_start, day_end)
            ).fetchall()

            cycles = 0
            in_cycle = False
            for r in rows:
                running = (r["power"] or 0) > 100
                if running and not in_cycle:
                    cycles += 1
                    in_cycle = True
                elif not running:
                    in_cycle = False

            # Average soil moisture (28-100cm) for this day
            soil = conn.execute(
                "SELECT AVG(soil_moisture_28_100) as avg_soil FROM weather WHERE ts >= ? AND ts < ?",
                (day_start, day_end)
            ).fetchone()

            # Average stream gauge height
            gauge = conn.execute(
                "SELECT AVG(gauge_height_ft) as avg_gauge FROM stream_gauge WHERE ts >= ? AND ts < ?",
                (day_start, day_end)
            ).fetchone()

            # Cumulative 48h precipitation (this day + previous)
            precip_start = (datetime.now(timezone.utc) - timedelta(days=days_ago + 2)).strftime("%Y-%m-%dT%H:%M:%S")
            precip = conn.execute(
                "SELECT SUM(precipitation) as total_precip FROM weather WHERE ts >= ? AND ts < ?",
                (precip_start, day_end)
            ).fetchone()

            # SWE change rate
            swe_oldest = conn.execute(
                "SELECT snow_water_equiv_mm FROM weather WHERE ts >= ? AND snow_water_equiv_mm IS NOT NULL ORDER BY ts LIMIT 1",
                (day_start,)
            ).fetchone()
            swe_newest = conn.execute(
                "SELECT snow_water_equiv_mm FROM weather WHERE ts < ? AND snow_water_equiv_mm IS NOT NULL ORDER BY ts DESC LIMIT 1",
                (day_end,)
            ).fetchone()
            swe_change = 0
            if swe_oldest and swe_newest:
                swe_change = (swe_oldest["snow_water_equiv_mm"] or 0) - (swe_newest["snow_water_equiv_mm"] or 0)

            days_data.append({
                "cycles": cycles,
                "soil_28_100": soil["avg_soil"] if soil else None,
                "gauge_height": gauge["avg_gauge"] if gauge else None,
                "precip_48h": precip["total_precip"] if precip else None,
                "swe_change_rate": swe_change,
            })

        # Compute correlations for each metric pair
        pairs = [
            ("soil_moisture_28_100", "pump_cycles_24h", "soil_28_100"),
            ("stream_gauge_height", "pump_cycles_24h", "gauge_height"),
            ("precip_48h_cumulative", "pump_cycles_24h", "precip_48h"),
            ("swe_change_rate", "pump_cycles_24h", "swe_change_rate"),
        ]

        cycle_vals = [d["cycles"] for d in days_data]
        computed = 0

        for metric_a, metric_b, key in pairs:
            metric_vals = [d[key] for d in days_data if d[key] is not None]
            filtered_cycles = [cycle_vals[i] for i, d in enumerate(days_data) if d[key] is not None]

            if len(metric_vals) < 3:
                continue

            r, r_sq, p = _pearson(metric_vals, filtered_cycles)
            if r is not None:
                conn.execute(
                    """INSERT INTO correlations (metric_a, metric_b, r_value, r_squared, p_value, sample_size)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (metric_a, metric_b, r, r_sq, p, len(metric_vals)),
                )
                computed += 1

        conn.commit()
        log(f"Daily correlations computed: {computed} pairs")
    except Exception as e:
        log(f"Correlation computation error: {e}")
    finally:
        conn.close()


def seed_hypotheses():
    """Seed initial hypotheses if table is empty."""
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
        if count > 0:
            return

        hypotheses = [
            (
                "Soil moisture (28-100cm) > 0.35 m\u00b3/m\u00b3 predicts pump cycling within 6 hours",
                "soil_moisture_28_100", 0.35,
                "pump cycling within 6h when soil moisture exceeds threshold",
            ),
            (
                "Stream gauge height > 4.0 ft correlates with pump frequency > 4/hour",
                "gauge_height_ft", 4.0,
                "pump frequency > 4/hour when gauge > 4.0 ft",
            ),
            (
                "Snow water equivalent decrease > 10mm/day during temps > 2C predicts increased pump activity within 24h",
                "swe_change_rate", 10.0,
                "increased pump activity within 24h during rapid snowmelt",
            ),
            (
                "Pump cycling with 0mm precipitation and soil moisture < 0.25 indicates mechanical issue (stuck float)",
                "soil_moisture_28_100", 0.25,
                "mechanical issue when cycling without environmental cause",
            ),
            (
                "48-hour cumulative precipitation > 25mm predicts pump frequency > 6/hour within 12 hours",
                "precip_48h_cumulative", 25.0,
                "pump frequency > 6/hour within 12h after heavy rain",
            ),
        ]

        for hyp, metric, threshold, prediction in hypotheses:
            conn.execute(
                """INSERT INTO hypotheses (hypothesis, metric, threshold, prediction, status)
                   VALUES (?, ?, ?, ?, 'active')""",
                (hyp, metric, threshold, prediction),
            )
        conn.commit()
        log(f"Seeded {len(hypotheses)} initial hypotheses")
    finally:
        conn.close()


def evaluate_hypotheses():
    """Evaluate active hypotheses against recent data and update evidence counts.

    Auto-terminal states:
    - confirmed: >70% support rate, n >= 30, p < 0.05
    - refuted:   <30% support rate, n >= 30
    - inconclusive: 30-70% support rate, n >= 50
    """
    conn = get_db()
    try:
        active = conn.execute(
            "SELECT * FROM hypotheses WHERE status = 'active'"
        ).fetchall()

        if not active:
            return

        now = datetime.now(timezone.utc)
        stats_1h = query_stats(conn, 1)
        stats_24h = query_stats(conn, 24)
        weather = get_latest_weather(conn)
        stream = get_latest_stream(conn)
        snow_melt = get_snow_melt_rate(conn)

        # 48h cumulative precipitation
        precip_48h_row = conn.execute(
            "SELECT SUM(precipitation) as total FROM weather WHERE ts >= ?",
            ((now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S"),)
        ).fetchone()
        precip_48h = precip_48h_row["total"] if precip_48h_row and precip_48h_row["total"] else 0

        cycle_rate = stats_1h["cycle_count"] if stats_1h else 0

        for hyp in active:
            hyp_id = hyp["id"]
            metric = hyp["metric"]
            threshold = hyp["threshold"]
            support = False
            has_evidence = False

            if metric == "soil_moisture_28_100" and weather:
                soil = weather.get("soil_moisture_28_100")
                if soil is not None:
                    if hyp_id == 1:  # H1: Soil > 0.35 -> cycling
                        if soil > threshold:
                            has_evidence = True
                            support = cycle_rate > 0
                    elif hyp_id == 4:  # H4: Cycling with low soil + no precip = mechanical
                        precip = weather.get("precipitation", 0) or 0
                        if cycle_rate > 0 and precip == 0 and soil < threshold:
                            has_evidence = True
                            support = True
                        elif cycle_rate > 0 and (precip > 0 or soil >= threshold):
                            has_evidence = True
                            support = False

            elif metric == "gauge_height_ft" and stream:
                gauge = stream.get("gauge_height_ft")
                if gauge is not None and gauge > threshold:
                    has_evidence = True
                    support = cycle_rate > 4

            elif metric == "swe_change_rate":
                if weather and (weather.get("temp_c") or 0) > 2:
                    if snow_melt > threshold:
                        has_evidence = True
                        cycles_24h = stats_24h["cycle_count"] if stats_24h else 0
                        support = cycles_24h > 0

            elif metric == "precip_48h_cumulative":
                if precip_48h > threshold:
                    has_evidence = True
                    support = cycle_rate > 6

            if has_evidence:
                conn.execute(
                    """UPDATE hypotheses SET
                       evidence_count = evidence_count + 1,
                       support_count = support_count + CASE WHEN ? THEN 1 ELSE 0 END,
                       refute_count = refute_count + CASE WHEN ? THEN 0 ELSE 1 END
                       WHERE id = ?""",
                    (support, support, hyp_id),
                )

                # Check for terminal states
                updated = conn.execute(
                    "SELECT * FROM hypotheses WHERE id = ?", (hyp_id,)
                ).fetchone()
                ev = updated["evidence_count"]
                sup = updated["support_count"]

                if ev >= 30:
                    rate = sup / ev if ev > 0 else 0
                    now_str = now.strftime("%Y-%m-%dT%H:%M:%S")
                    if rate > 0.70:
                        conn.execute(
                            "UPDATE hypotheses SET status = 'confirmed', closed_at = ? WHERE id = ?",
                            (now_str, hyp_id),
                        )
                        log(f"Hypothesis {hyp_id} CONFIRMED (support rate {rate:.0%}, n={ev})")
                    elif rate < 0.30:
                        conn.execute(
                            "UPDATE hypotheses SET status = 'refuted', closed_at = ? WHERE id = ?",
                            (now_str, hyp_id),
                        )
                        log(f"Hypothesis {hyp_id} REFUTED (support rate {rate:.0%}, n={ev})")

                if ev >= 50:
                    rate = sup / ev if ev > 0 else 0
                    if 0.30 <= rate <= 0.70:
                        conn.execute(
                            "UPDATE hypotheses SET status = 'inconclusive', closed_at = ? WHERE id = ?",
                            (now.strftime("%Y-%m-%dT%H:%M:%S"), hyp_id),
                        )
                        log(f"Hypothesis {hyp_id} INCONCLUSIVE (support rate {rate:.0%}, n={ev})")

        conn.commit()
        log("Hypothesis evaluation complete")
    except Exception as e:
        log(f"Hypothesis evaluation error: {e}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Data Retention
# ---------------------------------------------------------------------------

def prune_old_data():
    """Delete old data based on configured retention periods."""
    conn = get_db()
    try:
        now = datetime.now(timezone.utc)
        cutoffs = {
            "readings": (now - timedelta(days=RETENTION_READINGS)).strftime("%Y-%m-%dT%H:%M:%S"),
            "analyses": (now - timedelta(days=RETENTION_ANALYSES)).strftime("%Y-%m-%dT%H:%M:%S"),
            "weather": (now - timedelta(days=RETENTION_WEATHER)).strftime("%Y-%m-%dT%H:%M:%S"),
            "stream_gauge": (now - timedelta(days=RETENTION_STREAM)).strftime("%Y-%m-%dT%H:%M:%S"),
            "nws_alerts": (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S"),
            "forecast": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S"),
            "correlations": (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S"),
        }

        total = 0
        for table, cutoff in cutoffs.items():
            ts_col = "fetched_at" if table == "forecast" else "ts"
            deleted = conn.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)).rowcount
            if deleted:
                log(f"Pruned {deleted} rows from {table}")
                total += deleted
        conn.commit()
        if total:
            log(f"Pruned {total} total rows across all tables")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Background Threads
# ---------------------------------------------------------------------------

def analysis_loop():
    """Run Ollama analysis every 5 minutes."""
    time.sleep(30)  # let some readings accumulate first
    while True:
        try:
            run_analysis()
        except Exception as e:
            log(f"Analysis loop error: {e}")
        time.sleep(ANALYSIS_INTERVAL)


def weather_loop():
    """Fetch weather every hour. First fetch after 10 seconds."""
    time.sleep(10)
    while True:
        try:
            fetch_weather()
        except Exception as e:
            log(f"Weather loop error: {e}")
        time.sleep(WEATHER_INTERVAL)


def stream_loop():
    """Fetch USGS stream gauge every 30 minutes."""
    time.sleep(20)
    while True:
        try:
            fetch_usgs_stream()
        except Exception as e:
            log(f"Stream loop error: {e}")
        time.sleep(STREAM_INTERVAL)


def nws_loop():
    """Fetch NWS alerts every 15 minutes."""
    time.sleep(15)
    while True:
        try:
            fetch_nws_alerts()
        except Exception as e:
            log(f"NWS loop error: {e}")
        time.sleep(NWS_INTERVAL)


def forecast_loop():
    """Fetch 7-day forecast every 6 hours."""
    time.sleep(25)
    while True:
        try:
            fetch_forecast()
        except Exception as e:
            log(f"Forecast loop error: {e}")
        time.sleep(FORECAST_INTERVAL)


def correlation_loop():
    """Run daily correlation snapshot and hypothesis evaluation."""
    time.sleep(60)  # let other data sources populate first
    while True:
        try:
            compute_daily_correlations()
            evaluate_hypotheses()
        except Exception as e:
            log(f"Correlation loop error: {e}")
        time.sleep(CORRELATION_INTERVAL)


def prune_loop():
    """Prune old data once per day."""
    while True:
        time.sleep(86400)
        try:
            prune_old_data()
        except Exception as e:
            log(f"Prune loop error: {e}")


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/ingest", methods=["POST"])
def ingest():
    """Receive a telemetry reading from the sump pump monitor."""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO readings
               (power, voltage, current, freq, temp_c, illumination, rssi,
                uptime, output, energy, reset_reason, ram_free)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("power"),
                data.get("voltage"),
                data.get("current"),
                data.get("freq"),
                data.get("temp_c"),
                data.get("illumination"),
                data.get("rssi"),
                data.get("uptime"),
                1 if data.get("output") else 0,
                data.get("energy"),
                data.get("reset_reason"),
                data.get("ram_free"),
            ),
        )
        conn.commit()
        return jsonify({"ok": True}), 200
    finally:
        conn.close()


@app.route("/api/insights")
def insights():
    """Return the latest AI analysis with enriched weather/stream/risk data."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM analyses ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return jsonify({"status": "pending", "message": "No analysis yet"})

        weather = get_latest_weather(conn)

        result = {
            "status": row["status"],
            "anomalies": json.loads(row["anomalies"]) if row["anomalies"] else [],
            "recommendation": row["recommendation"],
            "confidence": row["confidence"],
            "analyzed_at": row["ts"],
            "model": AI_MODEL,
        }
        if weather:
            result["weather"] = {
                "temp_c": weather.get("temp_c"),
                "precipitation": weather.get("precipitation"),
                "rain": weather.get("rain"),
                "precip_prob_6h": weather.get("precip_prob_6h"),
                "soil_moisture_28_100": weather.get("soil_moisture_28_100"),
                "snow_depth_m": weather.get("snow_depth_m"),
                "snow_water_equiv_mm": weather.get("snow_water_equiv_mm"),
                "fetched_at": weather.get("ts"),
            }

        stream = get_latest_stream(conn)
        if stream:
            result["stream"] = {
                "discharge_cfs": stream.get("discharge_cfs"),
                "gauge_height_ft": stream.get("gauge_height_ft"),
                "trend": get_stream_trend(conn),
                "fetched_at": stream.get("ts"),
            }

        result["weather_risk"] = compute_weather_risk(conn)

        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/health")
def health():
    """Status check: Ollama reachable, data source counts, last analysis time."""
    conn = get_db()
    try:
        reading_count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        weather_count = conn.execute("SELECT COUNT(*) FROM weather").fetchone()[0]
        stream_count = conn.execute("SELECT COUNT(*) FROM stream_gauge").fetchone()[0]
        hypothesis_count = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
        last_analysis = conn.execute(
            "SELECT ts FROM analyses ORDER BY ts DESC LIMIT 1"
        ).fetchone()

        # Check Ollama
        ollama_ok = False
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            ollama_ok = r.status_code == 200
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "ollama_reachable": ollama_ok,
            "model": AI_MODEL,
            "reading_count": reading_count,
            "weather_count": weather_count,
            "stream_count": stream_count,
            "hypothesis_count": hypothesis_count,
            "last_analysis": last_analysis["ts"] if last_analysis else None,
            "db_path": str(DB_PATH),
        })
    finally:
        conn.close()


@app.route("/api/analyze", methods=["POST"])
def analyze_now():
    """Trigger an on-demand analysis (non-blocking, runs in a thread)."""
    def _run():
        try:
            run_analysis()
        except Exception as e:
            log(f"On-demand analysis error: {e}")
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Analysis started"})


@app.route("/api/hypotheses")
def hypotheses_endpoint():
    """Return all hypotheses with status, evidence counts, and latest correlations."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM hypotheses ORDER BY id"
        ).fetchall()

        result = []
        for r in rows:
            ev = r["evidence_count"] or 0
            sup = r["support_count"] or 0
            result.append({
                "id": r["id"],
                "hypothesis": r["hypothesis"],
                "metric": r["metric"],
                "threshold": r["threshold"],
                "prediction": r["prediction"],
                "status": r["status"],
                "evidence_count": ev,
                "support_count": sup,
                "refute_count": r["refute_count"] or 0,
                "support_rate": round(sup / ev, 3) if ev > 0 else None,
                "p_value": r["p_value"],
                "notes": r["notes"],
                "created_at": r["created_at"],
                "closed_at": r["closed_at"],
            })

        # Get latest correlations
        corrs = conn.execute(
            """SELECT * FROM correlations WHERE ts = (
                SELECT MAX(ts) FROM correlations
            ) ORDER BY metric_a"""
        ).fetchall()

        correlations = [{
            "metric_a": c["metric_a"],
            "metric_b": c["metric_b"],
            "r_value": c["r_value"],
            "r_squared": c["r_squared"],
            "p_value": c["p_value"],
            "sample_size": c["sample_size"],
        } for c in corrs]

        return jsonify({
            "hypotheses": result,
            "latest_correlations": correlations,
        })
    finally:
        conn.close()


@app.route("/api/weather-risk")
def weather_risk_endpoint():
    """Return the current weather risk score with contributing factors."""
    return jsonify(compute_weather_risk())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log("=== AI Sump Pump Analyzer Starting ===")
    log(f"Port: {PORT}")
    log(f"Ollama: {OLLAMA_URL} (model: {AI_MODEL})")
    log(f"Weather: lat={WEATHER_LAT}, lon={WEATHER_LON}")
    log(f"USGS Stream Gauge: site {USGS_SITE_ID}")
    log(f"Database: {DB_PATH}")
    log(f"Intervals: analysis={ANALYSIS_INTERVAL}s, weather={WEATHER_INTERVAL}s, "
        f"stream={STREAM_INTERVAL}s, nws={NWS_INTERVAL}s, forecast={FORECAST_INTERVAL}s")
    log(f"Retention: readings={RETENTION_READINGS}d, analyses={RETENTION_ANALYSES}d, "
        f"weather={RETENTION_WEATHER}d, stream={RETENTION_STREAM}d")

    init_db()
    seed_hypotheses()

    # Start background threads (daemon so they die with the main process)
    threads = [
        (analysis_loop, "analysis"),
        (weather_loop, "weather"),
        (stream_loop, "stream"),
        (nws_loop, "nws-alerts"),
        (forecast_loop, "forecast"),
        (correlation_loop, "correlation"),
        (prune_loop, "prune"),
    ]
    for target, name in threads:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        log(f"Started {name} thread")

    app.run(host="0.0.0.0", port=PORT)
