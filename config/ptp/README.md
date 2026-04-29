# PTP System Clock Discipline (eno2np1)

This runbook configures linuxptp so the Linux system clock (CLOCK_REALTIME)
is disciplined from a PTP grandmaster reachable from interface eno2np1.

Target grandmaster for this deployment: 204.114.29.59

## 1) Install required packages

On Ubuntu/Debian:

sudo apt-get update
sudo apt-get install -y linuxptp ethtool tcpdump

## 2) Check NIC timestamp capabilities

ethtool -T eno2np1

Expected for best accuracy: hardware transmit/receive timestamping and a PHC.

## 3) Deploy config files

Copy one ptp4l profile and one phc2sys env file:

sudo cp config/ptp/ptp4l-eno2np1-udp4.conf /etc/linuxptp/ptp4l-eno2np1.conf
sudo cp config/ptp/phc2sys-eno2np1.env /etc/default/phc2sys-eno2np1

If UDPv4 does not lock, switch to Layer-2 profile:

sudo cp config/ptp/ptp4l-eno2np1-l2.conf /etc/linuxptp/ptp4l-eno2np1.conf

## 4) Deploy services

sudo cp config/systemd/dug-ptp4l@.service /etc/systemd/system/
sudo cp config/systemd/dug-phc2sys@.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable dug-ptp4l@eno2np1.service
sudo systemctl enable dug-phc2sys@eno2np1.service

sudo systemctl start dug-ptp4l@eno2np1.service
sudo systemctl start dug-phc2sys@eno2np1.service

## 5) Verify lock and discipline

Check service status:

systemctl status dug-ptp4l@eno2np1.service --no-pager
systemctl status dug-phc2sys@eno2np1.service --no-pager

Follow logs:

journalctl -u dug-ptp4l@eno2np1.service -f
journalctl -u dug-phc2sys@eno2np1.service -f

Quick transport hinting:

sudo tcpdump -ni eno2np1 udp port 319 or udp port 320
sudo tcpdump -ni eno2np1 ether proto 0x88f7

If only UDP packets appear, use UDPv4 profile.
If EtherType 0x88f7 appears and UDP does not, use Layer-2 profile.

## 6) DUGseis alignment

In DUGseis YAML:
- timing.timestamp_source: system_clock
- timing.timing_quality_source: fixed

Note: ptp_status timing quality is not yet implemented in the current codebase.
