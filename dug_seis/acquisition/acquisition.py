# Copyright (c) 2018 by SCCER-SoE and SED at ETHZ
#
# Version 0.0, 23.10.2018, Joseph Doetsch (doetschj)
#              23.10.2018, Thomas Haag (thaag)

"""
Acquisition module of DUG-Seis.
"""

import logging
import dug_seis.acquisition.card_manager as card_manager
import os.path
import glob
import shutil
import socket

from obspy.core import UTCDateTime

logger = logging.getLogger('dug-seis')


def acquisition_(param):
    """
    Acquisition entry point.
    Defines Buffer sizes for: DMA, Stream, and RAM.
    Checks if this script runs on a computer where the Spectrum cards are installed or if they need to be simulated.
    Defines how complex simulated data is.
    Sets the daq_unit name based on the hostname.
    Start writing to the log file and adds the configuration to it.
    Runs the card manger.

    Args:
        param: Parameters which where loaded from dug-seis.yaml when calling "dug-seis acquisition"
    """
    logger.info('Acquisition script started')
    logger.info('==========================')
    # print("logger name: " + logger.name);
    # print("logger level: " + logging.getLevelName(logger.level));

    param['Acquisition']['simulation_mode'] = False     # should be False, or no real data is recorded when True!
    param['Acquisition']['bytes_per_stream_packet'] = 1*1024*1024
    # 32 * 1024 * 1024   # in bytes (amount of data processed per python call)
    param['Acquisition']['bytes_per_transfer'] = 32*1024*1024
    # 128 * 1024 * 1024 # in bytes (computer memory reserved for data)
    param['Acquisition']['hardware_settings']['ram_buffer_size'] = 128*1024*1024
    # ms, when during this time not transfer_size data is available -> timeout
    param['Acquisition']['hardware_settings']['timeout'] = 8000
    # amount of generated data for simulation: 0...4
    # 0 = fastest, only zeroes used, will lead to high compression rate -> small files, low load
    # 4 = slow, all channels with sine, sawtooth and random data filled -> "worst cast data"
    param['Acquisition']['simulation_amount'] = 0
    _apply_schema_defaults(param)
    _validate_schema_lengths(param)

    _check_if_hardware_needs_to_be_simulated(param)

    hostname = socket.gethostname()
    if hostname == 'continuous-01-bedretto':
        param['General']['stats']['daq_unit'] = '01'.zfill(2)
    elif hostname == 'continuous-02-bedretto':
        param['General']['stats']['daq_unit'] = '02'.zfill(2)
    elif hostname == 'continuous-03-bedretto':
        param['General']['stats']['daq_unit'] = '03'.zfill(2)
    elif hostname == 'continuous-04-bedretto':
        param['General']['stats']['daq_unit'] = '04'.zfill(2)
    elif hostname == 'continuous-05-bedretto':
        param['General']['stats']['daq_unit'] = '05'.zfill(2)
    else:
        if param['Acquisition']['simulation_mode'] == True:
            param['General']['stats']['daq_unit'] = '99'.zfill(2)
            logger.info('simulation on host: {}, setting daq_unit to: {}'.format(hostname, param['General']['stats']['daq_unit']))
        else:
            # Mt Terri will probably run here
            param['General']['stats']['daq_unit'] = '98'.zfill(2)
            logger.error('host name not known')

    param['Acquisition']['hardware_settings']['input_range_sorted'] = _sorted_input_ranges(param)

    logger.info('used configuration values (from .yaml file) :')
    _write_used_param_to_log_recursive(param)
    logger.info('additional information, os.name: {0}, os.getcwd(): {1}'.format(os.name, os.getcwd()))
    _copy_config_file(param)
    card_manager.run(param)


def _check_if_hardware_needs_to_be_simulated(param):
    if param['Acquisition']['simulation_mode']:
        logger.warning('param["Acquisition"]["simulation_mode"] = True, this is for testing purposes only.'
                        ' This setting should never be pushed to Git, the real system does only record simulated'
                        ' data this way. A computer without the acquisition hardware will detect the missing hardware'
                        ' and ask to change to the simulation mode automatically.')
    else:
        expected_cards = param['Acquisition']['topology']['card_count']
        if _check_if_hardware_driver_can_be_loaded(expected_cards):
            logger.info('Hardware driver found, running on real hardware')
        else:
            user_input = input("\nCould not load hardware driver, to simulate hardware press: enter or (y)es?")
            if user_input == 'y' or user_input == 'Y' or user_input == 'yes' or user_input == '':
                param['Acquisition']['simulation_mode'] = True
                logger.info('Could not load hardware driver, user requested simulation of hardware.')
            else:
                logger.info('Could not load hardware driver, user abort.')
                return


