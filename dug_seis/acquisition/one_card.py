# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""Representation of one data acquisition card. Translation between python and the spectrum hardware drive.
- making function names understandable
- translation to hardware calls
- bundling of several calls to a "higher level one"
- hiding card pointers and management
"""

import logging

import numpy as np
import os.path

import dug_seis.acquisition.hardware_driver.regs as regs
import dug_seis.acquisition.hardware_driver.spcerr as err

from ctypes import byref, c_int32, c_int64, POINTER, c_int16, cast, addressof, cdll

if os.path.isfile("c:\\windows\\system32\\spcm_win64.dll") or os.path.isfile(
        "c:\\windows\\system32\\spcm_win32.dll"):
    from dug_seis.acquisition.one_card_std_init import init_card as sdt_init_card, pre_open_card as sdt_pre_open_card
    from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwSetParam_i32, spcm_dwGetParam_i32, spcm_dwGetParam_i64, spcm_vClose, spcm_dwGetErrorInfo_i32
else:
    pass
    # logging at import messes with the later logging settings, no logging needed here
    # logger.warning('one_card.py: problems loading the hardware driver. simulation still available.')

if os.name == 'posix':
    try:
        spcmDll = cdll.LoadLibrary("libspcm_linux.so")
        from dug_seis.acquisition.one_card_std_init import init_card as sdt_init_card, pre_open_card as sdt_pre_open_card
        from dug_seis.acquisition.hardware_driver.pyspcm import spcm_dwSetParam_i32, spcm_dwGetParam_i32, spcm_dwGetParam_i64, spcm_vClose, spcm_dwGetErrorInfo_i32
    except OSError as exception:
        print("linux card driver could not be loaded.")
        print(exception)

logger = logging.getLogger('dug-seis')


class Card:

    def __init__(self, param, card_nr):

        # l_notify_size = c_int32(regs.KILO_B(2 * 1024))
        self.l_notify_size = c_int32(param['Acquisition']['bytes_per_transfer'])
        self.ram_buffer_size = param['Acquisition']['hardware_settings']['ram_buffer_size']
        self.card_nr = card_nr
        self.h_card = None
        self.device_path = None
        self.serial_number = None
        self.has_starhub_feature = None
        self.channels_per_card = param['Acquisition']['topology']['channels_per_card']
        self.card_count = param['Acquisition']['topology']['card_count']

        self.debug_buffer_behaviour = False

        self._pv_buffer = None
        # nr of channels & 16 bit = 2 bytes
        # self._nr_of_datapoints = floor(param['Acquisition']['bytes_per_transfer'] / 16 / 2)

        input_range_sorted = param['Acquisition']['hardware_settings']['input_range_sorted']
        start = card_nr * self.channels_per_card
        end = (card_nr + 1) * self.channels_per_card
        self.scaling_this_card = [i * 2 / 65536 for i in input_range_sorted[start:end]]

    def pre_open(self, param):
        """Open card handle and read identity info without FIFO/DMA setup.

        Must be called for all cards before StarHub.open_sync_handle(), after which
        init_card() will complete the FIFO/DMA configuration using the open handle.
        This matches the Spectrum example's init ordering.
        """
        logger.info("pre-open card: {}".format(self.card_nr))
        if 0 <= self.card_nr < self.card_count:
            result = sdt_pre_open_card(param, self.card_nr)
            if result == -1:
                raise RuntimeError("Card {} pre-open failed".format(self.card_nr))
            self.h_card, self.device_path, self.serial_number, self.has_starhub_feature = result
        else:
            logger.error("card_nr needs to be in [0, {}], received:{}".format(self.card_count - 1, self.card_nr))

    def init_card(self, param):
        """Initialise card. Setup card parameters. Reserve buffers for DMA data transfer."""
        logger.info("init card: {}".format(self.card_nr))
        if 0 <= self.card_nr < self.card_count:
            # Pass pre-opened handle if available (from pre_open()), so sync can be opened
            # between the open and FIFO-setup phases (Spectrum example ordering).
            result = sdt_init_card(param, self.card_nr,
                                   h_card=self.h_card, device_path=self.device_path)
            if result == -1:
                raise RuntimeError("Card {} initialization failed".format(self.card_nr))
            if isinstance(result, tuple) and len(result) == 5:
                self.h_card, self._pv_buffer, self.device_path, self.serial_number, self.has_starhub_feature = result
            elif isinstance(result, tuple) and len(result) == 4:
                self.h_card, self._pv_buffer, self.device_path, self.serial_number = result
            elif isinstance(result, tuple) and len(result) == 2:
                self.h_card, self._pv_buffer = result
            else:
                raise RuntimeError(
                    "Unexpected init_card() return for card {}: {}".format(self.card_nr, type(result)))
        else:
            logger.error("card_nr needs to be in [0, {}], received:{}".format(self.card_count - 1, self.card_nr))

    def print_settings(self):
        """print selected voltage range."""
        selected_range = c_int32(0)
        spcm_dwGetParam_i32(self.h_card, regs.SPC_AMP0, byref(selected_range))
        logger.info("selectedRange: +- {0:.3f} mV\n".format(selected_range.value))

    def wait_for_data(self):
        """Wait for a data package(l_notify_size) to be ready.
        Timeout after SPC_TIMEOUT, if data in not ready. (defined in one_card_sdt_init.py).
        param['Acquisition']['hardware_settings']['timeout']"""

        dw_error = spcm_dwSetParam_i32(self.h_card, regs.SPC_M2CMD, regs.M2CMD_DATA_WAITDMA)
        if dw_error != err.ERR_OK:
            if dw_error == err.ERR_TIMEOUT:
                logger.error("{0} ... Timeout".format(self.card_nr))
            else:
                logger.error("{0} ... Error: {1:d}".format(self.card_nr, dw_error))

    def read_status(self):
        """Read the status of the card. SPC_M2STATUS."""
        l_status = c_int32()
        spcm_dwGetParam_i32(self.h_card, regs.SPC_M2STATUS, byref(l_status))

        if regs.M2STAT_DATA_OVERRUN & l_status.value:
            logger.error("card {} overrun or underrun detected: M2STAT_DATA_OVERRUN".format(self.card_nr))
        return l_status.value

    def trigger_received(self):
        """Returns true once the card received the first trigger."""
        if self.read_status() & regs.M2STAT_CARD_TRIGGER:  # The first trigger has been detected.
            return True
        return False

    def read_xio(self):
        """Read the digital IO's."""
        l_data = c_int32()
        spcm_dwGetParam_i32(self.h_card, regs.SPC_XIO_DIGITALIO, byref(l_data))
        return l_data.value

    def nr_of_bytes_available(self):
        """Get amount of available data."""
        l_avail_user = c_int32()
        spcm_dwGetParam_i32(self.h_card, regs.SPC_DATA_AVAIL_USER_LEN, byref(l_avail_user))
        return l_avail_user.value

    def read_buffer_position(self):
        """Get where the buffer reader/pointer is."""
        l_pc_pos = c_int32()
        spcm_dwGetParam_i32(self.h_card, regs.SPC_DATA_AVAIL_USER_POS, byref(l_pc_pos))
        return l_pc_pos.value

    def read_data(self, bytes_per_transfer, bytes_offset):
        """Read data from the RAM buffer. Interprets a part of the RAM buffer as array.

        Args:
            bytes_per_transfer: how many bytes that are interpreted.
            bytes_offset: bytes left out from the start of the buffer.
        """
        # cast to pointer to 16bit integer
        nr_of_datapoints = int(bytes_per_transfer / self.channels_per_card / 2)
        # logger.info("read_data: {} Mb".format((self.read_buffer_position() + bytes_offset)/1024/1024))
        if self.read_buffer_position() + bytes_offset >= self.ram_buffer_size:
            # logger.info("wrap around, bytes_offset % ram_buffer_size: {} Mb"
            #             .format(bytes_offset % self.ram_buffer_size/1024/1024))
            offset = (self.read_buffer_position() + bytes_offset) % self.ram_buffer_size
        else:
            offset = self.read_buffer_position() + bytes_offset
        x = cast(addressof(self._pv_buffer) + offset, POINTER(c_int16))
        np_data = np.ctypeslib.as_array(x, shape=(nr_of_datapoints, self.channels_per_card)).T
        return np_data

    def data_has_been_read(self, bytes_read=None):
        """Mark buffer space as available again."""
        notify_size = self.l_notify_size if bytes_read is None else c_int32(bytes_read)
        if self.debug_buffer_behaviour is True:
            print("mark buffer as available: {0:08x}".format(notify_size.value))
        spcm_dwSetParam_i32(self.h_card, regs.SPC_DATA_AVAIL_CARD_LEN, notify_size)

    def stop_recording(self):
        """Send the stop command to the card."""
        logger.info("card {0} stopped.".format(self.card_nr))
        spcm_dwSetParam_i32(self.h_card, regs.SPC_M2CMD, regs.M2CMD_CARD_STOP | regs.M2CMD_DATA_STOPDMA)

    def start_recording(self):
        """Start recording and DMA for this card."""
        dw_error = spcm_dwSetParam_i32(
            self.h_card,
            regs.SPC_M2CMD,
            regs.M2CMD_CARD_START | regs.M2CMD_CARD_ENABLETRIGGER | regs.M2CMD_DATA_STARTDMA,
        )
        if dw_error != err.ERR_OK:
            logger.error("card {} start failed with error {}".format(self.card_nr, dw_error))
            return -1
        return 0

    def close(self):
        """Close the handle to the card."""
        if self.h_card is not None:
            spcm_vClose(self.h_card)
            logger.info("card {0} closed.".format(self.card_nr))

    # --- PPS / hardware timestamp methods ---

    def pps_sync(self):
        """Issue SPC_TS_RESET_WAITREFCLK to block until the next PPS edge.

        The Spectrum driver latches the PC system clock at the PPS edge and stores
        it in SPC_TIMESTAMP_STARTDATE / SPC_TIMESTAMP_STARTTIME.  The timestamp
        engine mode must already be configured (done in init_card via
        one_card_std_init).

        Returns 0 on success, -1 on error/timeout.
        """
        # Drain any stale error left on the handle by init_card (e.g. from a register
        # write that is not supported by this firmware revision).  If an error is
        # pending, the very first driver call on this handle returns ERR_LASTERR
        # (0x0101) instead of executing the requested command.
        from ctypes import create_string_buffer as _csb
        _drain_buf = _csb(256)
        spcm_dwGetErrorInfo_i32(self.h_card, None, None, _drain_buf)

        logger.info("card {}: waiting for PPS edge (SPC_TS_RESET_WAITREFCLK)...".format(self.card_nr))
        # OR in the persistent mode+TSCNT bits alongside the command bit so the
        # TSCNT field is not cleared to zero (which would mean no refclock source).
        ts_wait_cmd = (regs.SPC_TS_RESET_WAITREFCLK
                       | regs.SPC_TSMODE_STANDARD
                       | regs.SPC_TSCNT_REFCLOCKPOS)
        dw_error = spcm_dwSetParam_i32(
            self.h_card, regs.SPC_TIMESTAMP_CMD, ts_wait_cmd)
        if dw_error != err.ERR_OK:
            logger.error("card {}: SPC_TS_RESET_WAITREFCLK failed with error 0x{:04x}".format(
                self.card_nr, dw_error))
            # Clear the error so it doesn't become ERR_LASTERR on the next driver call
            from ctypes import create_string_buffer
            _errbuf = create_string_buffer(256)
            spcm_dwGetErrorInfo_i32(self.h_card, None, None, _errbuf)
            return -1
        logger.info("card {}: PPS sync completed".format(self.card_nr))
        return 0

    def read_pps_start_time(self):
        """Read the hardware-latched start date and time after a PPS sync.

        Returns (startdate, starttime) as raw 32-bit integers, or (None, None) on error.
        Spectrum encoding:
          - SPC_TIMESTAMP_STARTDATE: YYYYMMDD integer (e.g. 20260408)
          - SPC_TIMESTAMP_STARTTIME: seconds since midnight * 1000 (millisecond resolution)
        """
        l_date = c_int32(0)
        l_time = c_int32(0)
        spcm_dwGetParam_i32(self.h_card, regs.SPC_TIMESTAMP_STARTDATE, byref(l_date))
        spcm_dwGetParam_i32(self.h_card, regs.SPC_TIMESTAMP_STARTTIME, byref(l_time))
        logger.info("card {}: SPC_TIMESTAMP_STARTDATE={} SPC_TIMESTAMP_STARTTIME={}".format(
            self.card_nr, l_date.value, l_time.value))
        return l_date.value, l_time.value

    def read_timestamp_count(self):
        """Return the number of timestamps currently in the timestamp FIFO."""
        l_count = c_int32(0)
        spcm_dwGetParam_i32(self.h_card, regs.SPC_TIMESTAMP_COUNT, byref(l_count))
        return l_count.value

    def read_timestamp_status(self):
        """Return the raw SPC_TIMESTAMP_STATUS register value."""
        l_status = c_int32(0)
        spcm_dwGetParam_i32(self.h_card, regs.SPC_TIMESTAMP_STATUS, byref(l_status))
        return l_status.value
