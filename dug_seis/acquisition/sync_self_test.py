# DUG-Seis
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""Quick Star Hub/sync preflight diagnostics."""

import logging

from ctypes import byref, c_int32, create_string_buffer

import dug_seis.acquisition.hardware_driver.regs as regs
from dug_seis.acquisition.hardware_driver.pyspcm import (
    spcm_dwGetParam_i32,
    spcm_hOpen,
    spcm_vClose,
)

logger = logging.getLogger("dug-seis")


def _collect_device_paths(topology):
    card_count = int(topology.get("card_count", 0))
    device_map = list(topology.get("card_device_map", []))
    paths = []
    for i in range(card_count):
        if i < len(device_map) and device_map[i]:
            paths.append(device_map[i])
        else:
            paths.append("/dev/spcm{}".format(i))
    return paths


def _probe_sync_handles():
    candidates = [b"sync0", b"sync1", b"/dev/sync0"]
    logger.info("Sync handle probe:")
    ok_any = False
    for candidate in candidates:
        h_sync = spcm_hOpen(create_string_buffer(candidate))
        is_ok = bool(h_sync)
        logger.info("  {} -> {}".format(candidate.decode("ascii"), "OPEN" if is_ok else "FAIL"))
        if is_ok:
            ok_any = True
            spcm_vClose(h_sync)
    return ok_any


def run_sync_self_test(param):
    """Run Star Hub related diagnostics without starting acquisition."""
    topology = param["Acquisition"]["topology"]
    paths = _collect_device_paths(topology)

    logger.info("Sync self-test started")
    logger.info("Configured card_count: {}".format(len(paths)))

    handles = []
    starhub_cards = []

    logger.info("Card feature probe:")
    for i, path in enumerate(paths):
        h_card = spcm_hOpen(create_string_buffer(path.encode("ascii")))
        if not h_card:
            logger.error("  card {} {} -> OPEN FAIL".format(i, path))
            continue

        handles.append(h_card)

        serial = c_int32(0)
        features = c_int32(0)
        err_sn = spcm_dwGetParam_i32(h_card, regs.SPC_PCISERIALNO, byref(serial))
        err_feat = spcm_dwGetParam_i32(h_card, regs.SPC_PCIFEATURES, byref(features))

        has_starhub = bool(features.value & (regs.SPCM_FEAT_STARHUB5 | regs.SPCM_FEAT_STARHUB16))
        if has_starhub:
            starhub_cards.append((i, serial.value, path, features.value))

        logger.info(
            "  card {} {} serial={} err_sn={} err_feat={} features=0x{:08x} starhub_bit={}".format(
                i,
                path,
                serial.value,
                err_sn,
                err_feat,
                features.value & 0xFFFFFFFF,
                has_starhub,
            )
        )

    if starhub_cards:
        logger.info("Detected Star Hub carrier candidates:")
        for idx, serial, path, features in starhub_cards:
            logger.info(
                "  index={} serial={} device={} features=0x{:08x}".format(
                    idx, serial, path, features & 0xFFFFFFFF
                )
            )
    else:
        logger.warning("No card reports Star Hub feature bits")

    sync_open_ok = _probe_sync_handles()
    if not sync_open_ok:
        logger.error("No sync handle could be opened (sync0/sync1/dev sync path)")

    for h_card in handles:
        spcm_vClose(h_card)

    logger.info("Sync self-test finished")
    if starhub_cards and sync_open_ok:
        logger.info("Sync self-test result: PASS")
        return 0

    logger.error("Sync self-test result: FAIL")
    return 1
