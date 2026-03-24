import dug_seis.acquisition.raw_server as raw_server
from dug_seis.acquisition.raw_server import Streamer, Channel
import logging, sys 

def sync_logger_settings(target_name, source_name):
    src = logging.getLogger(source_name)
    dst = logging.getLogger(target_name)
    dst.handlers = src.handlers
    dst.setLevel(src.level)
    dst.propagate = src.propagate
 
sync_logger_settings('raw_api', 'dug-seis')
logger = logging.getLogger('dug-seis')

def create_servers(param):
    sampling_rate = param["Acquisition"]["hardware_settings"]["sampling_frequency"]
    streamers = []
    if "streaming_servers" not in param["Acquisition"]:
        return streamers
    for server in param["Acquisition"]["streaming_servers"]:
        logger.info(f"Starting server: {server}")
        channels = []
        for ch_id in server["channels"]:
            if str(ch_id).isdigit():
                channels.append(Channel(int(ch_id), sampling_rate, sys.byteorder, "int16"))
            else:
                a, b = ch_id.split("-")
                for ch_id in range(int(a), int(b) + 1):
                    channels.append(Channel(ch_id, sampling_rate, sys.byteorder, "int16"))
        streamer = Streamer(channels, host=server["bind_to"], port=server["port"])
        streamers.append(streamer)
    return streamers

def feed_servers(param, streamers, cards_data, data_timestamp, timing_quality=100):
    reorder_channels = param["Acquisition"]["asdf_settings"]["reorder_channels"]
    channels_per_card = param["Acquisition"]["topology"]["channels_per_card"]
    if timing_quality is None:
        timing_cfg = param["Acquisition"].get("timing", {})
        timing_quality = int(timing_cfg.get("timing_quality_fixed_value", 100))
    for card_nr in range(len(cards_data)):
        card_data = cards_data[card_nr]
        num_samps = int(card_data.size / channels_per_card)
        for i in range(channels_per_card):
            samples = card_data[i, 0:num_samps]
            ch_id = reorder_channels[i + channels_per_card * card_nr]
            for streamer in streamers:
                if ch_id in streamer.channels:
                    streamer.feed_data(ch_id, data_timestamp,timing_quality, samples)
