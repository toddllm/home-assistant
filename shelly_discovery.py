#!/usr/bin/env python3
"""
Shelly Plug discovery helpers for the sump pump monitor and guardian.

Resolution order:
1. explicit candidate IPs from caller / environment
2. /var/tmp/shelly_last_ip cache
3. 192.168.68.0/24 scan by expected MAC
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable


DEFAULT_MAC = "841FE8F85BBC"
DEFAULT_CIDR = "192.168.68.0/24"
DEFAULT_CACHE_PATH = "/var/tmp/shelly_last_ip"


def normalize_mac(value: str | None) -> str:
    return "".join(ch for ch in (value or "").upper() if ch in "0123456789ABCDEF")


def _log(logger: Callable[[str], None] | None, message: str) -> None:
    if logger:
        logger(message)


def rpc_get_json(host: str, method: str, timeout: float = 2.0) -> dict | None:
    url = f"http://{host}/rpc/{method}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def shelly_mac_at(host: str, timeout: float = 2.0) -> str | None:
    info = rpc_get_json(host, "Shelly.GetDeviceInfo", timeout=timeout)
    if not info:
        return None
    return normalize_mac(info.get("mac") or info.get("id"))


def host_matches_mac(host: str, expected_mac: str, timeout: float = 2.0) -> bool:
    expected = normalize_mac(expected_mac)
    return bool(expected and shelly_mac_at(host, timeout=timeout) == expected)


def _read_cache(cache_path: str | Path | None) -> str | None:
    if not cache_path:
        return None
    path = Path(cache_path)
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


def _write_cache(ip: str, cache_path: str | Path | None) -> None:
    if not cache_path:
        return
    try:
        Path(cache_path).write_text(ip + "\n")
    except OSError:
        pass


def _candidate_ips(extra_candidates: Iterable[str] | None, cache_path: str | Path | None) -> list[str]:
    candidates: list[str] = []

    for value in extra_candidates or []:
        if value and value not in candidates:
            candidates.append(value)

    for env_key in ("SHELLY_IP", "SHELLY_LAST_IP"):
        value = os.environ.get(env_key, "").strip()
        if value and value not in candidates:
            candidates.append(value)

    cached = _read_cache(cache_path)
    if cached and cached not in candidates:
        candidates.append(cached)

    return candidates


def scan_subnet_for_shelly(
    expected_mac: str = DEFAULT_MAC,
    cidr: str = DEFAULT_CIDR,
    timeout: float = 1.0,
    workers: int = 64,
) -> str | None:
    expected = normalize_mac(expected_mac)
    if not expected:
        return None

    try:
        hosts = list(ipaddress.ip_network(cidr, strict=False).hosts())
    except ValueError:
        return None

    def probe(host: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        ip = str(host)
        return ip if host_matches_mac(ip, expected, timeout=timeout) else None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(probe, host) for host in hosts]
        for future in as_completed(futures):
            found = future.result()
            if found:
                for pending in futures:
                    pending.cancel()
                return found

    return None


def find_shelly_ip(
    expected_mac: str | None = None,
    candidates: Iterable[str] | None = None,
    cidr: str | None = None,
    cache_path: str | Path | None = DEFAULT_CACHE_PATH,
    logger: Callable[[str], None] | None = None,
) -> str | None:
    expected = normalize_mac(expected_mac or os.environ.get("SHELLY_MAC") or DEFAULT_MAC)
    network = cidr or os.environ.get("SHELLY_SCAN_CIDR") or DEFAULT_CIDR

    for candidate in _candidate_ips(candidates, cache_path):
        if host_matches_mac(candidate, expected, timeout=2.0):
            _write_cache(candidate, cache_path)
            return candidate

    _log(logger, f"Shelly discovery: scanning {network} for MAC {expected}")
    found = scan_subnet_for_shelly(expected, cidr=network)
    if found:
        _log(logger, f"Shelly discovery: found MAC {expected} at {found}")
        _write_cache(found, cache_path)
        return found

    _log(logger, f"Shelly discovery: no match for MAC {expected} on {network}")
    return None


if __name__ == "__main__":
    ip = find_shelly_ip(logger=print)
    if ip:
        print(ip)
        raise SystemExit(0)
    raise SystemExit(1)
