#!/home/gmf/miniconda3/envs/dug_seis_acquisition/bin/python3
"""
plot_x1_pps.py — Probe and plot the PPS signal on the X1 MMCX pin of a
Spectrum M2p card.

Usage:
    python plot_x1_pps.py [--device /dev/spcm5] [--duration 5] [--no-plot]

What it does:
1. Opens the card (default /dev/spcm5, the StarHub carrier with the X1 MMCX).
2. Reads the available X1 modes and reports them.
3. Sets X1 to SPCM_XMODE_ASYNCIN (asynchronous digital input).
4. Polls SPC_XIO_DIGITALIO bit-1 as fast as possible for --duration seconds.
5. Plots the result: a step plot showing the signal level vs time, with rising
   and falling edge timestamps annotated so you can confirm the PPS polarity.

Run it BEFORE starting the acquisition so the card is not already armed.
"""

import argparse
import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '..', '..', '..', '..'))

import sys
import time
import os

# ---------------------------------------------------------------------------
# Load Spectrum driver
# ---------------------------------------------------------------------------
from ctypes import cdll, c_int32, c_uint64, byref, create_string_buffer, POINTER

try:
    _spcmDll = cdll.LoadLibrary("libspcm_linux.so")
except OSError as e:
    sys.exit("Cannot load libspcm_linux.so: {}".format(e))

from dug_seis.acquisition.hardware_driver.pyspcm import (
    spcm_hOpen,
    spcm_dwSetParam_i32,
    spcm_dwGetParam_i32,
    spcm_dwGetErrorInfo_i32,
    spcm_vClose,
)
import dug_seis.acquisition.hardware_driver.regs as regs
import dug_seis.acquisition.hardware_driver.spcerr as spcerr

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check(rc, h, label):
    if rc != spcerr.ERR_OK:
        buf = create_string_buffer(regs.ERRORTEXTLEN)
        spcm_dwGetErrorInfo_i32(h, None, None, buf)
        print("ERROR  {}: 0x{:04x}  {}".format(label, rc, buf.value.decode(errors='replace')))
        return False
    return True


