# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""Manages the different hardware components. Calls the data transfer periodically.
- state machine
- restart
- help to hardware problems
- simulation of data
"""
import time
import logging
import copy

from obspy.core import UTCDateTime

from dug_seis.acquisition.one_card import Card
from dug_seis.acquisition.star_hub import StarHub
from dug_seis.acquisition.data_to_asdf import DataToASDF
from dug_seis.acquisition.time_stamps import TimeStamps

from dug_seis.acquisition.hardware_mockup import SimulatedHardware

import dug_seis.acquisition.streaming as streaming

logger = logging.getLogger('dug-seis')


def _log_card_mapping_table(cards):
    logger.info('Card mapping table:')
    logger.info('{:>6} | {:>10} | {:<16}'.format('index', 'serial', 'device'))
    logger.info('{:->6}-+-{:->10}-+-{:->16}'.format('', '', ''))
    for card in cards:
        serial = getattr(card, 'serial_number', None)
        device = getattr(card, 'device_path', None)
        logger.info('{:>6} | {:>10} | {:<16}'.format(
            card.card_nr,
            'n/a' if serial is None else str(serial),
            'n/a' if device is None else str(device)))


def run(param):
    """
    Main acquisition loop, run's until ctrl + c.
    """
    bytes_per_transfer = param['Acquisition']['bytes_per_transfer']
    bytes_per_stream_packet = param['Acquisition']['bytes_per_stream_packet']
    simulation_mode = param['Acquisition']['simulation_mode']
    topology = param['Acquisition']['topology']
    output_cfg = param['Acquisition']['output']
    timing_cfg = param['Acquisition']['timing']

    card_count = topology['card_count']
    trigger_card_index = topology['trigger_card_index']
    channels_per_card = topology['channels_per_card']
    sync_strategy = topology.get('sync_strategy', 'star_hub')
    wait_for_trigger = param['Acquisition']['hardware_settings']['wait_for_trigger']

    mode = output_cfg.get('mode', 'both')
    enable_streaming = output_cfg.get('enable_streaming', True) and mode in ('both', 'streaming_only')
    enable_asdf = output_cfg.get('enable_asdf', True) and mode in ('both', 'asdf_only')
    timing_quality = int(timing_cfg.get('timing_quality_fixed_value', 100))

    # make classes
    cards = [Card(param, i) for i in range(card_count)]
    star_hub = StarHub()

    # simulate hardware if in simulation mode
    if simulation_mode:
        for card in cards:
            simulated_hardware = SimulatedHardware(param)
            simulated_hardware.mock_card(card)
            simulated_hardware.mock_starhub(star_hub)

    # try close, in case the last run was aborted ...
    for card in cards:
        card.close()
    star_hub.close()

    # init setup
    if sync_strategy == 'star_hub':
        # Spectrum example ordering: open ALL card handles first, THEN open sync handle,
        # THEN do FIFO/DMA setup.  The driver requires at least one card handle (especially
        # the carrier) to be open before the sync virtual device becomes accessible.
        for card in cards:
            card.pre_open(param)
        star_hub.open_sync_handle()

    for card in cards:
        card.init_card(param)
    _log_card_mapping_table(cards)
    if sync_strategy == 'star_hub':
        init_attempts = int(topology.get('star_hub_init_retries', 3))
        init_attempts = max(1, init_attempts)
        init_ok = False
        sampling_frequency = param['Acquisition']['hardware_settings']['sampling_frequency']
        for attempt in range(1, init_attempts + 1):
            if star_hub.init_star_hub(cards, sampling_frequency) != -1:
                init_ok = True
                break
            logger.warning('Star Hub initialization attempt {}/{} failed'.format(attempt, init_attempts))
            star_hub.close()
            if attempt < init_attempts:
                time.sleep(0.5)

        if not init_ok:
            for card in cards:
                card.close()
            star_hub.close()
            raise RuntimeError('Star Hub initialization failed after {} attempts. Acquisition aborted to avoid running with no synchronized data.'.format(init_attempts))

        if star_hub.clock_master_index is not None and trigger_card_index != star_hub.clock_master_index:
            logger.warning(
                'Configured trigger_card_index ({}) differs from Star Hub clock master index ({}). '
                'This may be intentional, but verify your timing/trigger topology.'.format(
                    trigger_card_index, star_hub.clock_master_index))
    else:
        logger.warning('topology.sync_strategy is set to none: cards will start without Star Hub synchronization')

    # read xio, for testing purpose, enable inputs in one_card_std_init.py
    # while True:
    #    logger.info("xio l_data, card1: {0:b}, card2: {1:b}".format(card1.read_xio(), card2.read_xio()))
    #    time.sleep(0.1)

    # start
    if sync_strategy == 'star_hub':
        if star_hub.start() == -1:
            for card in cards:
                card.close()
            star_hub.close()
            raise RuntimeError('Star Hub start failed. Acquisition aborted to avoid streaming with no payload.')
    else:
        for card in cards:
            if card.start_recording() == -1:
                for c in cards:
                    c.close()
                star_hub.close()
                raise RuntimeError('Card {} start failed while sync_strategy is none'.format(card.card_nr))
    data_to_asdf = DataToASDF(param) if enable_asdf else None
    stream_ts = TimeStamps(param)
    if data_to_asdf and data_to_asdf.error:
        logger.error("an error occurred, closing cards.")
        for card in cards:
            card.close()
        star_hub.close()
        exit(1)

    #
    # start the data streaming servers
    #
    servers = streaming.create_servers(param) if enable_streaming else []
    for server in servers:
        server.start()

    # wait?
    # card1.wait_for_data()
    # card2.wait_for_data()

    # read status, no actions planned at the moment
    # the read status function will print() if there is a problem ...
    for card in cards:
        card.read_status()

    time_stamp_this_loop = time.perf_counter()

    if wait_for_trigger:
        logger.info("Setup complete, waiting for Trigger...")
        while not cards[trigger_card_index].trigger_received():
            pass
    else:
        logger.info("Setup complete, trigger wait disabled.")

    if data_to_asdf:
        data_to_asdf.set_starttime_now()
        stream_ts = copy.copy(data_to_asdf.time_stamps)
    else:
        stream_ts.set_starttime_now()
    bytes_streamed = 0
    packets_sent = 0
    t_stream = 0

    logger.info("Acquisition started...")

    # Streaming diagnostics: emit a compact status line at most once per second
    # to show whether any card is preventing the min-bytes gate from opening.
    last_stream_diag_log = 0.0

    try:
        while True:
            #
            # polling scheme here, might not be the best?
            #
            bytes_available = [card.nr_of_bytes_available() for card in cards]
            min_bytes_available = min(bytes_available)

            now_diag = time.perf_counter()
            if now_diag - last_stream_diag_log >= 1.0:
                logger.info(
                    "stream-diag bytes_available={} min={} threshold={} packets_sent={} stream_ts={}"
                    .format(bytes_available, min_bytes_available, bytes_per_stream_packet,
                            packets_sent, stream_ts.starttime_UTCDateTime())
                )
                last_stream_diag_log = now_diag

            #
            # handle streaming: send data packets until all the available bytes
            # have been consumed or less than bytes_per_stream_packet are left
            #
            if enable_streaming:
                if enable_asdf:
                    while min_bytes_available >= bytes_streamed + bytes_per_stream_packet:
                        _tref = time.perf_counter()

                        cards_data = [card.read_data(bytes_per_stream_packet, bytes_streamed) for card in cards]
                        streaming.feed_servers(param, servers, cards_data, stream_ts.starttime_UTCDateTime(), timing_quality)
                        stream_ts.set_starttime_next_segment(int(cards_data[0].size / channels_per_card))
                        bytes_streamed += bytes_per_stream_packet
                        packets_sent += 1

                        t_stream += time.perf_counter() - _tref
                else:
                    while min_bytes_available >= bytes_per_stream_packet:
                        _tref = time.perf_counter()

                        cards_data = [card.read_data(bytes_per_stream_packet, 0) for card in cards]
                        streaming.feed_servers(param, servers, cards_data, stream_ts.starttime_UTCDateTime(), timing_quality)
                        stream_ts.set_starttime_next_segment(int(cards_data[0].size / channels_per_card))
                        packets_sent += 1
                        for card in cards:
                            card.data_has_been_read(bytes_per_stream_packet)
                        min_bytes_available -= bytes_per_stream_packet

                        t_stream += time.perf_counter() - _tref

            #
            # handle file generation: create files when enough data is available
            #
            if enable_asdf and min_bytes_available >= bytes_per_transfer:

                #
                # Log system vs data time
                #
                logger.info("Data time {} sys/data time difference: {} sec".format(
                    stream_ts.starttime_UTCDateTime(), UTCDateTime()-stream_ts.starttime_UTCDateTime()))

                _tref = time.perf_counter()
                cards[0].read_status()     # writes overrun error to logger.error
                data_to_asdf.data_to_asdf([card.read_data(bytes_per_transfer, 0) for card in cards])
                for card in cards:
                    card.data_has_been_read()

                #
                # streaming time sync due to sample dropping logic in data_to_asdf
                # this will cause the next sample to have the same timestamp as the
                # last sent sample. The software downstram will decide how to handle
                # this
                #
                bytes_streamed -= bytes_per_transfer
                if bytes_streamed < 0:
                    bytes_streamed = 0
                if bytes_streamed == 0: # streamed data and data writted to asdf files is the same amount
                    if enable_streaming and stream_ts.starttime_UTCDateTime() != data_to_asdf.time_stamps.starttime_UTCDateTime():
                        stream_ts = copy.copy(data_to_asdf.time_stamps) # align timestamps with asdf
                        logger.info("Aligned streaming timestamps with asdf files")

                now = time.perf_counter()
                t_asdf = now - _tref
                t_loop = now - time_stamp_this_loop
                logger.info("Loop took: {:.2f} sec (asdf {:.2f} + stream {:.2f} -> {}%)"
                            .format(t_loop, t_asdf, t_stream, int((t_asdf + t_stream)/t_loop * 100)))
                t_stream = 0
                time_stamp_this_loop = now
            else:
                time.sleep(0.1)

    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt detected, exiting...")
    finally:
        for card in cards:
            card.close()
        star_hub.close()
        for server in servers:
            server.stop()
