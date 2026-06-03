#!/usr/bin/env bash
# shelly — query/control the sump-pump Shelly from this Mac, on ANY network.
#
# Background: sump control now runs on toddllm (this Mac is a cold standby). The
# Shelly plug lives on the 192.168.68.x LAN. This Mac roams — when it's on the
# SECOND ISP (192.168.1.x) it CANNOT reach 192.168.68.x directly. toddllm sits on
# BOTH networks and tracks the Shelly's DHCP-drifting IP (MAC 841FE8F85BBC,
# cached at /var/tmp/shelly_last_ip on toddllm), so this script routes through
# toddllm whenever the Mac is off the Shelly LAN (and as a fallback if a direct
# hit fails — e.g. the cached IP is stale after a DHCP drift).
#
# Usage:
#   shelly                               # Switch.GetStatus?id=0 (relay/W/degC) [default]
#   shelly Shelly.GetStatus              # full device status
#   shelly 'Switch.Set?id=0&on=true'     # control — quote args containing &
#
# Tip: alias shelly="$HOME/home-assistant/shelly.sh"   (or symlink into ~/bin)
set -uo pipefail
METHOD="${1:-Switch.GetStatus?id=0}"

remote_shelly() {
  # toddllm resolves the Shelly's current IP itself; works from either network
  # because `ssh toddllm` follows mDNS (toddllm.local) across both.
  ssh toddllm "IP=\$(cat /var/tmp/shelly_last_ip 2>/dev/null || echo 192.168.68.134); curl -fsS -m 6 \"http://\$IP/rpc/$METHOD\""
}

if ifconfig 2>/dev/null | grep -q 'inet 192.168.68.'; then
  echo "shelly: on the Shelly LAN (192.168.68.x) -> trying direct" >&2
  IP="$(cat /var/tmp/shelly_last_ip 2>/dev/null || echo 192.168.68.134)"
  if curl -fsS -m 5 "http://$IP/rpc/$METHOD"; then echo >&2; exit 0; fi
  echo "shelly: direct failed -> falling back via toddllm" >&2
else
  echo "shelly: not on the Shelly LAN (e.g. 2nd ISP) -> via toddllm" >&2
fi
remote_shelly
echo >&2
