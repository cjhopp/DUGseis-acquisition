# Software for data acquisition and real-time processing of induced
# seismicity during rock-laboratory experiments.
#
#
# :copyright:
#    ETH Zurich, Switzerland
# :license:
#    GNU Lesser General Public License, Version 3
#    (https://www.gnu.org/copyleft/lesser.html)
#
"""Command line."""

import click
import yaml
import logging
import os
import datetime
import subprocess
import numpy as np
from logging.handlers import RotatingFileHandler
from dug_seis.acquisition.acquisition import acquisition_ as acquisition_function
from dug_seis.acquisition.sync_self_test import run_sync_self_test
# from dug_seis.processing.processing import processing as processing_function
# from dug_seis.merge import merge as merge_function
# from dug_seis.visualization.dashboard import dashboard as dashboard_function

# shut up libraries
logging.getLogger('requests').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)

CONFIG_VERSION = 3

@click.group()
@click.option('-v', '--verbose', is_flag=True, help='Enables verbose mode')
@click.option('--cfg', metavar='<config file>',
              help='Source config file. If not specified,'
                   'the script tries ./dug-seis.yaml and config/dug-seis.yaml',
              default=None)
@click.option('--mode', metavar='<mode>', default='live',
              help='Mode can be either "live" (default) or'
                   '"post", for post processing mode)')
@click.option('--log', metavar='<path>', help='Specify a log file')
@click.version_option()
@click.pass_context
def cli(ctx, cfg, verbose, mode, log):
    """
    Run data acquisition and real-time processing of induced
    seismicity during rock-laboratory experiments

    """
    # kill leftover celery workers
    #os.system("pkill -9 -f 'celery worker'")
    os.system("pkill -9 -f 'redis-server'")
    # Setup logging. By default we log to stdout with ERROR level and to a log
    # file (if specified) with INFO level. Setting verbose logs to both
    # handlers with DEBUG level.
    logger = logging.getLogger('dug-seis')
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s')
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    if not log: log = 'dugseis_acquisition.log'
    fh = RotatingFileHandler(log)
    fh.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh.formatter = formatter
    logger.addHandler(fh)
    logger.info('DUG-Seis started')
    # Load config file and set the context for the subcommand
    # ptp-status does not need a config file — skip loading entirely
    if ctx.invoked_subcommand == 'ptp-status':
        ctx.obj = {}
        return

    search = [cfg or '', 'dug-seis.yaml', 'config/dug-seis.yaml']
    cfg_path = next((p for p in search if os.path.exists(p)), None)
    if not cfg_path:
        logger.error('no parameter file found')
        exit(-1)
    with open(cfg_path) as f:
        try:
            param = yaml.load(f, Loader=yaml.FullLoader)
        except IOError:
            logger.error(f'could not read parameter file at {cfg_path}')
            exit(-1)
        if param['Version'] != CONFIG_VERSION:
            logger.error(f'Configuration Version is {param["Version"]} but it must be {CONFIG_VERSION}')
            exit(-1)
    param['General']['mode'] = mode
    param.setdefault('_meta', {})['config_path'] = os.path.abspath(cfg_path)
    ctx.obj = {
        'param': param
    }


@cli.command()
@click.pass_context
def acquisition(ctx):
    """
    Run data acquisition

    The output goes to ASDF files in the folder defined in the options file

    """
    param = ctx.obj['param']
    acquisition_function(param)

@cli.command()
@click.pass_context
def merge(ctx):
    """
    Merge short ASDF files

    The output goes to ASDF files in the folder defined in the options file

    """
    param = ctx.obj['param']
    merge_function(param)

@cli.command()
@click.pass_context
def processing(ctx):
    """
    Run event trigger on ASDF files

    """
    param = ctx.obj['param']
    processing_function(param)


@cli.command()
@click.pass_context
def show_parameters(ctx):
    """
    Show parameters

    """
    param = ctx.obj['param']
    print(yaml.dump(param))

@cli.command()
@click.pass_context
def dashboard(ctx):
    """
    Run dashboard to show recent events

    """
    param = ctx.obj['param']
    dashboard_function(param)


@cli.command(name='sync-self-test')
@click.pass_context
def sync_self_test(ctx):
    """Run Star Hub/sync diagnostics without starting acquisition."""
    param = ctx.obj['param']
    rc = run_sync_self_test(param)
    if rc != 0:
        raise SystemExit(rc)


@cli.command(name='write-file')
@click.option('--duration', required=True, type=float, metavar='<seconds>',
              help='Recording duration in seconds')
