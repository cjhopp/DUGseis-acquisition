"""Plot waveforms and/or spectra from a write_file_*.npz recording.

Usage examples
--------------
# Plot all channels, waveforms only:
    python plot_write_file.py write_file_20260408T143052.npz

# Plot only channels 1-8:
    python plot_write_file.py write_file_20260408T143052.npz --channels 1-8

# Plot specific channels:
    python plot_write_file.py write_file_20260408T143052.npz --channels 1,5,9,13

# Waveforms + FFT side by side:
    python plot_write_file.py write_file_20260408T143052.npz --fft

# Convert to millivolts (uses input_ranges_mv from the .json sidecar):
    python plot_write_file.py write_file_20260408T143052.npz --mv

# Zoom to a time window (seconds from start):
    python plot_write_file.py write_file_20260408T143052.npz --tstart 1.0 --tend 5.0

# Save to file instead of showing interactively:
    python plot_write_file.py write_file_20260408T143052.npz --out plot.png
"""

import argparse
import json
import os
import sys

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker


def load_npz(npz_path):
    npz = np.load(npz_path, allow_pickle=True)
    data = npz['data'].astype(np.float32)          # (n_channels, n_samples)
    channel_ids = npz['channel_ids'].tolist()       # [1, 2, ..., N]
    sensor_codes = npz['sensor_codes'].tolist()
    fs = float(npz['sampling_frequency'])
    starttime = str(npz['starttime'])
    return data, channel_ids, sensor_codes, fs, starttime


def load_json(npz_path):
    json_path = npz_path.replace('.npz', '.json')
    if os.path.exists(json_path):
        with open(json_path) as fh:
            return json.load(fh)
    return None


def parse_channel_arg(arg, channel_ids):
    """Parse '--channels 1-8' or '--channels 1,3,5' into a list of channel ids."""
    ids = set()
    for part in arg.split(','):
        part = part.strip()
        if '-' in part:
            lo, hi = part.split('-')
            ids.update(range(int(lo), int(hi) + 1))
        else:
            ids.add(int(part))
    # Only keep channels that exist in the file
    valid = sorted(i for i in ids if i in channel_ids)
    if not valid:
        print('ERROR: no matching channels found. Available: {}-{}'.format(
            min(channel_ids), max(channel_ids)))
        sys.exit(1)
    return valid


