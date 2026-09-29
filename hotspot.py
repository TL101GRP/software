#!/usr/bin/env python3
"""
hotspot_csi.py — turn this laptop into a Wi-Fi AP for a single-ESP CSI radar.
Arch Linux / NetworkManager version.

No discovery. Just ping-sweep the entire /24 continuously forever.
The ESP32 will reply to its own IP; we don't care which one it is.
"""

import subprocess
import time
import signal
import sys
import re
import threading
from concurrent.futures import ThreadPoolExecutor

SSID        = "CSI_RADAR"
PASSWORD    = "radar12345"
CON_NAME    = "csi-radar-hotspot"
BAND        = "bg"
CHANNEL     = 6

# How often we do a full sweep of the /24. 1.0s => each IP pinged ~1Hz.
SWEEP_INTERVAL = 1.0

def run(cmd, check=True, capture=True):
    return subprocess.run(cmd, shell=True, check=check,
                          capture_output=capture, text=True)

def log(msg): print(msg, flush=True)

# ------------------------------------------------------------------
# Wi-Fi interface detection
# ------------------------------------------------------------------

def get_wifi_iface():
    out = run("nmcli -t -f DEVICE,TYPE device").stdout
    for line in out.splitlines():
        if not line.strip(): continue
        parts = line.split(":")
        if len(parts) == 2 and parts[1] == "wifi":
            return parts[0]
    raise RuntimeError("No Wi-Fi interface found")

def check_ap_support(iface):
    out = run(f"nmcli -f WIFI-PROPERTIES.AP device show {iface}").stdout
    if "yes" not in out.lower():
        raise RuntimeError(f"Interface {iface} does NOT support AP mode.\n{out}")
    log(f"[+] {iface} supports AP mode")

# ------------------------------------------------------------------
# Hotspot lifecycle
# ------------------------------------------------------------------

def disconnect_wifi(iface):
    log("[*] Disconnecting current Wi-Fi...")
    run(f"nmcli device disconnect {iface}", check=False)
    time.sleep(1)

def kill_existing_profile():
    run(f"nmcli connection down {CON_NAME}", check=False)
    run(f"nmcli connection delete {CON_NAME}", check=False)

def start_hotspot(iface):
    log(f"[*] Creating hotspot '{SSID}' on {iface} (channel {CHANNEL})...")
    kill_existing_profile()

    add_cmd = (
        f"nmcli connection add type wifi ifname {iface} con-name {CON_NAME} "
        f"autoconnect no ssid '{SSID}' "
        f"802-11-wireless.mode ap "
        f"802-11-wireless.band {BAND} "
        f"802-11-wireless.channel {CHANNEL} "
        f"ipv4.method shared "
        f"ipv6.method disabled "
        f"wifi-sec.key-mgmt wpa-psk "
        f"wifi-sec.psk '{PASSWORD}'"
    )
    r = run(add_cmd, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"nmcli connection add failed:\n{r.stderr}\n{r.stdout}")

    r = run(f"nmcli connection up {CON_NAME}", check=False)
    if r.returncode != 0:
        logs = run("journalctl -u NetworkManager -n 30 --no-pager",
                   check=False).stdout
        raise RuntimeError(
            f"nmcli connection up failed:\n{r.stderr}\n{r.stdout}\n"
            f"--- NetworkManager last 30 lines ---\n{logs}"
        )

    time.sleep(3)

    state = run(f"nmcli -t -f GENERAL.STATE device show {iface}").stdout.strip()
    log(f"[i] {iface} state: {state}")
    if "100 (connected)" not in state:
        raise RuntimeError(f"Interface not connected after hotspot up: {state}")

    addr = run(f"nmcli -t -f IP4.ADDRESS device show {iface}").stdout.strip()
    log(f"[+] Hotspot up. Gateway IP: {addr}")

    m = re.search(r"(\d+\.\d+\.\d+)\.", addr)
    if not m:
        raise RuntimeError(f"Can't parse subnet from: {addr}")
    return m.group(1)   # e.g. "10.42.0"

# ------------------------------------------------------------------
# Continuous subnet pinger
# ------------------------------------------------------------------

def ping_one(ip):
    """Fire one ping with a short timeout. Return True on reply."""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False

def continuous_ping_sweep(prefix, stop):
    """
    Endlessly ping 10.42.0.2 through 10.42.0.254 in parallel.
    Logs any host that replies (for information only).
    """
    targets = [f"{prefix}.{i}" for i in range(2, 255)]
    log(f"[*] Continuously pinging {len(targets)} hosts in {prefix}.0/24")
    log(f"[*] Sweep interval: {SWEEP_INTERVAL}s")

    # 128 parallel ping processes: enough to finish a sweep in <2s
    # without thrashing the system.
    with ThreadPoolExecutor(max_workers=128) as pool:
        sweep_n = 0
        while not stop.is_set():
            sweep_n += 1
            t0 = time.time()
            results = pool.map(ping_one, targets)

            alive = []
            for ip, ok in zip(targets, results):
                if ok:
                    alive.append(ip)

            if alive:
                log(f"[+] Sweep {sweep_n}: alive = {', '.join(alive)}")

            elapsed = time.time() - t0
            # Sleep just enough to keep the interval consistent.
            remaining = SWEEP_INTERVAL - elapsed
            if remaining > 0:
                time.sleep(remaining)

# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------

def cleanup(iface, original_conn):
    log("\n[*] Shutting down hotspot...")
    run(f"nmcli connection down {CON_NAME}", check=False)
    run(f"nmcli connection delete {CON_NAME}", check=False)
    if original_conn:
        log(f"[*] Reconnecting to '{original_conn}'...")
        run(f"nmcli connection up {original_conn}", check=False)
    else:
        run(f"nmcli device connect {iface}", check=False)
    log("[+] Done.")

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    iface = get_wifi_iface()
    log(f"[i] Using Wi-Fi interface: {iface}")
    check_ap_support(iface)

    original = None
    out = run("nmcli -t -f NAME,DEVICE connection show --active").stdout
    for line in out.splitlines():
        if ":" not in line: continue
        name, dev = line.rsplit(":", 1)
        if dev == iface:
            original = name
            break
    if original:
        log(f"[i] Will restore '{original}' on exit")

    stop = threading.Event()

    def sigint(sig, frame):
        stop.set()
        cleanup(iface, original)
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint)
    signal.signal(signal.SIGTERM, sigint)

    try:
        disconnect_wifi(iface)
        prefix = start_hotspot(iface)
        continuous_ping_sweep(prefix, stop)
    except Exception as e:
        log(f"[!] Error: {e}")
        cleanup(iface, original)
        sys.exit(1)

if __name__ == "__main__":
    main()