@click.option('--out', default=None, metavar='<dir>',
              help='Output directory (default: current working directory)')
@click.pass_context
def write_file(ctx, duration, out):
    """Record all channels for a fixed duration and write to a .npz file.

    Useful for channel mapping tests or any scenario where you need a simple
    file dump without the full acquisition pipeline (no ASDF, no streaming).
    """
    import os
    from dug_seis.acquisition.acquisition import (
        _apply_schema_defaults,
        _validate_schema_lengths,
        _check_if_hardware_needs_to_be_simulated,
        _sorted_input_ranges,
    )
    from dug_seis.acquisition.write_file import run_write_file

    param = ctx.obj['param']
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

    out_dir = out or os.getcwd()
    run_write_file(param, duration, out_dir)


@cli.command(name='ptp-status', context_settings=dict(max_content_width=120))
def ptp_status():
    """Check PTP discipline chain status (ptp4l, phc2sys, system clock).

    Does not require a config file. Exits with code 1 if any check fails.
    """
    import re, sys

    ok = True

    def _run(cmd):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            return r.stdout + r.stderr
        except Exception as e:
            return str(e)

    def _service_state(name):
        out = _run(f'systemctl is-active {name}')
        return out.strip()

    def _last_lines(unit, n=5):
        return _run(f'journalctl -u {unit} -n {n} --no-pager -o short-monotonic')

    # 1. ptp4l
    ptp4l_state = _service_state('dug-ptp4l@eno2np1.service')
    ptp4l_ok = ptp4l_state == 'active'
    click.echo(f"[{'OK' if ptp4l_ok else 'FAIL'}] ptp4l service: {ptp4l_state}")

    if ptp4l_ok:
        ptp4l_log = _last_lines('dug-ptp4l@eno2np1.service', 15)
        # In steady state the log only shows rms lines — that IS slave state.
        rms_match = re.search(r'rms\s+(\d+)\s+max\s+(\d+)', ptp4l_log)
        slave_transition = 'to SLAVE' in ptp4l_log
        if rms_match:
            click.echo(f"  [OK] PHC locked to grandmaster  rms={rms_match.group(1)}ns  max={rms_match.group(2)}ns")
        elif slave_transition:
            click.echo(f"  [OK] PHC locked to grandmaster (SLAVE state confirmed)")
        else:
            click.secho("  [WARN] ptp4l running but no rms stats found — may not be locked yet", fg='yellow')
            ok = False
    else:
        ok = False

    # 2. phc2sys
    phc2sys_state = _service_state('dug-phc2sys@eno2np1.service')
    phc2sys_ok = phc2sys_state == 'active'
    click.echo(f"[{'OK' if phc2sys_ok else 'FAIL'}] phc2sys service: {phc2sys_state}")

    if phc2sys_ok:
        phc_log = _last_lines('dug-phc2sys@eno2np1.service', 10)
        s2_match = re.search(r'phc offset\s+([-\d]+)\s+(s\d)\s+freq\s+([-+\d]+)', phc_log)
        if s2_match:
            offset_ns, servo, freq = s2_match.group(1), s2_match.group(2), s2_match.group(3)
            servo_ok = servo == 's2'
            click.echo(f"  [{'OK' if servo_ok else 'WARN'}] CLOCK_REALTIME servo={servo}  offset={offset_ns}ns  freq={freq}ppb")
            if not servo_ok:
                click.secho("  [WARN] servo not in s2 (actively steering) — still converging", fg='yellow')
                ok = False
        else:
            click.secho("  [WARN] no phc offset line found — phc2sys may not have locked yet", fg='yellow')
            ok = False
    else:
        ok = False

    # 3. Competing NTP daemons
    for daemon in ('chrony', 'systemd-timesyncd', 'ntp'):
        state = _service_state(daemon)
        if state == 'active':
            click.secho(f"[WARN] {daemon} is active — may fight phc2sys over CLOCK_REALTIME", fg='yellow')
            ok = False

    # 4. System clock sync source
    tc = _run('timedatectl show')
    ntp_sync = re.search(r'NTPSynchronized=(\w+)', tc)
    if ntp_sync and ntp_sync.group(1) == 'yes':
        click.echo(f"  [OK] kernel clock discipline active (set by phc2sys)")

    # Summary
    click.echo()
    if ok:
        click.secho("PTP discipline chain: HEALTHY", fg='green', bold=True)
    else:
        click.secho("PTP discipline chain: DEGRADED — see warnings above", fg='red', bold=True)
        sys.exit(1)