def _check_if_hardware_driver_can_be_loaded(expected_cards=2):
    if os.name == 'nt':
        if os.path.isfile("c:\\windows\\system32\\spcm_win64.dll") or os.path.isfile(
                "c:\\windows\\system32\\spcm_win32.dll"):
            return True
    if os.name == 'posix':
        logger.info('os.name == posix')
        if os.path.isfile('/proc/spcm_cards'):
            if os.access('/proc/spcm_cards', os.R_OK):
                logger.info('/proc/spcm_cards is accessible')
                file = open('/proc/spcm_cards', 'r')
                found_cards = 0
                for line in file:
                    # print(line.rstrip("\n"))
                    if '/dev/spcm' in line:
                        logger.info(line.rstrip("\n"))
                        found_cards = found_cards + 1
                file.close()
                if found_cards >= expected_cards:
                    return True
                logger.error('Found {} card device entries, expected at least {}'.format(found_cards, expected_cards))
        else:
            # Some Spectrum driver installations do not expose /proc/spcm_cards.
            # Fall back to checking for device nodes directly.
            dev_nodes = sorted(glob.glob('/dev/spcm*'))
            logger.info('/proc/spcm_cards missing, fallback detected {} Spectrum device nodes'.format(len(dev_nodes)))
            for node in dev_nodes:
                logger.info('found device node: {}'.format(node))
            if len(dev_nodes) >= expected_cards:
                return True
            logger.error('Found {} /dev/spcm* device nodes, expected at least {}'.format(len(dev_nodes), expected_cards))
    return False


def _apply_schema_defaults(param):
    acquisition = param.setdefault('Acquisition', {})
    topology = acquisition.setdefault('topology', {})
    output = acquisition.setdefault('output', {})
    timing = acquisition.setdefault('timing', {})
    hardware = acquisition.setdefault('hardware_settings', {})
    channel_map = acquisition.setdefault('channel_map', {})
    asdf_settings = acquisition.setdefault('asdf_settings', {})

    topology.setdefault('card_count', 2)
    topology.setdefault('channels_per_card', 16)
    topology.setdefault('trigger_card_index', 1)
    topology.setdefault('sync_strategy', 'star_hub')
    topology.setdefault('card_device_policy', 'fixed_order')
    topology.setdefault('card_device_map', [])
    topology.setdefault('card_serial_map', [])
    topology.setdefault('device_scan_limit', max(topology['card_count'] * 2, 16))

    output.setdefault('mode', 'both')
    output.setdefault('enable_streaming', True)
    output.setdefault('enable_asdf', True)
    if output['mode'] not in ('both', 'streaming_only', 'asdf_only'):
        raise ValueError('Acquisition.output.mode must be one of both, streaming_only, asdf_only')

    hardware.setdefault('clock_source', 'external_sample_clock' if hardware.get('external_clock', False) else 'intpll')
    hardware.setdefault('reference_clock_hz', int(hardware.get('sampling_frequency', 0)))
    hardware.setdefault('clock_termination_50ohm', True)

    timing.setdefault('timestamp_source', 'system_clock')
    timing.setdefault('timing_quality_source', 'fixed')
    timing.setdefault('timing_quality_fixed_value', 100)

    total_channels = topology['card_count'] * topology['channels_per_card']

    # Keep backward compatibility: use legacy asdf_settings.reorder_channels if provided.
    if 'reorder_channels' not in channel_map:
        legacy_reorder = asdf_settings.get('reorder_channels')
        if legacy_reorder:
            channel_map['reorder_channels'] = legacy_reorder
        elif total_channels == 32 and topology['channels_per_card'] == 16:
            channel_map['reorder_channels'] = [
                1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14, 7, 15, 8, 16,
                17, 25, 18, 26, 19, 27, 20, 28, 21, 29, 22, 30, 23, 31, 24, 32
            ]
        else:
            channel_map['reorder_channels'] = list(range(1, total_channels + 1))

    # Keep existing consumers working while we migrate to channel_map.
    asdf_settings['reorder_channels'] = channel_map['reorder_channels']


