# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""Interface to the Star Hub hardware.
Translation between python and the spectrum hardware drive.
The Star Hub is a "wire" connecting every card to every card and can therefore be used to start,
stop and synchronise the cards.

- on "same level" as one_card.py
- initialise star hub
- start cards over star hub
- bundling of several calls to a "higher level one"
"""
import logging
import os.path
import time
import dug_seis.acquisition.hardware_driver.regs as regs
import dug_seis.acquisition.hardware_driver.spcerr as spcerr

from ctypes import create_string_buffer, byref

from ctypes import c_int32, cdll

if os.path.isfile("c:\\windows\\system32\\spcm_win64.dll") or os.path.isfile(
        "c:\\windows\\system32\\spcm_win32.dll"):
    from dug_seis.acquisition.hardware_driver.pyspcm import spcm_hOpen, spcm_dwSetParam_i32, spcm_dwGetParam_i32
    from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwGetErrorInfo_i32, spcm_vClose
else:
    pass
    # logging at import messes with the later logging settings, no logging needed here
    # logging.warning('star_hub.py: problems loading the hardware driver. simulation still available.')

if os.name == 'posix':
    try:
        spcmDll = cdll.LoadLibrary("libspcm_linux.so")
        from dug_seis.acquisition.one_card_std_init import init_card as sdt_init_card
        from dug_seis.acquisition.hardware_driver.pyspcm import spcm_hOpen, spcm_dwSetParam_i32, spcm_dwGetParam_i32
        from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwSetParam_i64, spcm_dwGetParam_i64
        from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwGetErrorInfo_i32, spcm_vClose
    except OSError as exception:
        print("linux card driver could not be loaded.")
        print(exception)

logger = logging.getLogger('dug-seis')


class StarHub:
    def __init__(self):

        self.h_sync = None
        self.clock_master_index = None

    def open_sync_handle(self):
        if self.h_sync:
            return 0

        sync_paths = (b'sync0', b'/dev/sync0', b'sync1')
        max_attempts = 3
        for attempt in range(max_attempts):
            for sync_path in sync_paths:
                h_try = spcm_hOpen(create_string_buffer(sync_path))
                logger.info(
                    "star-hub handler for {} (attempt {}/{}): {}".format(
                        sync_path.decode('ascii'), attempt + 1, max_attempts, h_try
                    )
                )
                if h_try:
                    self.h_sync = h_try
                    logger.info("Using star-hub sync handle path: {}".format(sync_path.decode('ascii')))
                    return 0
            time.sleep(0.2)

        logger.error("Could not open star-hub (tried sync0, /dev/sync0, sync1 with retries)...")
        return -1

    def _detect_starhub_carrier(self, card_list):
        i = 0
        for one_card in card_list:
            serial = getattr(one_card, 'serial_number', None)
            has_feature = getattr(one_card, 'has_starhub_feature', None)
            logger.info("checking card nr {0:d} for star hub. card serial:{1} has_starhub_feature:{2}".format(
                i,
                'n/a' if serial is None else serial,
                has_feature,
            ))

            if has_feature is True:
                logger.info("Star hub found on card nr:{}, serial:{}".format(i, serial))
                return i
            i += 1

        logger.warning("No star hub carrier card feature detected while selecting clock master")
        return None

    def init_star_hub(self, card_list, sampling_frequency=None):
        """Initialise the star hub."""
        logger.info("init star hub")

        self.clock_master_index = self._detect_starhub_carrier(card_list)
        if self.clock_master_index is None:
            return -1

        # open handle for star-hub (or reuse pre-opened handle)
        if self.open_sync_handle() == -1:
            return -1

        # check sync count
        l_sync_count = c_int32(0)
        spcm_dwGetParam_i32(self.h_sync, regs.SPC_SYNC_READ_CABLECON0, byref(l_sync_count))
        logger.info("SPC_SYNC_READ_CABLECON0: {}.".format(l_sync_count.value))
        spcm_dwGetParam_i32(self.h_sync, regs.SPC_SYNC_READ_CABLECON1, byref(l_sync_count))
        logger.info("SPC_SYNC_READ_CABLECON1: {}.".format(l_sync_count.value))
        spcm_dwGetParam_i32(self.h_sync, regs.SPC_SYNC_READ_CARDIDX0, byref(l_sync_count))
        logger.info("SPC_SYNC_READ_CARDIDX0: {}.".format(l_sync_count.value))
        spcm_dwGetParam_i32(self.h_sync, regs.SPC_SYNC_READ_NUMCONNECTORS, byref(l_sync_count))
        logger.info("SPC_SYNC_READ_NUMCONNECTORS: {}.".format(l_sync_count.value))
        spcm_dwGetParam_i32(self.h_sync, regs.SPC_SYNC_READ_SYNCCOUNT, byref(l_sync_count))
        logger.info("SPC_SYNC_READ_SYNCCOUNT: {}.".format(l_sync_count.value))

        # setup star-hub
        nr_of_cards = len(card_list)
        dw_error = spcm_dwSetParam_i32(self.h_sync, regs.SPC_SYNC_ENABLEMASK, (1 << nr_of_cards) - 1)
        if dw_error != 0:  # != ERR_OK
            sz_error_text_buffer = create_string_buffer(regs.ERRORTEXTLEN)
            spcm_dwGetErrorInfo_i32(self.h_sync, None, None, sz_error_text_buffer)
            logger.error("Setting setting synchronisation mask to star hub failed. sz_error_text_buffer.value: {0}".format(sz_error_text_buffer.value))
            return -1

        spcm_dwGetParam_i32(self.h_sync, regs.SPC_SYNC_READ_SYNCCOUNT, byref(l_sync_count))
        if l_sync_count.value <= 0:
            logger.error('Star Hub reports zero synchronized cards after enable mask setup')
            return -1

        dw_error = spcm_dwSetParam_i32(self.h_sync, regs.SPC_SYNC_CLKMASK, (1 << self.clock_master_index))
        if dw_error != 0:  # != ERR_OK
            sz_error_text_buffer = create_string_buffer(regs.ERRORTEXTLEN)
            spcm_dwGetErrorInfo_i32(self.h_sync, None, None, sz_error_text_buffer)
            logger.error("Setting setting clock master to star hub failed. sz_error_text_buffer.value: {0}".format(sz_error_text_buffer.value))
            return -1

    def start(self):
        """Start all cards using the star-hub handle."""
        dw_error = spcm_dwSetParam_i32(self.h_sync, regs.SPC_M2CMD,
                                       regs.M2CMD_CARD_START | regs.M2CMD_CARD_ENABLETRIGGER | regs.M2CMD_DATA_STARTDMA)
        if dw_error == spcerr.ERR_OK:
            logger.info("Star hub start: OK")
            return 0

        # ERR_LASTERR means the command was accepted but the result is in the error register.
        # If the last error is ERR_OK, the start actually succeeded.
        sz_error_text_buffer = create_string_buffer(regs.ERRORTEXTLEN)
        spcm_dwGetErrorInfo_i32(self.h_sync, None, None, sz_error_text_buffer)
        if dw_error == spcerr.ERR_LASTERR and b'no error' in sz_error_text_buffer.value.lower():
            logger.info("Star hub start: OK (ERR_LASTERR / no error)")
            return 0

        logger.error("Start of starhub failed (dw_error={}). sz_error_text_buffer.value: {}".format(
            dw_error, sz_error_text_buffer.value))
        return -1

    def close(self):
        """Close the star hub."""
        if self.h_sync is not None:
            spcm_vClose(self.h_sync)
            logger.info("Star hub closed.")
