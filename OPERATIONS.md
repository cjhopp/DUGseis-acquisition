# DUG-Seis Acquisition — Operations Guide

**Project:** CUSSP_commissioning / SURF  
**Host:** gmf@\<acquisition-host\>  
**Config:** `config/dug-seis.yaml`  
**Service:** `dug-seis-acquisition` (systemd user service, `gmf`)

---

## Quick-Reference Commands

```bash
# Start acquisition (begin recording)
systemctl --user start  dug-seis-acquisition
systemctl --user enable dug-seis-acquisition   # persist across reboots

# Stop acquisition (end recording)
systemctl --user disable dug-seis-acquisition  # don't restart on next boot
systemctl --user stop    dug-seis-acquisition

# Status / logs
systemctl --user status  dug-seis-acquisition
journalctl --user -u dug-seis-acquisition -f   # follow live
journalctl --user -u dug-seis-acquisition -n 200 --no-pager
```

> **Intent:** `enable` means "restart if the host reboots"; `disable` means
> "do not auto-start — I am intentionally stopping acquisition".  
> `Restart=always` is set in the unit, so the service self-heals from crashes
> only while enabled.

---

## Service Unit

`~/.config/systemd/user/dug-seis-acquisition.service`

```ini
[Unit]
Description=DUG-Seis Acquisition
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/gmf/DUGseis-acquisition
ExecStart=/home/gmf/miniconda3/envs/dug_seis_acquisition/bin/dug-seis \
          --cfg /home/gmf/DUGseis-acquisition/config/dug-seis.yaml \
          acquisition
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Linger is enabled for `gmf` so user services survive logout:

```bash
loginctl enable-linger gmf
```

---

## Hardware Topology

| Parameter | Value |
|---|---|
| Cards | 8 × Spectrum digitiser cards |
| Channels per card | 8 → **64 channels total** |
| Device nodes | `/dev/spcm0` … `/dev/spcm7` |
| Trigger card index | 5 (`/dev/spcm5`) |
| Sync strategy | StarHub |
| Device policy | `fixed_order` |

**Card serial → device mapping** (order matches `card_device_map`):

| `/dev/spcm` | Serial |
|---|---|
| spcm0 | 24916 |
| spcm1 | 24917 |
| spcm2 | 24918 |
| spcm3 | 24920 |
| spcm4 | 24914 |
| spcm5 | 24913 |
| spcm6 | 24915 |
| spcm7 | 24919 |

---

## Acquisition Settings

| Parameter | Value |
|---|---|
| Sampling frequency | 200 000 Hz (200 kHz) |
| Input range | ±1000 mV (all 64 channels) |
| Clock source | `intpll` (internal PLL slaved to PTP-disciplined system clock) |
| Reference clock | 200 000 Hz |
| Clock termination | 50 Ω |
| Output mode | `streaming_only` (no ASDF files written) |
| Streaming port | 65535, all channels 1–64, bind to all interfaces |
| Acquisition folder | `./raw_waveforms` |

---

## Timing Configuration

### Overview

```
GPS/GNSS grandmaster (204.114.29.59)
    │  PTP (IEEE 1588, UDPv4 unicast or Layer-2)
    ▼
eno2np1 NIC hardware clock (PHC)
    │  phc2sys  →  CLOCK_REALTIME (Linux system clock)
    ▼
DUG-Seis: timestamp_source = system_clock
    │  hardware_timestamps enabled
    ▼
Spectrum cards latch PPS pulse → per-sample timestamps
```

### DUG-Seis YAML timing block

```yaml
timing:
  timestamp_source: system_clock          # use Linux CLOCK_REALTIME
  timing_quality_source: fixed            # ptp_status not yet implemented
  timing_quality_fixed_value: 100         # report 100 % quality unconditionally

  hardware_timestamps:
    enabled: true
    pps_sync_timeout_ms: 2500             # wait up to 2.5 s for PPS at startup
    pps_edge_polarity: negative           # falling edge (active-low GPS PPS)
```

**`timestamp_source: system_clock`** — DUG-Seis derives all packet timestamps
from the Linux system clock.  Accuracy depends entirely on how well the system
clock is disciplined (see PTP section below).

**`timing_quality_source: fixed`** — The `ptp_status` quality source is not
yet implemented in the current codebase.  Setting `fixed` with value 100
suppresses spurious quality warnings; it does **not** mean timing is
unconditionally good — see "Verifying PTP lock" below.

**`pps_sync_timeout_ms: 2500`** — At startup the acquisition waits up to
2500 ms for the first PPS pulse to arrive from the hardware.  If no pulse is
seen within that window, acquisition proceeds anyway (timestamps will still
come from the system clock but will lack the PPS-synchronised hardware latch).

**`pps_edge_polarity: negative`** — PPS signal uses a falling edge
(active-low / 3.3 V → 0 V).  Do not change this unless the PPS source is
known to be active-high.

### PTP Services

Two systemd system services must be running **before** starting acquisition:

```bash
# Status
systemctl status dug-ptp4l@eno2np1.service  --no-pager
systemctl status dug-phc2sys@eno2np1.service --no-pager

