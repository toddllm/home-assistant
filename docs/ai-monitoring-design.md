# AI Monitoring Layer — Plan Options

## Context

The sump pump monitor currently uses fixed thresholds and rule-based logic to detect problems (stuck float, overtemp, voltage anomaly, etc.). This works well for known failure modes but misses subtle patterns — gradual motor degradation, seasonal water table shifts, correlations between sensors that a human wouldn't notice.

We want to add an AI layer that analyzes the telemetry stream and provides smarter alerting, anomaly detection, and eventually predictive maintenance. The output of this planning phase is **three candidate architectures** — no implementation yet.

### Hardware Available

| Machine | CPU | RAM | GPU | Ollama Models | Notes |
|---------|-----|-----|-----|---------------|-------|
| **Mac** | M3 Max | 128 GB | Integrated (unified memory) | 50+ models up to 72B | Plenty of headroom |
| **Linux (toddllm)** | i7-12700K | 64 GB (38 GB avail) | RTX 3090 24 GB (8 GB free) | 7 small models (up to 20B) | Running TTS/STT, limited VRAM |

### Data Available (per 30-second poll)

Power (W), voltage (V), current (A), frequency (Hz), temperature (C), illumination (dark/dim/bright), WiFi RSSI (dBm), uptime (s), energy (Wh), switch state (on/off). ~2,880 samples/day.

### Integration Point

The monitor's main loop in `sump_pump_monitor.py` runs every 30 seconds. An AI call can be inserted after the current rule-based checks, either synchronously (blocking the loop) or asynchronously (separate process/thread). The existing `send_notification()` function handles alert delivery — AI insights use the same channel.

---

## Option A: Local Ollama on Mac (Sidecar Analyzer)

**Summary:** A lightweight Python service runs on the Mac alongside Ollama. The Linux monitor ships telemetry to the Mac over the LAN. A small model (8B–14B) analyzes batches of readings periodically and sends findings back.

### Architecture

```
Linux (toddllm)                          Mac (M3 Max, 128GB)
┌──────────────────┐                     ┌──────────────────────────┐
│ sump_pump_       │  HTTP POST          │  ai_analyzer.py          │
│ monitor.py       │──── telemetry ─────▶│                          │
│                  │  (every 30s)        │  Collects readings into  │
│ Existing logic   │                     │  rolling window (last    │
│ stays unchanged  │◀── AI alerts ───────│  1h / 24h / 7d)          │
│                  │  (when triggered)   │                          │
│ send_            │                     │  Every 5 min, feeds      │
│ notification()   │                     │  summary to Ollama       │
│ handles delivery │                     │  (qwen2.5-coder:14b     │
└──────────────────┘                     │   or llama3.1:8b)        │
                                         │                          │
                                         │  Ollama returns:         │
                                         │  - anomaly score         │
                                         │  - natural language note │
                                         │  - recommended action    │
                                         └──────────────────────────┘
```

### How It Works

1. **Data shipping:** Monitor adds one line to its main loop — POST current reading to `http://mac-ip:8078/ingest` (fire-and-forget, non-blocking, with timeout catch so monitor never stalls).
2. **Accumulation:** `ai_analyzer.py` on Mac stores readings in a SQLite DB (simple, no extra infra). Rolling windows: last 1 hour, 24 hours, 7 days.
3. **Periodic analysis (every 5 minutes):** Builds a structured prompt with recent stats (min/max/avg power, voltage trend, cycle count, run durations) and asks the model: "Given this sump pump telemetry, identify any anomalies or concerns."
4. **Alerting:** If the model flags something, POST it back to the Linux monitor's dashboard API or directly call `ntfy.sh`.

### Model Choice

- **qwen2.5-coder:14b** (9 GB) — already installed, good at structured data analysis
- **llama3.1:8b** (5 GB) — fast, low overhead, adequate for pattern matching
- **phi4** (9 GB) — strong reasoning for its size, already installed

### Pros
- All data stays on LAN, zero cloud cost, full privacy
- Mac has massive headroom — 14B model uses <10% of memory
- Monitor on Linux is unchanged except one non-blocking HTTP POST
- Can use larger models (32B, 72B) for deeper analysis if needed
- Works even if internet is down

### Cons
- Mac must be running — if it sleeps or is off, AI layer goes dark
- LAN dependency (Mac ↔ Linux must be reachable)
- Small models may have lower accuracy on subtle anomalies
- Need to write good structured prompts (garbage in, garbage out)
- No pre-trained knowledge of sump pump failure modes specifically

### Estimated Effort
- `ai_analyzer.py` — ~200 lines (Flask endpoint + SQLite + Ollama client + prompt engineering)
- Monitor change — ~10 lines (one HTTP POST in main loop)
- New `.env` vars — `AI_ANALYZER_URL`, `AI_ENABLED`

---

