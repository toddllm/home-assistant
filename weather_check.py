#!/usr/bin/env python3
"""
Small Open-Meteo helper for the sump pump monitor + guardian.

Returns a "wetness" assessment so other code can decide whether
events like "pump has not run in 12 h" are urgent (wet weather =
flooding risk) or expected (dry spell = pump correctly idle).

Cached for 10 minutes per process invocation. Failures degrade
gracefully — return is_wet=False so we don't spam URGENT mails
when the weather API is down.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
CACHE_SECONDS = 600  # 10 minutes


def _fetch(lat: float, lon: float, timeout: float = 8.0) -> dict | None:
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "precipitation,rain,showers",
        "hourly": "precipitation",
        "past_days": 1,
        "forecast_days": 2,
        "timezone": "America/New_York",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def assess(
    lat: float | None = None,
    lon: float | None = None,
    recent_threshold_mm: float = 1.0,
    forecast_threshold_mm: float = 2.0,
) -> dict[str, Any]:
    """Return dict with: recent_rain_mm, current_rain_mm_h, forecast_24h_mm, is_wet, ok."""
    lat = float(lat if lat is not None else os.environ.get("WEATHER_LAT", "42.938"))
    lon = float(lon if lon is not None else os.environ.get("WEATHER_LON", "-74.1853"))

    now = time.time()
    if CACHE["payload"] and now - CACHE["ts"] < CACHE_SECONDS:
        return CACHE["payload"]

    data = _fetch(lat, lon)
    if not data:
        result = {
            "ok": False,
            "recent_rain_mm": None,
            "current_rain_mm_h": None,
            "forecast_24h_mm": None,
            "is_wet": False,  # safe default — don't escalate URGENT on missing data
        }
        CACHE["payload"] = result
        CACHE["ts"] = now
        return result

    current = data.get("current", {}) or {}
    current_rain = float(current.get("precipitation", 0) or current.get("rain", 0) or 0)

    hourly = data.get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    precip = hourly.get("precipitation", []) or []

    # Build aligned (epoch_ts, mm) series.
    series = []
    for t, p in zip(times, precip):
        try:
            tt = time.mktime(time.strptime(t, "%Y-%m-%dT%H:%M"))
            pp = float(p or 0)
            series.append((tt, pp))
        except (ValueError, TypeError):
            continue

    recent = sum(p for tt, p in series if now - 86400 <= tt <= now)
    forecast = sum(p for tt, p in series if now < tt <= now + 86400)

    is_wet = (
        current_rain > 0.1
        or recent >= recent_threshold_mm
        or forecast >= forecast_threshold_mm
    )

    result = {
        "ok": True,
        "recent_rain_mm": round(recent, 2),
        "current_rain_mm_h": round(current_rain, 2),
        "forecast_24h_mm": round(forecast, 2),
        "is_wet": is_wet,
    }
    CACHE["payload"] = result
    CACHE["ts"] = now
    return result


if __name__ == "__main__":
    r = assess()
    print(json.dumps(r, indent=2))