# Follow logs
journalctl -u dug-ptp4l@eno2np1.service  -f
journalctl -u dug-phc2sys@eno2np1.service -f
```

| Service | Role |
|---|---|
| `dug-ptp4l@eno2np1` | Synchronises NIC PHC to PTP grandmaster |
| `dug-phc2sys@eno2np1` | Disciplines `CLOCK_REALTIME` from NIC PHC |

`phc2sys` options in use: `-s eno2np1 -c CLOCK_REALTIME -w -m -N 8 -R 16`  
(`-w` = wait for ptp4l lock before disciplining; `-N 8 -R 16` = tighter
servo averaging)

### PTP Profile: UDPv4 unicast (default)

Config installed at `/etc/linuxptp/ptp4l-eno2np1.conf`:

```
network_transport   UDPv4
time_stamping       hardware
clientOnly          1
delay_mechanism     E2E
domainNumber        0
unicast GM          204.114.29.59
```

**If UDPv4 does not lock**, switch to Layer-2:

```bash
sudo cp config/ptp/ptp4l-eno2np1-l2.conf /etc/linuxptp/ptp4l-eno2np1.conf
sudo systemctl restart dug-ptp4l@eno2np1.service
```

Diagnose with:

```bash
sudo tcpdump -ni eno2np1 udp port 319 or udp port 320   # UDPv4 PTP
sudo tcpdump -ni eno2np1 ether proto 0x88f7              # Layer-2 PTP
```

### Verifying PTP Lock

Good lock looks like this in the ptp4l log:

```
ptp4l[…]: rms   8 max  12 freq  -2345 +/-  15 delay  210 +/-   3
```

Good discipline in the phc2sys log:

```
phc2sys[…]: CLOCK_REALTIME phc offset    -42 s2 freq  +3210 delay   398
```

Key indicators:
- **`s2`** state = servo locked (good). `s0` = unlocked, `s1` = stepping.
- **rms offset** should be single-digit nanoseconds on a good link.
- If offset is in the microsecond range, check network path to grandmaster.

### Timing Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| No PPS in 2500 ms at startup | PPS cable unplugged or wrong polarity | Check cable; verify `pps_edge_polarity: negative` |
| phc2sys stuck in `s0`/`s1` | ptp4l not locked | Check `dug-ptp4l` logs; verify GM reachable |
| ptp4l sees no packets | Wrong transport profile | Use `tcpdump` to determine UDPv4 vs L2; switch profile |
| Competing NTP discipline | chrony/timesyncd still running | `sudo systemctl disable --now chrony chronyd systemd-timesyncd` |
| rms offset > 1 µs | Noisy network path or large GM distance | Use hardware timestamps (`time_stamping hardware` in ptp4l profile) |
| `timing_quality` warnings in DUG-Seis log | `ptp_status` source selected | Switch to `timing_quality_source: fixed` |

### Initial PTP Setup (one-time)

See `config/ptp/README.md` for the full runbook.  Summary:

```bash
# Deploy configs
sudo cp config/ptp/ptp4l-eno2np1-udp4.conf /etc/linuxptp/ptp4l-eno2np1.conf
sudo cp config/ptp/phc2sys-eno2np1.env      /etc/default/phc2sys-eno2np1
sudo cp config/systemd/dug-ptp4l@.service   /etc/systemd/system/
sudo cp config/systemd/dug-phc2sys@.service /etc/systemd/system/
sudo systemctl daemon-reload

# Disable competing time sources
sudo systemctl disable --now chrony chronyd systemd-timesyncd || true
sudo timedatectl set-ntp false

# Enable and start
sudo systemctl enable --now dug-ptp4l@eno2np1.service
sudo systemctl enable --now dug-phc2sys@eno2np1.service
```

---

## Modifying the Config

Edit `config/dug-seis.yaml`, then restart the service:

```bash
# Edit
nano config/dug-seis.yaml    # or your preferred editor

# Restart
systemctl --user restart dug-seis-acquisition
journalctl --user -u dug-seis-acquisition -f   # watch for startup errors
```

Common changes and where to find them:

| What to change | YAML key |
|---|---|
| Input voltage range | `hardware_settings.input_range` |
| Sampling rate | `hardware_settings.sampling_frequency` (and `reference_clock_hz`) |
| Streaming port | `streaming_servers[0].port` |
| Which channels to stream | `streaming_servers[0].channels` |
| Project / output folder | `General.project_name`, `General.acquisition_folder` |
| PPS timeout at startup | `timing.hardware_timestamps.pps_sync_timeout_ms` |
| PPS polarity | `timing.hardware_timestamps.pps_edge_polarity` |

---

## Normal Startup Sequence

1. Verify PTP lock (`systemctl status dug-ptp4l@eno2np1 dug-phc2sys@eno2np1`)
2. `systemctl --user start dug-seis-acquisition`
3. `journalctl --user -u dug-seis-acquisition -f` — confirm "Acquisition started", no PPS timeout warnings
4. `systemctl --user enable dug-seis-acquisition` if acquisition should survive a reboot

## Normal Shutdown Sequence

1. `systemctl --user disable dug-seis-acquisition` — prevents auto-restart on reboot
2. `systemctl --user stop dug-seis-acquisition`
3. Optionally stop PTP if no other consumers: `sudo systemctl stop dug-ptp4l@eno2np1 dug-phc2sys@eno2np1`