def main():
    parser = argparse.ArgumentParser(
        description='Plot waveforms from a write_file_*.npz recording.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('npz', help='Path to write_file_*.npz file')
    parser.add_argument('--channels', default=None, metavar='SPEC',
                        help='Channels to plot, e.g. "1-8" or "1,5,9,13" (default: all)')
    parser.add_argument('--tstart', type=float, default=None, metavar='SEC',
                        help='Start of time window in seconds from recording start')
    parser.add_argument('--tend', type=float, default=None, metavar='SEC',
                        help='End of time window in seconds from recording start')
    parser.add_argument('--mv', action='store_true',
                        help='Convert ADC counts to millivolts using .json sidecar')
    parser.add_argument('--fft', action='store_true',
                        help='Show FFT spectrum alongside waveforms')
    parser.add_argument('--out', default=None, metavar='FILE',
                        help='Save figure to this file instead of displaying')
    args = parser.parse_args()

    # --- Load data ---
    data, channel_ids, sensor_codes, fs, starttime = load_npz(args.npz)
    meta = load_json(args.npz)
    n_channels, n_samples = data.shape
    t = np.arange(n_samples) / fs  # time axis in seconds

    # --- Channel selection ---
    ch_id_set = set(channel_ids)
    if args.channels:
        selected_ids = parse_channel_arg(args.channels, ch_id_set)
    else:
        selected_ids = channel_ids

    # Filter out disabled channels (sensor code contains '.NOT.')
    active_ids = [ch for ch in selected_ids
                  if '.NOT.' not in sensor_codes[ch - 1]]
    disabled_ids = [ch for ch in selected_ids
                    if '.NOT.' in sensor_codes[ch - 1]]
    if disabled_ids:
        print('Skipping disabled channels: {}'.format(disabled_ids))
    if not active_ids:
        print('No active channels to plot.')
        sys.exit(0)

    # --- Time window ---
    i0 = int(args.tstart * fs) if args.tstart is not None else 0
    i1 = int(args.tend * fs) if args.tend is not None else n_samples
    i0 = max(0, min(i0, n_samples - 1))
    i1 = max(i0 + 1, min(i1, n_samples))
    t_plot = t[i0:i1]

    # --- mV conversion ---
    if args.mv:
        if meta is None:
            print('WARNING: no .json sidecar found, cannot convert to mV. Plotting raw counts.')
            args.mv = False
        else:
            input_ranges_mv = meta['input_ranges_mv']  # indexed by logical ch (0-based)
            y_label = 'Amplitude (mV)'
    if not args.mv:
        y_label = 'ADC counts'

    # --- Decimation for display speed (cap at 50k points per trace) ---
    n_plot = i1 - i0
    decimate_factor = max(1, n_plot // 50000)
    t_plot = t_plot[::decimate_factor]

    # --- Layout ---
    n_rows = len(active_ids)
    n_cols = 2 if args.fft else 1
    fig_height = max(6, n_rows * 0.9)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(14 if args.fft else 12, fig_height),
                             sharex='col' if not args.fft else False,
                             squeeze=False)
    fig.suptitle(
        'write-file recording\n{}\n{}'.format(os.path.basename(args.npz), starttime),
        fontsize=9, y=1.01
    )

    for row, ch_id in enumerate(active_ids):
        ch_idx = ch_id - 1                          # 0-based index into data rows
        code = sensor_codes[ch_idx]
        trace = data[ch_idx, i0:i1][::decimate_factor].copy()

        if args.mv:
            scale = input_ranges_mv[ch_idx] * 2 / 65536
            trace = trace * scale

        ax_w = axes[row, 0]
        ax_w.plot(t_plot, trace, lw=0.5, color='steelblue')
        ax_w.set_ylabel('Ch {:d}\n{}'.format(ch_id, code), fontsize=6, rotation=0,
                        labelpad=60, va='center')
        ax_w.yaxis.set_major_locator(ticker.MaxNLocator(3))
        ax_w.tick_params(labelsize=7)
        ax_w.grid(True, alpha=0.3)

        if args.fft:
            # Full-resolution FFT (no decimation) up to 2^N samples, max 2M
            fft_n = min(n_plot, 2**21)
            fft_data = data[ch_idx, i0:i0 + fft_n]
            if args.mv:
                fft_data = fft_data * scale
            freqs = np.fft.rfftfreq(fft_n, d=1.0 / fs)
            spectrum = np.abs(np.fft.rfft(fft_data)) * 2 / fft_n
            ax_f = axes[row, 1]
            ax_f.semilogy(freqs, spectrum, lw=0.5, color='darkorange')
            ax_f.set_ylabel(y_label, fontsize=6)
            ax_f.tick_params(labelsize=7)
            ax_f.grid(True, alpha=0.3, which='both')
            if row == 0:
                ax_f.set_title('FFT spectrum', fontsize=8)

    # x-axis labels
    axes[-1, 0].set_xlabel('Time (s from recording start)', fontsize=8)
    axes[0, 0].set_title('Waveforms  [{} — {}]'.format(
        args.tstart if args.tstart else '0',
        '{:.3f}s'.format(t[i1 - 1])), fontsize=8)
    if args.fft:
        axes[-1, 1].set_xlabel('Frequency (Hz)', fontsize=8)

    plt.tight_layout()

    if args.out:
        plt.savefig(args.out, dpi=150, bbox_inches='tight')
        print('Saved to {}'.format(args.out))
    else:
        plt.show()


if __name__ == '__main__':
    main()