## Option B: Cloud API on Linux (Lightweight, Always-On)

**Summary:** The Linux monitor itself calls a cloud AI API (Claude, OpenAI, or Gemini) periodically to analyze accumulated readings. No extra services, no Mac dependency. The model runs in the cloud; only a small API call is made.

### Architecture

```
Linux (toddllm)
┌──────────────────────────────────────────────┐
│ sump_pump_monitor.py                         │
│                                              │
│ Existing main loop (every 30s)               │
│ ┌──────────────────────────────────────┐     │
│ │ 1. Poll Shelly                       │     │
│ │ 2. Rule-based checks (existing)      │     │
│ │ 3. Append reading to local buffer    │     │
│ │ 4. Every 5 min: call cloud AI API    │──────────▶ Cloud API
│ │    with last 10 readings + 24h stats │     │      (Claude/GPT/Gemini)
│ │ 5. If AI flags issue → notify        │◀──────────
│ └──────────────────────────────────────┘     │
│                                              │
│ Buffer: collections.deque (in-memory,        │
│ last 2880 readings = 24 hours)               │
└──────────────────────────────────────────────┘
```

### How It Works

1. **In-memory buffer:** Each poll appends the reading to a `deque(maxlen=2880)` — 24 hours of data at 30s intervals. No database needed.
2. **Periodic AI call (every 5 minutes):** Builds a prompt with: last 10 raw readings, 1h/6h/24h aggregates (min, max, avg, stddev for each field), pump cycle count and durations, any rule-based alerts that fired.
3. **Structured response:** API returns JSON with `anomaly_detected` (bool), `severity` (low/medium/high), `summary` (string), `recommendation` (string).
4. **Alerting:** If anomaly detected, feeds into existing `send_notification()`.

### API Choice

| Provider | Model | Cost (est.) | Latency | Notes |
|----------|-------|-------------|---------|-------|
| **Anthropic** | claude-haiku-4-5 | ~$0.02/day (288 calls × ~500 tokens) | ~1s | Cheapest, fast, great reasoning |
| **OpenAI** | gpt-4.1-mini | ~$0.03/day | ~1s | Widely available |
| **Google** | gemini-2.5-flash | ~$0.01/day | ~1s | Very cheap, generous free tier |

At 288 calls/day (every 5 min) with ~500 input tokens and ~100 output tokens each, this costs pennies per day.

### Pros
- Simplest architecture — everything stays in one script on Linux
- No extra services to run, no Mac dependency
- Cloud models are much smarter than local 8B models
- Always-on (as long as internet is up)
- Tiny resource footprint on Linux (one HTTP call every 5 min)
- Can switch providers easily (all use similar chat APIs)

### Cons
- Requires internet — if internet is down, AI layer is offline (rule-based still works)
- Data leaves the network (telemetry only, no credentials — low sensitivity)
- Ongoing cost (tiny but nonzero — ~$1-10/month depending on model)
- API key management (another secret in `.env`)
- Rate limits / API changes could disrupt service

### Estimated Effort
- Add ~80 lines to `sump_pump_monitor.py` (buffer, periodic call, response parsing)
- New `.env` vars — `AI_PROVIDER`, `AI_API_KEY`, `AI_MODEL`, `AI_INTERVAL_MINUTES`
- No new files or services needed

---

## Option C: Hybrid (Cloud Brain + Local Fallback)

**Summary:** Combines Options A and B. Cloud API is the primary analyzer (smarter, always available when internet is up). When internet is down or cloud is unreachable, falls back to a local Ollama model on Mac. The best of both worlds, with no single point of failure.

### Architecture

```
Linux (toddllm)                      Mac (M3 Max)
┌─────────────────────┐              ┌──────────────────┐
│ sump_pump_monitor.py│              │ ai_analyzer.py   │
│                     │              │ (Ollama sidecar) │
│ Main loop:          │              │                  │
│ 1. Poll Shelly      │  telemetry   │ SQLite history   │
│ 2. Rule checks      │─────────────▶│ Rolling analysis │
│ 3. Buffer reading   │              │ Local Ollama     │
│ 4. Every 5 min:     │              └──────────────────┘
│    ┌────────────┐   │                     ▲
│    │ Try cloud  │───────▶ Cloud API       │
│    │ API first  │   │    (Claude/GPT)     │
│    └─────┬──────┘   │                     │
│          │          │                     │
│     fail?│          │    fallback          │
│          └──────────│─────────────────────┘
│                     │
│ 5. Merge analysis   │
│ 6. Notify if needed │
└─────────────────────┘
```

### How It Works