def _validate_schema_lengths(param):
    topology = param['Acquisition']['topology']
    total_channels = topology['card_count'] * topology['channels_per_card']
    reorder = param['Acquisition']['channel_map']['reorder_channels']
    sensor_codes = param['General']['stats']['sensor_codes']
    input_range = param['Acquisition']['hardware_settings']['input_range']

    if len(reorder) != total_channels:
        raise ValueError('channel_map.reorder_channels length {} does not match total channels {}'.format(
            len(reorder), total_channels))
    if len(sensor_codes) != total_channels:
        raise ValueError('General.stats.sensor_codes length {} does not match total channels {}'.format(
            len(sensor_codes), total_channels))
    if len(input_range) != total_channels:
        raise ValueError('Acquisition.hardware_settings.input_range length {} does not match total channels {}'.format(
            len(input_range), total_channels))

    trigger_card_index = topology['trigger_card_index']
    if trigger_card_index < 0 or trigger_card_index >= topology['card_count']:
        raise ValueError('topology.trigger_card_index {} is outside [0, {}]'.format(
            trigger_card_index, topology['card_count'] - 1))

    sync_strategy = topology.get('sync_strategy', 'star_hub')
    if sync_strategy not in ('star_hub', 'none'):
        raise ValueError('topology.sync_strategy must be star_hub or none')

    card_device_policy = topology.get('card_device_policy', 'fixed_order')
    if card_device_policy not in ('fixed_order', 'serial_map'):
        raise ValueError('topology.card_device_policy must be fixed_order or serial_map')
    if card_device_policy == 'serial_map' and len(topology.get('card_serial_map', [])) < topology['card_count']:
        raise ValueError('topology.card_serial_map must contain at least card_count entries when using serial_map policy')


def _copy_config_file(param):
    _folder = param['General']['acquisition_folder']
    if _folder[len(_folder) - 1] != "/":
        _folder += "/"
    _folder += 'configs/'
    _time_str = str(UTCDateTime()).replace(":", "_").replace("-", "_")
    _time_str = _time_str.split('.')[0]
    _time_str += '_'
    _folder_file = _folder + _time_str + 'dug-seis.yaml'

    if not os.path.isdir(_folder):
        os.makedirs(_folder)
        logger.info("creating folder: {}".format(_folder))
    cfg_meta = param.get('_meta', {})
    source_cfg = cfg_meta.get('config_path')
    if source_cfg and os.path.isfile(source_cfg):
        logger.info("copying {} to {}".format(source_cfg, _folder_file))
        shutil.copyfile(source_cfg, _folder_file)
    elif os.path.isfile('./dug-seis.yaml'):
        logger.info("copying ./dug-seis.yaml to {}".format(_folder_file))
        shutil.copyfile('./dug-seis.yaml', _folder_file)
    elif os.path.isfile('./config/dug-seis.yaml'):
        logger.info("copying ./config/dug-seis.yaml to {}".format(_folder_file))
        shutil.copyfile('./config/dug-seis.yaml', _folder_file)
    else:
        logger.error("could not find source config file to copy")


def _write_used_param_to_log_recursive(param_dict):
    for key, value in param_dict.items():
        if type(value) == dict:
            # print('next call, key:{}, value:{}'.format(key, value))
            _write_used_param_to_log_recursive(value)
        else:
            # print('{}: {}'.format(key, value))
            logger.info('{}: {}'.format(key, value))


def _sorted_input_ranges(param):
    input_range = param['Acquisition']['hardware_settings']['input_range']
    reorder_channels = param['Acquisition']['asdf_settings']['reorder_channels']
    multiplex_order = [x - 1 for x in reorder_channels]
    input_range_sorted = input_range.copy()
    input_range_sorted[:] = [input_range_sorted[i] for i in multiplex_order]
    # ch_nr = 0
    # for x in reorder_channels:
    #     input_range_sorted[int(x)-1] = (input_range[ch_nr])
    #     # logger.info('x: {}'.format( int(x) ))
    #     ch_nr = ch_nr+1
    return input_range_sorted
