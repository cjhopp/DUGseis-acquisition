# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""PPS hardware timestamp decoding and start-time correction.

Converts the raw register values read from the Spectrum timestamp engine
(SPC_TIMESTAMP_STARTDATE, SPC_TIMESTAMP_STARTTIME) into an absolute UTC
epoch in nanoseconds compatible with the TimeStamps class.
"""
import logging

from obspy.core import UTCDateTime

logger = logging.getLogger('dug-seis')


def decode_spectrum_startdate(raw_date):
    """Decode SPC_TIMESTAMP_STARTDATE from YYYYMMDD integer.

    Args:
        raw_date: Integer read from SPC_TIMESTAMP_STARTDATE (e.g. 20260408).

    Returns:
        Tuple (year, month, day).
    """
    year = raw_date // 10000
    month = (raw_date % 10000) // 100
    day = raw_date % 100
    return year, month, day


def decode_spectrum_starttime(raw_time):
    """Decode SPC_TIMESTAMP_STARTTIME.

    The Spectrum driver stores the time-of-day as seconds since midnight
    (integer).  Sub-second precision is not available from this register;
    the PPS edge defines the exact second boundary.

    Args:
        raw_time: Integer read from SPC_TIMESTAMP_STARTTIME.

    Returns:
        Tuple (hour, minute, second).
    """
    total_seconds = raw_time
    hour = total_seconds // 3600
    minute = (total_seconds % 3600) // 60
    second = total_seconds % 60
    return hour, minute, second


def pps_registers_to_ns(raw_date, raw_time):
    """Convert Spectrum PPS start registers to nanosecond epoch.

    Args:
        raw_date: SPC_TIMESTAMP_STARTDATE value.
        raw_time: SPC_TIMESTAMP_STARTTIME value.

    Returns:
        Start time as nanoseconds since Unix epoch, or None on decode error.
    """
    if raw_date == 0 and raw_time == 0:
        logger.warning("PPS start registers are zero — PPS sync may not have completed")
        return None

    year, month, day = decode_spectrum_startdate(raw_date)
    hour, minute, second = decode_spectrum_starttime(raw_time)

    logger.info("PPS decoded start: {:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
        year, month, day, hour, minute, second))

    try:
        t = UTCDateTime(year=year, month=month, day=day,
                        hour=hour, minute=minute, second=second)
    except Exception as exc:
        logger.error("Failed to construct UTCDateTime from PPS registers "
                     "(date={}, time={}): {}".format(raw_date, raw_time, exc))
        logger.error("Raw values may use a different encoding — check Spectrum "
                     "driver documentation and update decode functions")
        return None

    pps_ns = t.ns
    pc_ns = UTCDateTime().ns
    diff_ms = (pc_ns - pps_ns) / 1_000_000

    logger.info("PPS start time:   {} ({}ns)".format(t, pps_ns))
    logger.info("PC system time:   {} (diff {:.1f} ms)".format(UTCDateTime(ns=pc_ns), diff_ms))

    return pps_ns
