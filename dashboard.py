#!/usr/bin/env python3
"""
Sump Pump Dashboard - Mobile-friendly web UI
Runs on port 8077
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string
import requests
from requests.auth import HTTPDigestAuth


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

SHELLY_IP = os.environ["SHELLY_IP"]
AI_ANALYZER_URL = os.environ.get("AI_ANALYZER_URL", "")
AUTH = HTTPDigestAuth(
    os.environ["SHELLY_USER"],
    os.environ["SHELLY_PASSWORD"],
)
LOG_PATH = Path(__file__).parent / "sump_pump_monitor.log"

app = Flask(__name__)

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Sump Pump</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 16px; max-width: 480px; margin: 0 auto; }
h1 { font-size: 1.3rem; margin-bottom: 16px; color: #94a3b8; }
.card { background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 12px; }
.status-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #334155; }
.status-row:last-child { border-bottom: none; }
.label { color: #94a3b8; font-size: 0.9rem; }
.value { font-size: 1.1rem; font-weight: 600; }
.pump-state { font-size: 1.5rem; text-align: center; padding: 20px; font-weight: 700; border-radius: 12px; margin-bottom: 12px; }
.idle { background: #064e3b; color: #6ee7b7; }
.running { background: #7c2d12; color: #fdba74; }
.off { background: #1e293b; color: #64748b; }
.error { background: #7f1d1d; color: #fca5a5; }
.buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
.btn { padding: 14px; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: opacity 0.2s; }
.btn:active { opacity: 0.7; }
.btn-on { background: #059669; color: white; }
.btn-off { background: #dc2626; color: white; }
.btn-cycle { background: #d97706; color: white; grid-column: span 2; }
.btn-refresh { background: #334155; color: #e2e8f0; grid-column: span 2; }
.log { background: #1e293b; border-radius: 12px; padding: 12px; font-family: monospace; font-size: 0.75rem; line-height: 1.5; max-height: 300px; overflow-y: auto; color: #94a3b8; white-space: pre-wrap; word-break: break-all; }
.log-title { color: #94a3b8; font-size: 0.9rem; margin-bottom: 8px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; }
.badge-ok { background: #064e3b; color: #6ee7b7; }
.badge-warn { background: #78350f; color: #fbbf24; }
.badge-err { background: #7f1d1d; color: #fca5a5; }
.spinner { display: none; }
.loading .spinner { display: inline; }
.ts { color: #64748b; font-size: 0.8rem; text-align: center; margin-top: 12px; }
</style>
</head>
<body>
<h1>Sump Pump Monitor</h1>
<div id="pump-state" class="pump-state off">Loading...</div>
<div class="card" id="status-card">
  <div class="status-row"><span class="label">Power</span><span class="value" id="power">--</span></div>
  <div class="status-row"><span class="label">Current</span><span class="value" id="current">--</span></div>
  <div class="status-row"><span class="label">Voltage</span><span class="value" id="voltage">--</span></div>
  <div class="status-row"><span class="label">Plug Temp</span><span class="value" id="temp">--</span></div>
  <div class="status-row"><span class="label">Energy</span><span class="value" id="energy">--</span></div>
  <div class="status-row"><span class="label">Light</span><span class="value" id="light">--</span></div>
  <div class="status-row"><span class="label">WiFi</span><span class="value" id="wifi">--</span></div>
  <div class="status-row"><span class="label">Uptime</span><span class="value" id="uptime">--</span></div>
  <div class="status-row"><span class="label">Monitor</span><span class="value" id="monitor">--</span></div>
</div>
<div class="card" id="ai-card" style="display:none;">
  <div class="status-row"><span class="label">AI Analysis</span><span class="value" id="ai-status">--</span></div>
  <div class="status-row"><span class="label">Recommendation</span><span class="value" id="ai-rec" style="font-size:0.85rem;font-weight:400;">--</span></div>
  <div class="status-row"><span class="label">Weather</span><span class="value" id="ai-weather" style="font-size:0.85rem;">--</span></div>
  <div class="status-row"><span class="label">Confidence</span><span class="value" id="ai-confidence">--</span></div>
  <div class="status-row"><span class="label">Last Analysis</span><span class="value" id="ai-time" style="font-size:0.8rem;color:#64748b;">--</span></div>
  <div style="text-align:center;padding-top:8px;"><button class="btn btn-refresh" style="grid-column:unset;padding:8px 16px;font-size:0.85rem;" onclick="fetch('/api/ai-analyze',{method:'POST'});this.textContent='Analyzing...';setTimeout(()=>{this.textContent='Analyze Now';refresh();},20000);">Analyze Now</button></div>
</div>
<div class="buttons">
  <button class="btn btn-on" onclick="action('on')">Turn ON</button>
  <button class="btn btn-off" onclick="action('off')">Turn OFF</button>
  <button class="btn btn-cycle" onclick="if(confirm('Power cycle the pump?')) action('cycle')">Power Cycle</button>
  <button class="btn btn-refresh" onclick="refresh()">Refresh</button>
</div>
<div class="log-title">Recent Logs</div>
<div class="log" id="log">Loading...</div>
<div class="ts" id="updated"></div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.error) {
      document.getElementById('pump-state').className = 'pump-state error';
      document.getElementById('pump-state').textContent = 'UNREACHABLE';
      return;
    }
    const ps = document.getElementById('pump-state');
    if (!d.output) { ps.className = 'pump-state off'; ps.textContent = 'SWITCH OFF'; }
    else if (d.power > 100) { ps.className = 'pump-state running'; ps.textContent = 'PUMP RUNNING ' + d.power.toFixed(0) + 'W'; }
    else { ps.className = 'pump-state idle'; ps.textContent = 'IDLE'; }
    document.getElementById('power').textContent = d.power.toFixed(1) + ' W';
    document.getElementById('current').textContent = d.current.toFixed(2) + ' A';
    var vBadge = d.voltage < 110 || d.voltage > 130 ? 'badge-err' : d.voltage < 115 || d.voltage > 125 ? 'badge-warn' : 'badge-ok';
    document.getElementById('voltage').innerHTML = d.voltage.toFixed(1) + ' V <span class="badge ' + vBadge + '">' + d.freq.toFixed(1) + ' Hz</span>';
    var tBadge = d.temp_c > 60 ? 'badge-err' : d.temp_c > 50 ? 'badge-warn' : 'badge-ok';
    document.getElementById('temp').innerHTML = d.temp_c.toFixed(1) + '\u00b0C / ' + d.temp_f.toFixed(1) + '\u00b0F <span class="badge ' + tBadge + '">' + (d.temp_c > 60 ? 'HOT' : d.temp_c > 50 ? 'warm' : 'ok') + '</span>';
    document.getElementById('energy').textContent = (d.energy / 1000).toFixed(3) + ' kWh';
    document.getElementById('light').innerHTML = '<span class="badge ' + (d.illumination === 'dark' ? 'badge-ok' : 'badge-warn') + '">' + d.illumination + '</span>';
    document.getElementById('wifi').innerHTML = d.ssid + ' <span class="badge ' + (d.rssi > -60 ? 'badge-ok' : d.rssi > -75 ? 'badge-warn' : 'badge-err') + '">' + d.rssi + ' dBm</span>';
    var ut = d.uptime; var uts = ut < 3600 ? Math.floor(ut/60) + 'm' : ut < 86400 ? Math.floor(ut/3600) + 'h ' + Math.floor((ut%3600)/60) + 'm' : Math.floor(ut/86400) + 'd ' + Math.floor((ut%86400)/3600) + 'h';
    document.getElementById('uptime').textContent = uts;
    document.getElementById('monitor').innerHTML = d.monitor_status;
    document.getElementById('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('pump-state').className = 'pump-state error';
    document.getElementById('pump-state').textContent = 'CONNECTION ERROR';
  }
  try {
    const lr = await fetch('/api/logs');
    const ld = await lr.json();
    document.getElementById('log').textContent = ld.logs;
  } catch(e) {}
  try {
    const ar = await fetch('/api/ai-insights');
    const ai = await ar.json();
    var ac = document.getElementById('ai-card');
    if (ai.status && ai.status !== 'pending') {
      ac.style.display = '';
      var sb = ai.status === 'normal' ? 'badge-ok' : ai.status === 'watch' ? 'badge-warn' : 'badge-err';
      document.getElementById('ai-status').innerHTML = '<span class="badge ' + sb + '">' + ai.status + '</span>';
      document.getElementById('ai-rec').textContent = ai.recommendation || 'None';
      document.getElementById('ai-confidence').textContent = ai.confidence != null ? (ai.confidence * 100).toFixed(0) + '%' : '--';
      document.getElementById('ai-time').textContent = ai.analyzed_at || '--';
      if (ai.weather) {
        document.getElementById('ai-weather').textContent = ai.weather.temp_c + '\u00b0C, ' + (ai.weather.rain || 0) + 'mm rain, ' + (ai.weather.precip_prob_6h || 0) + '% 6h prob';
      } else {
        document.getElementById('ai-weather').textContent = 'No data';
      }
    }
  } catch(e) {}
}

async function action(cmd) {
  const r = await fetch('/api/' + cmd, {method: 'POST'});
  const d = await r.json();
  setTimeout(refresh, cmd === 'cycle' ? 12000 : 1500);
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


def get_shelly_status():
    try:
        r = requests.get(
            f"http://{SHELLY_IP}/rpc/Shelly.GetStatus", auth=AUTH, timeout=5
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_monitor_status():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "sump-pump-monitor"],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip()
        if active == "active":
            return '<span class="badge badge-ok">running</span>'
        return f'<span class="badge badge-err">{active}</span>'
    except Exception:
        return '<span class="badge badge-warn">unknown</span>'


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    data = get_shelly_status()
    if "error" in data:
        return jsonify({"error": data["error"]})

    sw = data.get("switch:0", {})
    wifi = data.get("wifi", {})
    temp = sw.get("temperature", {})
    energy = sw.get("aenergy", {})
    illum = data.get("illuminance:0", {})
    sys_info = data.get("sys", {})

    return jsonify({
        "output": sw.get("output", False),
        "power": sw.get("apower", 0.0),
        "voltage": sw.get("voltage", 0.0),
        "freq": sw.get("freq", 0.0),
        "current": sw.get("current", 0.0),
        "temp_c": temp.get("tC", 0.0),
        "temp_f": temp.get("tF", 0.0),
        "energy": energy.get("total", 0.0),
        "illumination": illum.get("illumination", "unknown"),
        "ssid": wifi.get("ssid", "?"),
        "rssi": wifi.get("rssi", 0),
        "uptime": sys_info.get("uptime", 0),
        "monitor_status": get_monitor_status(),
    })


@app.route("/api/logs")
def api_logs():
    try:
        lines = LOG_PATH.read_text().strip().split("\n")
        return jsonify({"logs": "\n".join(lines[-30:])})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})


@app.route("/api/on", methods=["POST"])
def api_on():
    try:
        r = requests.get(
            f"http://{SHELLY_IP}/rpc/Switch.Set?id=0&on=true", auth=AUTH, timeout=5
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/off", methods=["POST"])
def api_off():
    try:
        r = requests.get(
            f"http://{SHELLY_IP}/rpc/Switch.Set?id=0&on=false", auth=AUTH, timeout=5
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/ai-insights")
def api_ai_insights():
    if not AI_ANALYZER_URL:
        return jsonify({"status": "disabled", "message": "AI analyzer not configured"})
    try:
        r = requests.get(f"{AI_ANALYZER_URL}/api/insights", timeout=3)
        return jsonify(r.json())
    except Exception:
        return jsonify({"status": "unavailable", "message": "AI analyzer unreachable"})


@app.route("/api/ai-analyze", methods=["POST"])
def api_ai_analyze():
    if not AI_ANALYZER_URL:
        return jsonify({"status": "disabled"})
    try:
        r = requests.post(f"{AI_ANALYZER_URL}/api/analyze", timeout=3)
        return jsonify(r.json())
    except Exception:
        return jsonify({"status": "unavailable"})


@app.route("/api/cycle", methods=["POST"])
def api_cycle():
    try:
        requests.get(
            f"http://{SHELLY_IP}/rpc/Switch.Set?id=0&on=false", auth=AUTH, timeout=5
        )
        import time
        time.sleep(10)
        r = requests.get(
            f"http://{SHELLY_IP}/rpc/Switch.Set?id=0&on=true", auth=AUTH, timeout=5
        )
        return jsonify({"cycled": True})
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8077)
