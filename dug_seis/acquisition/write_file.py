# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""Finite-duration data recording from Spectrum cards to a compressed NumPy file.

This module can be used in two ways:

  1. As a dug-seis CLI subcommand::

         dug-seis --cfg config/dug-seis.yaml write-file --duration 30

  2. As a standalone script (easy to modify for custom tests)::

         python dug_seis/acquisition/write_file.py --cfg config/dug-seis.yaml --duration 30 --out /tmp

Output is a compressed NumPy .npz file plus a JSON sidecar with metadata.
Data is stored as int16 ADC counts in ascending logical-channel order
(channel 1 = row 0, channel 2 = row 1, etc.).

Memory note: 64 channels x 200 kHz x 60 s x 2 bytes ~ 1.5 GB RAM during assembly.
"""

import json
import logging
import math
import os
import time

import numpy as np
from obspy.core import UTCDateTime

from dug_seis.acquisition.hardware_mockup import SimulatedHardware
from dug_seis.acquisition.one_card import Card
from dug_seis.acquisition.star_hub import StarHub

logger = logging.getLogger('dug-seis')


def run_write_file(param, duration_sec, out_dir):
    """Initialise all Spectrum cards + star hub, record for *duration_sec* seconds,
    write one compressed .npz file and a JSON sidecar to *out_dir*, then stop.

    Args:
        param: Parameter dict loaded from dug-seis.yaml (already validated and
               augmented by _apply_schema_defaults, _validate_schema_lengths,
               _check_if_hardware_needs_to_be_simulated, and _sorted_input_ranges
               — see the __main__ block or cmd_line.py for that call sequence).
        duration_sec: Recording duration in seconds.
        out_dir: Directory to write output files into.
    """
    topology = param['Acquisition']['topology']
    card_count = topology['card_count']
    channels_per_card = topology['channels_per_card']
    total_channels = card_count * channels_per_card
    sync_strategy = topology.get('sync_strategy', 'star_hub')
    sampling_freq = param['Acquisition']['hardware_settings']['sampling_frequency']
    bytes_per_transfer = param['Acquisition']['bytes_per_transfer']
    simulation_mode = param['Acquisition']['simulation_mode']
    reorder_channels = param['Acquisition']['asdf_settings']['reorder_channels']
    sensor_codes_cfg = param['General']['stats']['sensor_codes']
    input_range = param['Acquisition']['hardware_settings']['input_range']

    # --- Hardware setup (mirrors card_manager.run() init sequence) ---
    cards = [Card(param, i) for i in range(card_count)]
    star_hub = StarHub()

    if simulation_mode:
        for card in cards:
            simulated_hardware = SimulatedHardware(param)
            simulated_hardware.mock_card(card)
            simulated_hardware.mock_starhub(star_hub)

    # Close any leftover handles from a previous aborted run
    for card in cards:
        card.close()
    star_hub.close()

    # Three-phase Spectrum driver init: pre_open -> sync handle -> FIFO/DMA.
    # The pre_open/sync phase is skipped in simulation mode because the
    # hardware library is not loaded and sdt_pre_open_card is not importable.
    if sync_strategy == 'star_hub' and not simulation_mode:
        for card in cards:
            card.pre_open(param)
        star_hub.open_sync_handle()

    for card in cards:
        card.init_card(param)
    _log_card_table(cards)

    if sync_strategy == 'star_hub':
        init_attempts = max(1, int(topology.get('star_hub_init_retries', 3)))
        init_ok = False
        for attempt in range(1, init_attempts + 1):
            if star_hub.init_star_hub(cards, sampling_freq) != -1:
                init_ok = True
                break
            logger.warning('Star Hub init attempt {}/{} failed'.format(attempt, init_attempts))
            star_hub.close()
            if attempt < init_attempts:
                time.sleep(0.5)
        if not init_ok:
            for card in cards:
                card.close()
            star_hub.close()
            raise RuntimeError('Star Hub init failed after {} attempts'.format(init_attempts))

        if star_hub.start() == -1:
            for card in cards:
                card.close()
            star_hub.close()
            raise RuntimeError('Star Hub start failed')
    else:
        logger.warning('sync_strategy is none: starting cards individually without star hub')
        for card in cards:
            if card.start_recording() == -1:
                for c in cards:
                    c.close()
                star_hub.close()
                raise RuntimeError('Card {} start failed'.format(card.card_nr))

    # --- Calculate number of chunks to collect ---
    # Each chunk covers bytes_per_transfer bytes:
    #   channels_per_card channels x int16 (2 bytes) x samples_per_chunk
    samples_per_chunk = bytes_per_transfer // (channels_per_card * 2)
    n_chunks = math.ceil(duration_sec * sampling_freq / samples_per_chunk)
    logger.info(
        'write-file: {:.1f}s @ {}Hz -> {} chunks x {} samples/ch = {} total samples/ch'.format(
            duration_sec, sampling_freq, n_chunks, samples_per_chunk, n_chunks * samples_per_chunk)
    )

    # --- Finite collection loop ---
    starttime = UTCDateTime()
    chunks_per_card = [[] for _ in range(card_count)]
    try:
        for chunk_i in range(n_chunks):
            # Poll until all cards have a full chunk ready in the DMA ring buffer
            while min(card.nr_of_bytes_available() for card in cards) < bytes_per_transfer:
                time.sleep(0.001)

            for i, card in enumerate(cards):
                # .copy() detaches the numpy view from the ctypes DMA buffer so
                # the data is not silently overwritten when the ring pointer advances
                chunks_per_card[i].append(card.read_data(bytes_per_transfer, 0).copy())
            for card in cards:
                card.data_has_been_read()

            logger.info('Collected chunk {}/{}'.format(chunk_i + 1, n_chunks))
    finally:
        # Always stop hardware cleanly, even on unexpected errors mid-loop
        if sync_strategy == 'star_hub':
            star_hub.close()
        for card in cards:
            card.close()

    # --- Assemble: stack all cards in physical order -> (total_channels, n_total_samples) ---
    card_arrays = [np.concatenate(chunks, axis=1) for chunks in chunks_per_card]
    raw_data = np.concatenate(card_arrays, axis=0)  # shape: (total_channels, n_total_samples)

    # --- Reorder rows from physical to ascending logical-channel order ---
    # reorder_channels[physical_idx] gives logical_ch (1-indexed), so we build
    # the inverse map: logical_to_physical[logical_ch - 1] = physical_idx
    logical_to_physical = [None] * total_channels
    for physical_idx, logical_ch in enumerate(reorder_channels):
        logical_to_physical[logical_ch - 1] = physical_idx

    ordered_data = raw_data[logical_to_physical, :]      # (total_channels, n_samples), logical order
    # sensor_codes and input_range from the config are already indexed by logical channel (0-based)
    ordered_sensor_codes = list(sensor_codes_cfg)
    ordered_input_ranges = list(input_range)

    # --- Write output files ---
    os.makedirs(out_dir, exist_ok=True)
    # Timestamp string: "20260408T143052" from "2026-04-08T14:30:52.123456Z"
    ts_str = str(starttime).replace('-', '').replace(':', '').split('.')[0].replace('T', 'T')
    stem = os.path.join(out_dir, 'write_file_{}'.format(ts_str))
    npz_path = stem + '.npz'
    json_path = stem + '.json'

    channel_ids = list(range(1, total_channels + 1))  # 1-indexed logical channel numbers
    np.savez_compressed(
        npz_path,
        data=ordered_data.astype(np.int16),
        channel_ids=np.array(channel_ids, dtype=np.int32),
        sensor_codes=np.array(ordered_sensor_codes),
        sampling_frequency=np.array(sampling_freq),
        starttime=np.array(str(starttime)),
    )

    n_active = sum(1 for sc in ordered_sensor_codes if '.NOT.' not in sc)
    n_disabled = total_channels - n_active

    sidecar = {
        'starttime': str(starttime),
        'sampling_frequency_hz': sampling_freq,
        'duration_requested_sec': duration_sec,
        'actual_duration_sec': float(ordered_data.shape[1]) / sampling_freq,
        'n_channels': total_channels,
        'n_samples': ordered_data.shape[1],
        'n_active_channels': n_active,
        'n_disabled_channels': n_disabled,
        'channel_ids': channel_ids,
        'sensor_codes': ordered_sensor_codes,
        'input_ranges_mv': ordered_input_ranges,
        'card_count': card_count,
        'channels_per_card': channels_per_card,
        'bytes_per_transfer': bytes_per_transfer,
    }
    with open(json_path, 'w') as fh:
        json.dump(sidecar, fh, indent=2)

    # --- Terminal summary ---
    actual_duration = float(ordered_data.shape[1]) / sampling_freq
    summary_lines = [
        '',
        'write-file recording complete',
        '  Start time  : {}'.format(starttime),
        '  Duration    : {:.3f} s ({:,} samples/channel)'.format(
            actual_duration, ordered_data.shape[1]),
        '  Cards       : {} ({} ch/card = {} channels total)'.format(
            card_count, channels_per_card, total_channels),
        '  Active ch   : {} / {} ({} disabled)'.format(n_active, total_channels, n_disabled),
        '  Output .npz : {}'.format(npz_path),
        '  Output .json: {}'.format(json_path),
        '',
    ]
    for line in summary_lines:
        logger.info(line)
    print('\n'.join(summary_lines))


def _log_card_table(cards):
    logger.info('Card mapping table:')
    logger.info('{:>6} | {:>10} | {:<16}'.format('index', 'serial', 'device'))
    logger.info('{:->6}-+-{:->10}-+-{:->16}'.format('', '', ''))
    for card in cards:
        serial = getattr(card, 'serial_number', None)
        device = getattr(card, 'device_path', None)
        logger.info('{:>6} | {:>10} | {:<16}'.format(
            card.card_nr,
            'n/a' if serial is None else str(serial),
            'n/a' if device is None else str(device),
        ))


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
# Run directly without needing the full dug-seis CLI:
#
#   python dug_seis/acquisition/write_file.py --cfg config/dug-seis.yaml --duration 30
#   python dug_seis/acquisition/write_file.py --cfg config/dug-seis.yaml --duration 30 --out /tmp/test
#
# Memory note: 64 ch x 200 kHz x 60 s x 2 bytes ~ 1.5 GB RAM during assembly.
#
if __name__ == '__main__':
    import argparse
    import yaml
    from dug_seis.acquisition.acquisition import (
        _apply_schema_defaults,
        _validate_schema_lengths,
        _check_if_hardware_needs_to_be_simulated,
        _sorted_input_ranges,
    )

    parser = argparse.ArgumentParser(
        description=(
            'Record all Spectrum card channels for a fixed duration.\n'
            'Writes a compressed NumPy .npz file and a .json metadata sidecar.\n\n'
            'Memory note: 64 ch x 200 kHz x 60 s x 2 bytes ~ 1.5 GB RAM during assembly.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--cfg', required=True, metavar='<yaml>',
                        help='Path to dug-seis.yaml config file')
    parser.add_argument('--duration', required=True, type=float, metavar='<seconds>',
                        help='Recording duration in seconds')
    parser.add_argument('--out', default='.', metavar='<dir>',
                        help='Output directory (default: current directory)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-7s %(message)s')

    with open(args.cfg) as fh:
        param = yaml.load(fh, Loader=yaml.FullLoader)
    param.setdefault('_meta', {})['config_path'] = os.path.abspath(args.cfg)

    # Mirror the param preparation done in acquisition_.py
    param['Acquisition']['simulation_mode'] = False
    param['Acquisition']['bytes_per_stream_packet'] = 1 * 1024 * 1024
    param['Acquisition']['bytes_per_transfer'] = 32 * 1024 * 1024
    param['Acquisition']['hardware_settings']['ram_buffer_size'] = 128 * 1024 * 1024
    param['Acquisition']['hardware_settings']['timeout'] = 8000
    param['Acquisition']['simulation_amount'] = 0
    _apply_schema_defaults(param)
    _validate_schema_lengths(param)
    _check_if_hardware_needs_to_be_simulated(param)
    param['Acquisition']['hardware_settings']['input_range_sorted'] = _sorted_input_ranges(param)

    run_write_file(param, args.duration, args.out)
