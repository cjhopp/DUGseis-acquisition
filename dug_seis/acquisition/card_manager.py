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
    for card in cards:
        card.init_card(param)
    star_hub.init_star_hub(cards)

    # read xio, for testing purpose, enable inputs in one_card_std_init.py
    # while True:
    #    logger.info("xio l_data, card1: {0:b}, card2: {1:b}".format(card1.read_xio(), card2.read_xio()))
    #    time.sleep(0.1)

    # start
    star_hub.start()
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

    logger.info("Setup complete, waiting for Trigger...")
    while not cards[trigger_card_index].trigger_received():
        pass

    if data_to_asdf:
        data_to_asdf.set_starttime_now()
        stream_ts = copy.copy(data_to_asdf.time_stamps)
    else:
        stream_ts.set_starttime_now()
    bytes_streamed = 0
    t_stream = 0

    logger.info("Acquisition started...")

    try:
        while True:
            #
            # polling scheme here, might not be the best?
            #
            bytes_available = [card.nr_of_bytes_available() for card in cards]
            min_bytes_available = min(bytes_available)

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

                        t_stream += time.perf_counter() - _tref
                else:
                    while min_bytes_available >= bytes_per_stream_packet:
                        _tref = time.perf_counter()

                        cards_data = [card.read_data(bytes_per_stream_packet, 0) for card in cards]
                        streaming.feed_servers(param, servers, cards_data, stream_ts.starttime_UTCDateTime(), timing_quality)
                        stream_ts.set_starttime_next_segment(int(cards_data[0].size / channels_per_card))
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
