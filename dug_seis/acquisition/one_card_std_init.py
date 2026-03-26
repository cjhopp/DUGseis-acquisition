# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""
The initialisation of the acquisition card.
"""
import logging
import dug_seis.acquisition.hardware_driver.regs as regs
import dug_seis.acquisition.hardware_driver.spcerr as spcerr

from dug_seis.acquisition.hardware_driver.pyspcm import spcm_hOpen, spcm_dwGetParam_i32
from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwSetParam_i32, spcm_dwSetParam_i64, spcm_dwGetContBuf_i64
from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwDefTransfer_i64
from dug_seis.acquisition.hardware_driver.pyspcm import spcm_vClose, spcm_dwGetErrorInfo_i32
from dug_seis.acquisition.hardware_driver.pyspcm import SPCM_BUF_DATA, SPCM_DIR_CARDTOPC

from ctypes import c_int32, c_uint64
from ctypes import create_string_buffer, byref, c_void_p

logger = logging.getLogger('dug-seis')


def _to_int_serial(serial_value):
    if isinstance(serial_value, int):
        return serial_value
    if isinstance(serial_value, str):
        serial_value = serial_value.strip()
        if not serial_value:
            raise ValueError('Empty serial string is not allowed')
        return int(serial_value)
    raise ValueError('Unsupported serial type: {}'.format(type(serial_value)))


def _candidate_device_paths(topology):
    card_count = topology['card_count']
    device_scan_limit = int(topology.get('device_scan_limit', max(card_count * 2, 16)))
    configured = topology.get('card_device_map', [])
    candidates = []

    for path in configured:
        if path not in candidates:
            candidates.append(path)

    for i in range(device_scan_limit):
        path = '/dev/spcm{}'.format(i)
        if path not in candidates:
            candidates.append(path)

    return candidates


def _open_card_by_serial(topology, target_serial, card_nr):
    seen = []
    for device_path in _candidate_device_paths(topology):
        h_try = spcm_hOpen(create_string_buffer(device_path.encode('ascii')))
        if not h_try:
            continue

        serial = c_int32(0)
        spcm_dwGetParam_i32(h_try, regs.SPC_PCISERIALNO, byref(serial))
        seen.append((device_path, serial.value))
        if serial.value == target_serial:
            logger.info('Mapped card {} serial {} to {}'.format(card_nr, target_serial, device_path))
            return h_try, device_path

        spcm_vClose(h_try)

    logger.error('Could not map card {} serial {}. Seen devices/serials: {}'.format(card_nr, target_serial, seen))
    return None, None


def sz_type_to_name(l_card_type):
    """sz_type_to_name: doing name translation."""

    l_version = (l_card_type & regs.TYP_VERSIONMASK)
    if (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M2ISERIES:
        s_name = 'M2i.%04x' % l_version
    elif (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M2IEXPSERIES:
        s_name = 'M2i.%04x-Exp' % l_version
    elif (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M3ISERIES:
        s_name = 'M3i.%04x' % l_version
    elif (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M3IEXPSERIES:
        s_name = 'M3i.%04x-Exp' % l_version
    elif (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M4IEXPSERIES:
        s_name = 'M4i.%04x-x8' % l_version
    elif (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M4XEXPSERIES:
        s_name = 'M4x.%04x-x4' % l_version
    elif (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M2PEXPSERIES:
        s_name = 'M2p.%04x-x4' % l_version
    elif hasattr(regs, 'TYP_M5IEXPSERIES') and (l_card_type & regs.TYP_SERIESMASK) == regs.TYP_M5IEXPSERIES:
        s_name = 'M5i.%04x-x16' % l_version
    else:
        s_name = 'unknown type'
    return s_name


def pre_open_card(param, card_nr):
    """Open card handle and read basic identity info without doing any FIFO/DMA setup.

    Returns (h_card, device_path, serial_number, has_starhub_feature) on success,
    or -1 on failure.  Call this for all cards before open_sync_handle(), then call
    init_card() (passing the returned h_card) to complete FIFO configuration.
    """
    topology = param['Acquisition']['topology']
    card_device_policy = topology.get('card_device_policy', 'fixed_order')

    if card_device_policy == 'serial_map':
        serial_map = topology.get('card_serial_map', [])
        if card_nr >= len(serial_map):
            logger.error('pre_open_card: card_serial_map missing entry for card {} (len={})'.format(card_nr, len(serial_map)))
            return -1
        target_serial = _to_int_serial(serial_map[card_nr])
        h_card, device_path = _open_card_by_serial(topology, target_serial, card_nr)
        if not h_card:
            return -1
    else:
        device_map = topology.get('card_device_map', [])
        device_path = device_map[card_nr] if card_nr < len(device_map) else '/dev/spcm{}'.format(card_nr)
        h_card = spcm_hOpen(create_string_buffer(device_path.encode('ascii')))

    logger.info('pre-open card {} at {}: {}'.format(card_nr, device_path, 'OK' if h_card else 'FAIL'))
    if not h_card:
        logger.error('pre_open_card: card {} not found at {}'.format(card_nr, device_path))
        return -1

    l_serial_number = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_PCISERIALNO, byref(l_serial_number))
    l_features = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_PCIFEATURES, byref(l_features))
    has_starhub_feature = bool(l_features.value & (regs.SPCM_FEAT_STARHUB5 | regs.SPCM_FEAT_STARHUB16))

    return h_card, device_path, l_serial_number.value, has_starhub_feature


def init_card(param, card_nr, h_card=None, device_path=None):
    """Initialise card. Setup card parameters. Reserve buffers for DMA data transfer.

    If h_card is provided (pre-opened via pre_open_card()), the open step is skipped
    and the existing handle is used directly.  This allows open_sync_handle() to be
    called between the open and FIFO-setup phases, matching the Spectrum example ordering.
    """
    logger.debug("Initializing card {} sdt_init...".format(card_nr))

    sampling_frequency = param['Acquisition']['hardware_settings']['sampling_frequency']
    qw_buffer_size = c_uint64(param['Acquisition']['hardware_settings']['ram_buffer_size'])
    l_notify_size_stream = c_int32(param['Acquisition']['bytes_per_stream_packet'])
    timeout = param['Acquisition']['hardware_settings']['timeout']
    wait_for_trigger = param['Acquisition']['hardware_settings']['wait_for_trigger']
    hardware_settings = param['Acquisition']['hardware_settings']
    topology = param['Acquisition']['topology']
    channels_per_card = topology['channels_per_card']
    trigger_card_index = topology['trigger_card_index']
    sync_strategy = topology.get('sync_strategy', 'star_hub')
    card_device_policy = topology.get('card_device_policy', 'fixed_order')
    clock_source = hardware_settings.get('clock_source')
    reference_clock_hz = hardware_settings.get('reference_clock_hz', sampling_frequency)
    clock_termination_50ohm = 1 if hardware_settings.get('clock_termination_50ohm', True) else 0

    # Backward compatibility with legacy boolean key.
    if clock_source is None:
        clock_source = 'external_sample_clock' if param['Acquisition']['hardware_settings'].get('external_clock', False) else 'intpll'

    input_range_sorted = param['Acquisition']['hardware_settings']['input_range_sorted']

    """ open card """
    if h_card is not None:
        # Reuse a handle that was pre-opened before open_sync_handle().
        logger.info('card {} reusing pre-opened handle at {}'.format(card_nr, device_path))
    elif card_device_policy == 'serial_map':
        serial_map = topology.get('card_serial_map', [])
        if card_nr >= len(serial_map):
            logger.error('card_serial_map missing entry for card {} (len={})'.format(card_nr, len(serial_map)))
            return -1
        target_serial = _to_int_serial(serial_map[card_nr])
        h_card, device_path = _open_card_by_serial(topology, target_serial, card_nr)
        if not h_card:
            return -1
    else:
        device_map = topology.get('card_device_map', [])
        if card_nr < len(device_map):
            device_path = device_map[card_nr]
        else:
            device_path = '/dev/spcm{}'.format(card_nr)
        h_card = spcm_hOpen(create_string_buffer(device_path.encode('ascii')))
        logger.info('card {} opening device {}'.format(card_nr, device_path))

    start = card_nr * channels_per_card
    end = (card_nr + 1) * channels_per_card
    input_range_this_card = input_range_sorted[start:end]
    if not h_card:
        logger.error("card {} not found...".format(card_nr))
        return -1
        # exit ()

    if len(input_range_this_card) != channels_per_card:
        logger.error(
            "input_range_sorted slice length {} does not match channels_per_card {} for card {}".format(
                len(input_range_this_card), channels_per_card, card_nr))
        return -1

    # read type, function and sn and check for A/D card
    l_card_type = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_PCITYP, byref(l_card_type))
    l_serial_number = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_PCISERIALNO, byref(l_serial_number))
    l_fnc_type = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_FNCTYPE, byref(l_fnc_type))
    l_features = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_PCIFEATURES, byref(l_features))
    has_starhub_feature = bool(l_features.value & (regs.SPCM_FEAT_STARHUB5 | regs.SPCM_FEAT_STARHUB16))
    l_num_modules = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_MIINST_MODULES, byref(l_num_modules))
    l_num_ch_per_module = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_MIINST_CHPERMODULE, byref(l_num_ch_per_module))
    l_num_ch_on_card = l_num_modules.value * l_num_ch_per_module.value

    s_card_name = sz_type_to_name(l_card_type.value)
    if l_fnc_type.value == regs.SPCM_TYPE_AI:
        logger.info("Found: {0} sn {1:05d}".format(s_card_name, l_serial_number.value))
    else:
        logger.error("Card: {0} sn {1:05d} not supported by example".format(s_card_name, l_serial_number.value))
        return -1

    if l_num_ch_on_card != channels_per_card:
        logger.error(
            "Card {} reports {} channels ({} modules x {}), but topology.channels_per_card is {}".format(
                card_nr, l_num_ch_on_card, l_num_modules.value, l_num_ch_per_module.value, channels_per_card))
        return -1

    """ do a simple FIFO setup """

    # all channels enabled (must be 1, 2, 4, 8, 16)
    channel_enable_mask = (1 << channels_per_card) - 1
    spcm_dwSetParam_i32(h_card, regs.SPC_CHENABLE,       channel_enable_mask)

    # Pre-trigger samples at start of FIFO mode (must be reduced with more channels, see manual for limits)
    # For 8-channel M2p.5913-x4 cards in FIFO mode, must use 0
    pretrigger_samples = 0
    dw_error = spcm_dwSetParam_i32(h_card, regs.SPC_PRETRIGGER, pretrigger_samples)
    if dw_error != spcerr.ERR_OK:
        sz_error_text_buffer = create_string_buffer(regs.ERRORTEXTLEN)
        spcm_dwGetErrorInfo_i32(h_card, None, None, sz_error_text_buffer)
        logger.warning("card {}: SetParam SPC_PRETRIGGER={} error: {}".format(
            card_nr, pretrigger_samples, sz_error_text_buffer.value))

    # single FIFO mode
    spcm_dwSetParam_i32(h_card, regs.SPC_CARDMODE,       regs.SPC_REC_FIFO_SINGLE)

    # timeout im ms (e.g. 8 sec)
    spcm_dwSetParam_i32(h_card, regs.SPC_TIMEOUT,        timeout)

    # xio setup
    # if card_nr == 1:
    #    spcm_dwSetParam_i32(h_card, regs.SPC_XIO_DIRECTION, regs.XD_CH0_INPUT | regs.XD_CH2_INPUT)
    #    spcm_dwSetParam_i32(h_card, regs.SPC_XIO_DIGITALIO, 0x00)

    # trigger set to software, card will trigger immediately after start
    # spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ORMASK,    regs.SPC_TMASK_SOFTWARE)

    # Trigger setup depends on whether cards are started through a Star Hub or independently.
    if sync_strategy == 'star_hub':
        if card_nr == trigger_card_index:
            if wait_for_trigger:
                spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_EXT0_MODE, regs.SPC_TM_POS)
                spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_TERM, 1)  # Enables the 50 Ohm input termination
                spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ORMASK, regs.SPC_TMASK_EXT0)
            else:
                spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ORMASK, regs.SPC_TMASK_SOFTWARE)
        else:
            spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ORMASK, regs.SPC_TMASK_NONE)
    else:
        if wait_for_trigger and card_nr == trigger_card_index:
            spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_EXT0_MODE, regs.SPC_TM_POS)
            spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_TERM, 1)
            spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ORMASK, regs.SPC_TMASK_EXT0)
        else:
            spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ORMASK, regs.SPC_TMASK_SOFTWARE)

    spcm_dwSetParam_i32(h_card, regs.SPC_TRIG_ANDMASK,   0)

    # clock mode
    if clock_source == 'external_sample_clock':
        spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKMODE, regs.SPC_CM_EXTERNAL)
        spcm_dwSetParam_i32(h_card, regs.SPC_CLOCK50OHM, clock_termination_50ohm)
    elif clock_source == 'external_reference_clock':
        spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKMODE, regs.SPC_CM_EXTREFCLOCK)
        spcm_dwSetParam_i32(h_card, regs.SPC_REFERENCECLOCK, int(reference_clock_hz))
    else:
        spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKMODE, regs.SPC_CM_INTPLL)

    # clock mode external
    # spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKMODE, regs.SPC_CM_EXTREFCLOCK)
    # spcm_dwSetParam_i32(h_card, regs.SPC_REFERENCECLOCK, sampling_frequency)

    # spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKMODE, regs.SPC_CM_EXTERNAL)

#    spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKMODE, regs.SPC_CM_EXTERNAL)
    # spcm_dwSetParam_i32(h_card, regs.SPC_EXTERNALCLOCK, 1)

    # set sample rate (SPC_SAMPLERATE is a 64-bit register — use i64 variant)
    spcm_dwSetParam_i64(h_card, regs.SPC_SAMPLERATE, sampling_frequency)
    logger.info("using: {0} sps".format(sampling_frequency))

    # no clock output
    spcm_dwSetParam_i32(h_card, regs.SPC_CLOCKOUT, 0)

    # read available ranges
    range_min = c_int32(0)
    range_max = c_int32(0)
    l_number_of_ranges = c_int32(0)
    spcm_dwGetParam_i32(h_card, regs.SPC_READIRCOUNT, byref(l_number_of_ranges))
    logger.debug("card {}: nr of available ranges: {}".format(card_nr, l_number_of_ranges.value))

    for i in range(l_number_of_ranges.value):
        spcm_dwGetParam_i32(h_card, regs.SPC_READRANGEMIN0 + i, byref(range_min))
        spcm_dwGetParam_i32(h_card, regs.SPC_READRANGEMAX0 + i, byref(range_max))
        logger.debug("card {}, range nr {}: {}mV to {}mV".format(card_nr, i, range_min.value, range_max.value))

    # set input range 50, 100, 250, 500, 1000, 2000, 5000, 10000 mV
    selected_range = c_int32(0)
    for i in range(channels_per_card):
        dw_error = spcm_dwSetParam_i32(h_card, regs.SPC_AMP0 + i * 100, input_range_this_card[i])
        if dw_error != spcerr.ERR_OK:
            sz_error_text_buffer = create_string_buffer(regs.ERRORTEXTLEN)
            spcm_dwGetErrorInfo_i32(h_card, None, None, sz_error_text_buffer)
            logger.warning("card {}, channel {}: SetParam error 0x{:04x}, detail: {}".format(
                card_nr, i, dw_error, sz_error_text_buffer.value))

        spcm_dwGetParam_i32(h_card, regs.SPC_AMP0 + i * 100, byref(selected_range))
        logger.info("card {}, channel {} requested range: {}mV, selected range: {}mV".format(
            card_nr, i, input_range_this_card[i], selected_range.value))

    """ define the data buffer """
    # we try to use continuous memory if available and big enough
    pv_buffer = c_void_p()
    qw_cont_buf_len = c_uint64(0)
    spcm_dwGetContBuf_i64(h_card, SPCM_BUF_DATA, byref(pv_buffer),
                          byref(qw_cont_buf_len))
    logger.debug("ContBuf length: {0:d}".format(qw_cont_buf_len.value))

    if qw_cont_buf_len.value >= qw_buffer_size.value:
        logger.info("Using continuous buffer")
    else:
        pv_buffer = create_string_buffer(qw_buffer_size.value)
        logger.info("Using buffer allocated by user program")

    spcm_dwDefTransfer_i64(h_card, SPCM_BUF_DATA, SPCM_DIR_CARDTOPC, l_notify_size_stream.value, pv_buffer, c_uint64(0),
                           qw_buffer_size)

    return h_card, pv_buffer, device_path, l_serial_number.value, has_starhub_feature