1. **Data flows to both:** Monitor ships telemetry to Mac sidecar (for history/local analysis) AND buffers locally (for cloud calls). Mac accumulates long-term data regardless.
2. **Cloud-primary analysis:** Every 5 minutes, monitor calls cloud API with recent stats. Cloud model does the heavy reasoning.
3. **Automatic fallback:** If cloud call fails (timeout, API error, no internet), monitor calls the Mac sidecar's `/analyze` endpoint instead. Ollama handles it locally.
4. **Long-term learning (Mac):** The Mac sidecar builds up weeks/months of historical data in SQLite. Even when cloud is primary, local Ollama periodically runs deeper analysis on the full dataset (e.g., weekly trend report).
5. **Unified alerting:** Both paths feed into the same `send_notification()` with a tag indicating source (`[AI-cloud]` or `[AI-local]`).

### Resilience Matrix

| Internet | Mac | AI Analysis |
|----------|-----|-------------|
| Up | Up | Cloud (primary) + Mac (history) |
| Up | Down | Cloud only |
| Down | Up | Mac / Ollama fallback |
| Down | Down | Rule-based only (existing system, fully functional) |

### Pros
- Most resilient — no single point of failure for AI layer
- Cloud models for accuracy, local models for availability
- Long-term data accumulation on Mac enables trend analysis
- Can run expensive cloud models rarely (daily summary) + cheap local models frequently
- Graceful degradation at every level

### Cons
- Most complex to build and maintain (two AI paths + fallback logic)
- Requires both Mac and cloud API setup
- More moving parts = more things that can break
- Overkill if the simple options work well enough
- Higher total cost (cloud API + Mac power)

### Estimated Effort
- `ai_analyzer.py` on Mac — ~250 lines (same as Option A, plus long-term trend endpoint)
- Monitor changes — ~120 lines (cloud call + fallback logic + buffer)
- New `.env` vars — all from both A and B
- New file: `ai_prompts.py` — shared prompt templates used by both paths

---

## Comparison Matrix

| Factor | A: Local Ollama | B: Cloud API | C: Hybrid |
|--------|----------------|-------------|-----------|
| **Complexity** | Medium | Low | High |
| **Cost** | $0 | ~$1-10/mo | ~$1-10/mo |
| **AI Quality** | Good (8-14B) | Best (frontier models) | Best + Good fallback |
| **Privacy** | Full (LAN only) | Telemetry leaves network | Mixed |
| **Reliability** | Mac must be on | Internet required | Resilient to both |
| **Mac dependency** | Yes | No | Partial |
| **Internet dependency** | No | Yes | Graceful fallback |
| **Setup effort** | ~2 hours | ~1 hour | ~3 hours |
| **New files** | 1 (ai_analyzer.py) | 0 | 2 (ai_analyzer.py, ai_prompts.py) |
| **Lines of code** | ~210 | ~80 | ~370 |

## Recommendation

**Start with Option B** (cloud API). It's the simplest, requires no new services, and gives you the smartest models for pennies/day. The data being sent (watts, volts, temperature) has zero privacy sensitivity. If you later want local fallback, it's easy to layer Option A on top — the prompt engineering and analysis logic transfer directly, making it a natural path to Option C.

## Decision (2026-02-25)

**Built Option A** (local Ollama on Mac) as a POC. Chose local-first to avoid cloud dependencies and prove the concept with zero cost. The `qwen3:32b-q4_K_M` model on M3 Max performs well (5-18s per analysis) with compact, rules-first prompts. See [ai-analyzer-poc.md](ai-analyzer-poc.md) for full results.

## Prompt Engineering (Shared Across All Options)

The quality of AI analysis depends heavily on the prompt. Here's the planned structure:

```
You are a sump pump monitoring assistant. Analyze the following telemetry
from a Shelly smart plug connected to a residential sump pump.

CURRENT READING:
  Power: {power}W | Voltage: {voltage}V | Current: {current}A
  Temperature: {temp_c}C | Light: {illumination} | RSSI: {rssi} dBm
  Uptime: {uptime}s | Switch: {output}

LAST HOUR (12 readings):
  Power:   min={min_p}W  max={max_p}W  avg={avg_p}W
  Voltage: min={min_v}V  max={max_v}V  avg={avg_v}V
  Temp:    min={min_t}C  max={max_t}C  avg={avg_t}C  trend={trend_t}
  Pump cycles: {cycle_count} (avg duration: {avg_duration}s)

LAST 24 HOURS:
  Total pump run time: {total_run}min | Cycle count: {cycles_24h}
  Energy consumed: {energy_delta} Wh
  Voltage range: {v_range} | Temp range: {t_range}
  Alerts fired: {recent_alerts}

KNOWN BASELINE:
  Normal pump power: 480-530W | Normal cycle: 60-90s
  Normal idle power: 0W | Normal plug temp: 28-38C

Respond in JSON:
{
  "status": "normal" | "watch" | "warning" | "critical",
  "anomalies": ["list of specific observations"],
  "recommendation": "what to do, if anything",
  "confidence": 0.0-1.0
}
```
