# AI Sump Pump Analyzer — POC Results

**Date:** 2026-02-25
**Status:** POC complete, not yet deployed to production
**Architecture:** Option A from [ai-monitoring-design.md](ai-monitoring-design.md) — Local Ollama on Mac

---

## What Was Built

A Flask service (`ai_analyzer.py`, 556 lines) that runs on the Mac (M3 Max, 128GB) and:

1. **Receives telemetry** from the Linux sump pump monitor via `POST /ingest`
2. **Stores readings** in SQLite with 30-day retention
3. **Fetches weather** from Open-Meteo every hour (free, no API key)
4. **Analyzes via Ollama** every 5 minutes using `qwen3:32b-q4_K_M` (20GB)
5. **Serves insights** via `GET /api/insights` (proxied through the dashboard)

The monitor (`sump_pump_monitor.py`) got a fire-and-forget POST (+21 lines). The dashboard (`dashboard.py`) got an AI card with status badge, recommendation, weather, and an "Analyze Now" button (+48 lines).

```
Linux (toddllm)                          Mac (M3 Max, 128GB)
┌─────────────────────┐                 ┌──────────────────────────┐
│ sump_pump_monitor.py│  POST /ingest   │ ai_analyzer.py (:8078)   │
│                     │────────────────>│                          │
│ +1 line: fire-and-  │  (every 30s,   │ SQLite (readings,        │
│  forget POST        │   2s timeout)  │  weather, analyses)      │
│                     │                 │                          │
│ dashboard.py (:8077)│  GET /insights  │ Background threads:      │
│                     │────────────────>│  - Every 5m: Ollama      │
│ +AI insights card   │<────────────────│  - Every 1h: Open-Meteo  │
│                     │                 │                          │
│ Rule-based safety   │                 │ Model: qwen3:32b-q4_K_M  │
│ (unchanged)         │                 │ Prompt: compact, JSON    │
└─────────────────────┘                 └──────────────────────────┘
```

---

## Key Learnings from Testing

### 1. Compact, rules-first prompts beat verbose data dumps

**Problem:** A verbose ~1800-char prompt with full telemetry tables caused the model to miss anomalies — it returned "normal" for data with clearly elevated power (575W vs 520W baseline).

**Solution:** Rewrote to a ~1050-char prompt with rules at the top:
```
RULES (apply strictly):
- Max running power >520W → at least "watch"
- >6 pump cycles/hour + no rain → "watch" or "warning"
- Voltage <115V → "warning"
- Plug temp >40C → "warning"
```

**Result:** Model consistently flags anomalies now. The rules act as a decision framework, and the data backs them up.

### 2. `/no_think` is required for qwen3 + JSON format

Qwen 3 models use internal chain-of-thought by default. When combined with Ollama's `format: "json"` constraint, the thinking overhead either gets suppressed awkwardly or conflicts with JSON output. Adding `/no_think` at the start of the prompt forces direct JSON output and halved response times.

### 3. Separate "running power" from overall averages

**Problem:** The model saw "avg power: 105W" (which includes idle 0W readings) and thought the pump was drawing less power than expected.

**Solution:** Added `running_power` stats that only count readings where power > 100W. Now the prompt shows "running power 495-575W avg 525W" — the actual pump draw.

### 4. Cycle duration from timestamps is unreliable

SQLite `DEFAULT` timestamps are insert-time, not reading-time. For batch inserts (or multiple readings in the same second), duration computation breaks. Fixed by estimating: `(reading_count_in_cycle) * 30s`.

### 5. Model response times are fast enough

| Scenario | Tokens | Time | Within 5-min cycle? |
|----------|--------|------|---------------------|
| Normal (idle) | 38 | 5s | Yes |
| Elevated power (watch) | 72 | 9s | Yes |
| Multi-anomaly (warning) | 73 | 8s | Yes |
| Minimal data | 38 | 8s | Yes |
| First call (cold model) | 41 | 17s | Yes |

Even the worst case (17s cold start) is well under 5 minutes.

### 6. Weather context is valuable for cross-correlation

Open-Meteo returns current conditions + 6-hour precipitation probability. The model correctly identifies "11 cycles with 0mm rain" as suspicious vs "11 cycles during heavy rain" as expected. This is exactly the kind of insight that fixed thresholds can't provide.

---

## Test Results

| Scenario | Expected | Model Said | Confidence | Correct? |
|----------|----------|------------|-----------|----------|
| Normal: 2 cycles, all baseline | normal | normal | 0.95 | Yes |
| Elevated power: 575W max, 11 cycles, no rain | watch | watch | 0.75 | Yes |
| Motor degrade + brownout (110V) + heat (44.5C) | warning | warning | 0.95 | Yes |
| Minimal data: 3 idle readings | normal | normal | 0.95 | Yes |

---

## What's NOT Deployed Yet

- `AI_ENABLED=false` in the Linux `.env` — flip to `true` when ready
- `ai_analyzer.py` needs to be run as a service on the Mac (launchd or similar)
- Dashboard AI card is hidden until the first analysis arrives (graceful degradation)

---

## Future Ideas (from original plan)

- **AcuRite weather station integration** via rtl_433 + RTL-SDR dongle (~$25) for hyperlocal weather
- **Daily digest email** — morning summary of pump activity, trends, weather correlation
- **Trend detection** — weekly analysis using longer historical window
- **Dashboard history charts** — pump cycles per day, power draw trends over weeks
- **Model comparison** — test `qwen3-coder:30b` (18GB) or `qwen3-coder-next` (51GB) for quality tradeoffs

---

## Files

| File | Action | Notes |
|------|--------|-------|
| `ai_analyzer.py` | NEW (556 lines) | Flask + SQLite + Ollama + Open-Meteo |
| `sump_pump_monitor.py` | +21 lines | Config, `ship_to_analyzer()`, main loop call |
| `dashboard.py` | +48 lines | AI card, proxy endpoint, "Analyze Now" button |
| `.env.example` | +8 lines | AI config block |
| `.env` | +8 lines | AI config (disabled by default) |
| `.gitignore` | +1 line | `ai_analyzer.db` |
