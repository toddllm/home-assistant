# Remote Access — Cloudflare Tunnel

Secure remote access to the sump pump dashboard without exposing any ports to the internet.

## Why Cloudflare Tunnel

The dashboard runs on `http://192.168.68.145:8077` — only reachable from the local network. To access it remotely (phone on cellular, laptop on another network), you need a secure path in.

| Approach | Pros | Cons |
|----------|------|------|
| Port forwarding | Simple | Exposes port to internet, requires static IP, firewall rules |
| VPN (WireGuard/OpenVPN) | Full LAN access | Need to manage VPN server, certs, client config |
| **Cloudflare Tunnel** | Zero ports open, free tier, built-in auth | Depends on Cloudflare, requires domain |

Cloudflare Tunnel creates an outbound-only connection from the Linux server to Cloudflare's edge. No inbound ports are opened on the router or firewall. Traffic flows:

```
Phone/Laptop
     │
     │  HTTPS (encrypted)
     ▼
Cloudflare Edge
(Access authentication)
     │
     │  Encrypted tunnel (outbound-only from server)
     ▼
cloudflared (on toddllm)
     │
     │  localhost:8077
     ▼
Dashboard (Flask)
```

---

## Prerequisites

- A domain managed by Cloudflare (or transfer one — free DNS)
- A Cloudflare account (free tier is sufficient)
- SSH access to the Linux server (toddllm)

---

## Step 1: Install cloudflared on Linux

```bash
# Debian/Ubuntu
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb

# Verify
cloudflared --version
```

---

## Step 2: Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This opens a browser to authorize the connection. Select the domain you want to use. A certificate is saved to `~/.cloudflared/cert.pem`.

---

## Step 3: Create the Tunnel

```bash
# Create a named tunnel
cloudflared tunnel create sump-pump

# Note the tunnel ID (UUID) that's printed — you'll need it
# A credentials file is saved to ~/.cloudflared/<TUNNEL_ID>.json
```

---

## Step 4: Configure the Tunnel

Create the config file:

```bash
mkdir -p ~/.cloudflared
```

**`~/.cloudflared/config.yml`:**
```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/tdeshane/.cloudflared/<TUNNEL_ID>.json

ingress:
  # Dashboard
  - hostname: sump.yourdomain.com
    service: http://localhost:8077
  # Catch-all (required)
  - service: http_status:404
```

---

## Step 5: Create DNS Route

```bash
cloudflared tunnel route dns sump-pump sump.yourdomain.com
```

This creates a CNAME record pointing `sump.yourdomain.com` to the tunnel.

---

## Step 6: Test the Tunnel

```bash
# Run in foreground first to verify
cloudflared tunnel run sump-pump
```

Visit `https://sump.yourdomain.com` — you should see the dashboard.

---

## Step 7: Run as a systemd Service

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

Or create a manual service if you want more control:

**`/etc/systemd/system/cloudflared.service`:**
```ini
[Unit]
Description=Cloudflare Tunnel for Sump Pump Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tdeshane
ExecStart=/usr/bin/cloudflared tunnel run sump-pump
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
```

---

## Step 8: Add Cloudflare Access (Authentication)

The tunnel alone makes the dashboard reachable, but anyone with the URL could access it. Add Cloudflare Access to require authentication.

### In the Cloudflare Dashboard (Zero Trust):

1. Go to **Zero Trust** > **Access** > **Applications**
2. Click **Add an application** > **Self-hosted**
3. Configure:
   - **Application name:** Sump Pump Dashboard
   - **Session duration:** 24 hours (or longer — you don't want to re-auth constantly)
   - **Application domain:** `sump.yourdomain.com`
4. Add a **policy:**
   - **Policy name:** Owner access
   - **Action:** Allow
   - **Include rule:** Emails — `your-email@gmail.com`
5. **Authentication method:** One-time PIN (emailed to you) — simplest, no extra setup

### How It Works

When you visit `sump.yourdomain.com`:
1. Cloudflare Access intercepts the request
2. Prompts you to enter your email
3. Sends a one-time PIN to your email
4. After verification, sets a cookie (valid for session duration)
5. Subsequent requests pass through to the dashboard

No passwords to manage, no VPN client needed. Works on any device with a browser.

---

## Security Considerations

### What's Protected
- **No open ports:** The tunnel is outbound-only from the server. Nothing is listening on a public port.
- **TLS everywhere:** Cloudflare handles HTTPS certificates automatically. Traffic is encrypted end-to-end.
- **Authentication:** Cloudflare Access requires email verification before any request reaches the dashboard.
- **Rate limiting:** Cloudflare provides DDoS protection by default.

### What to Be Aware Of
- **Cloudflare can see traffic:** They terminate TLS at their edge. The dashboard traffic (power readings, pump status) is low-sensitivity, but it does pass through Cloudflare's infrastructure.
- **Dashboard has no auth of its own:** The Flask app has no login. It relies entirely on Cloudflare Access for authentication. If the tunnel were misconfigured to bypass Access, the dashboard would be open.
- **Dashboard controls the pump:** The `/api/on`, `/api/off`, and `/api/cycle` endpoints are POST-only but have no additional auth. Cloudflare Access is the sole gatekeeper for remote access.

### Hardening (Optional)

- **Restrict tunnel to dashboard only:** The config already limits the tunnel to `localhost:8077`. The Shelly plug API (port 80 on a different IP) is never exposed.
- **Add a second Access policy:** Require both email AND IP country (e.g., US only) to further limit access.
- **Service token for API access:** If you later want the AI layer to access the dashboard remotely, create a Cloudflare Access service token instead of email-based auth.
- **Audit logs:** Cloudflare Zero Trust logs every access attempt — useful for security review.

---

## Services After Setup

Three systemd services running on toddllm:

| Service | Purpose | Port |
|---------|---------|------|
| `sump-pump-monitor` | State machine, alerts, pump control | — |
| `sump-pump-dashboard` | Web UI + REST API | 8077 (local) |
| `cloudflared` | Tunnel to Cloudflare edge | — (outbound only) |

---

## Turning the Tunnel On/Off

```bash
# Stop remote access (dashboard still works locally)
sudo systemctl stop cloudflared

# Resume remote access
sudo systemctl start cloudflared

# Check status
systemctl status cloudflared
```

The monitoring and dashboard continue to work locally regardless of tunnel state.

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| Tunnel won't connect | `cloudflared tunnel run sump-pump` in foreground — look for auth errors |
| Dashboard loads but is blank | Verify `sump-pump-dashboard.service` is active on port 8077 |
| Access page never appears | Verify the Access application is configured for the correct hostname |
| PIN email never arrives | Check spam folder; verify email in Access policy matches exactly |
| Connection refused after reboot | `sudo systemctl enable cloudflared` to persist across reboots |

---

## Cost

- **Cloudflare Tunnel:** Free (included in all Cloudflare plans)
- **Cloudflare Access:** Free for up to 50 users (we need 1)
- **Domain:** Required (if you don't already have one, `.com` is ~$10/year via Cloudflare Registrar)