def probe_x1(device, duration_s):
    h = spcm_hOpen(create_string_buffer(device.encode()))
    if not h:
        sys.exit("Cannot open {}".format(device))
    print("Opened {}".format(device))

    # ---- read card identity ----
    l_type = c_int32()
    l_sn = c_int32()
    spcm_dwGetParam_i32(h, regs.SPC_PCITYP, byref(l_type))
    spcm_dwGetParam_i32(h, regs.SPC_PCISERIALNO, byref(l_sn))
    print("Card type 0x{:08x}  serial {:05d}".format(l_type.value, l_sn.value))

    # ---- query available X1 modes ----
    l_avail = c_int32()
    rc = spcm_dwGetParam_i32(h, regs.SPCM_LEGACY_X1_AVAILMODES, byref(l_avail))
    if rc == spcerr.ERR_OK:
        avail = l_avail.value
        flags = []
        if avail & regs.SPCM_XMODE_ASYNCIN:    flags.append("ASYNCIN")
        if avail & regs.SPCM_XMODE_ASYNCOUT:   flags.append("ASYNCOUT")
        if avail & regs.SPCM_XMODE_DIGIN:      flags.append("DIGIN")
        if avail & regs.SPCM_XMODE_DIGOUT:     flags.append("DIGOUT")
        print("X1 available modes: 0x{:08x}  ({})".format(avail, ", ".join(flags) or "none"))
    else:
        print("WARNING: could not read X1 available modes (0x{:04x})".format(rc))

    # ---- set X1 to async input ----
    rc = spcm_dwSetParam_i32(h, regs.SPCM_LEGACY_X1_MODE, regs.SPCM_XMODE_ASYNCIN)
    if not _check(rc, h, "SPCM_LEGACY_X1_MODE = ASYNCIN"):
        print("  (will try to poll anyway — mode may already be correct)")

    # ---- poll ----
    print("\nPolling X1 (bit 1 of SPCM_XX_ASYNCIO) for {} s ...".format(duration_s))
    print("Press Ctrl-C to stop early.\n")

    times = []
    levels = []
    t0 = time.perf_counter()
    t_end = t0 + duration_s
    l_xio = c_int32()

    try:
        while True:
            now = time.perf_counter()
            if now >= t_end:
                break
            spcm_dwGetParam_i32(h, regs.SPCM_XX_ASYNCIO, byref(l_xio))
            bit1 = (l_xio.value >> 1) & 1   # X1 is bit 1
            times.append(now - t0)
            levels.append(bit1)
    except KeyboardInterrupt:
        pass

    spcm_vClose(h)
    print("Captured {:,} samples in {:.2f} s  (~{:.0f} kHz poll rate)".format(
        len(times), times[-1] if times else 0, len(times) / max(times[-1], 1e-9) / 1000))

    if not times:
        print("No samples captured.")
        return

    # ---- analyse edges ----
    rising  = [times[i] for i in range(1, len(levels)) if levels[i] == 1 and levels[i-1] == 0]
    falling = [times[i] for i in range(1, len(levels)) if levels[i] == 0 and levels[i-1] == 1]

    print("\nEdge summary:")
    print("  Rising  edges: {}".format(len(rising)))
    print("  Falling edges: {}".format(len(falling)))

    if rising:
        intervals = [rising[i+1] - rising[i] for i in range(len(rising)-1)]
        if intervals:
            print("  Rising-edge intervals: min={:.3f}s  max={:.3f}s  mean={:.3f}s".format(
                min(intervals), max(intervals), sum(intervals)/len(intervals)))
    if falling:
        intervals = [falling[i+1] - falling[i] for i in range(len(falling)-1)]
        if intervals:
            print("  Falling-edge intervals: min={:.3f}s  max={:.3f}s  mean={:.3f}s".format(
                min(intervals), max(intervals), sum(intervals)/len(intervals)))

    if not rising and not falling:
        print("\n  *** No edges detected. Signal is stuck at {}.".format(
            "HIGH (1)" if levels[0] else "LOW (0)"))
        print("  Possible causes:")
        print("    - PPS cable not connected or wrong card")
        print("    - GPS receiver not outputting PPS (no fix?)")
        print("    - Signal level too low for the card's input threshold (~1.4 V)")
    else:
        dominant_polarity = "negative (falling-edge)" if len(falling) >= len(rising) else "positive (rising-edge)"
        print("\n  Detected PPS polarity: {}".format(dominant_polarity))
        print("  Set pps_edge_polarity in dug-seis.yaml accordingly.")

    return times, levels, rising, falling


def plot(times, levels, rising, falling, device):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
        import numpy as np
    except ImportError:
        print("\nmatplotlib not available — skipping plot (pip install matplotlib)")
        return

    fig, ax = plt.subplots(figsize=(14, 3))
    ax.step(times, levels, where='post', linewidth=0.8, color='steelblue')
    ax.set_ylim(-0.1, 1.3)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(['LOW', 'HIGH'])
    ax.set_xlabel("Time (s)")
    ax.set_title("X1 MMCX digital input on {}".format(device))

    for t in rising[:20]:
        ax.axvline(t, color='green', linewidth=0.6, alpha=0.7)
    for t in falling[:20]:
        ax.axvline(t, color='red', linewidth=0.6, alpha=0.7)

    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], color='green', linewidth=1.2, label='Rising edge ({})'.format(len(rising))),
        Line2D([0], [0], color='red',   linewidth=1.2, label='Falling edge ({})'.format(len(falling))),
    ]
    ax.legend(handles=legend_elements, loc='upper right')

    ax.xaxis.set_major_locator(ticker.MultipleLocator(0.5))
    ax.xaxis.set_minor_locator(ticker.MultipleLocator(0.1))
    ax.grid(True, which='major', linestyle='--', alpha=0.4)
    fig.tight_layout()

    out = os.path.join(os.path.expanduser("~"), "x1_pps_probe.png")
    fig.savefig(out, dpi=150)
    print("Plot saved to {}".format(out))

    try:
        plt.show()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device",   default="/dev/spcm5",
                        help="Spectrum device to probe (default: /dev/spcm5, the StarHub carrier)")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="How many seconds to poll (default: 5)")
    parser.add_argument("--no-plot",  action="store_true",
                        help="Skip the matplotlib plot")
    args = parser.parse_args()

    result = probe_x1(args.device, args.duration)
    if result is None:
        return

    times, levels, rising, falling = result
    if not args.no_plot:
        plot(times, levels, rising, falling, args.device)


if __name__ == "__main__":
    main()
