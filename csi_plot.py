#!/usr/bin/env python3
"""
csi_reader.py — live amplitude AND phase from ESP32 CSI over serial.
"""

import sys, time
import numpy as np
import serial
import matplotlib.pyplot as plt
from collections import deque

# ---- CONFIG ----
PORT      = "/dev/ttyUSB0"     # Windows: "COM5"
BAUD      = 921600
PLOT_HZ   = 20
HISTORY   = 200

# Bins to ignore (DC spike at 0, null region ~28-37)
DEAD_BINS = np.array([0] + list(range(28, 38)))

# ---- OPEN SERIAL ----
print(f"[*] Opening {PORT} @ {BAUD}")
ser = serial.Serial(PORT, BAUD, timeout=1)
ser.reset_input_buffer()

# ---- PLOT: 3 panels ----
plt.ion()
fig, axes = plt.subplots(3, 1, figsize=(12, 8))
fig.suptitle("ESP32 CSI — live")

ax_amp, ax_phase, ax_wf = axes

amp_line, = ax_amp.plot([], [], lw=1, color='tab:blue')
ax_amp.set_ylim(0, 60)
ax_amp.set_ylabel("amplitude")
ax_amp.set_title("Amplitude per subcarrier")

phase_raw_line,     = ax_phase.plot([], [], lw=1, color='tab:gray',   alpha=0.5, label='raw')
phase_unwrap_line,  = ax_phase.plot([], [], lw=1, color='tab:orange', label='unwrapped')
phase_sanit_line,   = ax_phase.plot([], [], lw=2, color='tab:red',    label='sanitized')
ax_phase.set_ylim(-4, 4)
ax_phase.set_ylabel("phase (rad)")
ax_phase.set_title("Phase per subcarrier")
ax_phase.legend(loc='upper right', fontsize=8)

waterfall = np.zeros((HISTORY, 64))
wf_img = ax_wf.imshow(waterfall, aspect='auto', cmap='viridis',
                      origin='lower', vmin=0, vmax=60)
ax_wf.set_xlabel("subcarrier")
ax_wf.set_ylabel("time →")
ax_wf.set_title("Amplitude waterfall")

plt.tight_layout()
plt.pause(0.001)

# ---- PHASE SANITIZATION ----
def sanitize_phase(phase):
    """
    Remove the linear component (SFO + PDD + CFO) by fitting a line
    across valid subcarrier indices and subtracting it.
    """
    valid = np.ones(len(phase), dtype=bool)
    valid[DEAD_BINS[DEAD_BINS < len(phase)]] = False

    k = np.arange(len(phase))[valid]
    p = phase[valid]

    if len(k) < 4:
        return phase

    # Least-squares fit: p = a*k + b
    A = np.vstack([k, np.ones(len(k))]).T
    a, b = np.linalg.lstsq(A, p, rcond=None)[0]
    return phase - (a * np.arange(len(phase)) + b)

# ---- PARSE ----
def parse(line: str):
    if not line.startswith("CSI,"): return None
    parts = line.strip().split(",")
    if len(parts) < 6: return None
    try:
        ts   = int(parts[1])
        rssi = int(parts[2])
        ch   = int(parts[3])
        ln   = int(parts[4])
        iq   = np.array(parts[5:5+ln], dtype=np.int16)
    except ValueError:
        return None
    if len(iq) < 4: return None
    I = iq[0::2].astype(np.float32)
    Q = iq[1::2].astype(np.float32)
    return ts, rssi, ch, I, Q

# ---- MAIN LOOP ----
last_plot = time.time()
wf = None
prev_unwrapped = None

print("[*] Waiting for CSI stream...")

while True:
    try:
        raw = ser.readline().decode(errors='ignore').strip()
        if not raw:
            continue
        parsed = parse(raw)
        if parsed is None:
            continue

        ts, rssi, ch, I, Q = parsed

        # amplitude
        amp = np.sqrt(I*I + Q*Q)

        # phase (raw, wrapped)
        phase_raw = np.arctan2(Q, I)

        # unwrap across subcarriers (adds 2π continuity)
        phase_unwrapped = np.unwrap(phase_raw)

        # sanitize: remove linear ramp
        phase_sanit = sanitize_phase(phase_unwrapped)

        # Build waterfall on first frame
        if wf is None:
            n_sc = len(amp)
            print(f"[+] First frame: {n_sc} subcarriers, rssi={rssi}, ch={ch}")
            wf = np.zeros((HISTORY, n_sc))
            wf_img.set_data(wf)
            wf_img.set_clim(0, max(20, amp.max()*1.5))
            wf_img.set_extent([0, n_sc, 0, HISTORY])
            ax_wf.set_xlim(0, n_sc)
            ax_wf.set_ylim(0, HISTORY)
            ax_phase.set_xlim(0, n_sc)
            ax_amp.set_xlim(0, n_sc)

        # Update waterfall
        wf = np.roll(wf, 1, axis=0)
        wf[0, :len(amp)] = amp[:wf.shape[1]]
        wf_img.set_data(wf)

        # Plot every 1/PLOT_HZ seconds
        now = time.time()
        if now - last_plot > 1.0 / PLOT_HZ:
            x = np.arange(len(amp))

            amp_line.set_data(x, amp)

            phase_raw_line.set_data(x, phase_raw)
            phase_unwrap_line.set_data(x, phase_unwrapped)
            phase_sanit_line.set_data(x, phase_sanit)

            # Auto-scale phase panel
            lo = min(phase_unwrapped.min(), phase_sanit.min()) - 0.5
            hi = max(phase_unwrapped.max(), phase_sanit.max()) + 0.5
            ax_phase.set_ylim(lo, hi)

            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            last_plot = now

    except KeyboardInterrupt:
        print("\n[*] Exit")
        break
    except Exception as e:
        print(f"[!] {e}")
        time.sleep(0.1)

ser.close()