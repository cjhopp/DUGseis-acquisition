#!/bin/bash
# add_hydrophone_bindings.sh
#
# Adds slarchive archiving and one scautopick alias per even-numbered
# hydrophone station (TS02, TS04, ... TS24, channel XDH).
#
# Layout produced:
#   scautopick_TS02XDH  streams.whitelist = CB.TS02..XDH
#   scautopick_TS04XDH  streams.whitelist = CB.TS04..XDH
#   ...
#
# Usage: DRY_RUN=1 ./add_hydrophone_bindings.sh   # preview only
#        sudo ./add_hydrophone_bindings.sh          # apply changes

set -euo pipefail

SEISCOMP_ETC="/home/sysop/seiscomp/etc"
SEISCOMP_BIN="/home/sysop/seiscomp/bin/seiscomp"
KEY_DIR="${SEISCOMP_ETC}/key"
NET="CB"
CHANNEL="XDH"
PROFILE_NAME="cussp"
PRIMARY_GROUP="UNFILTERED_PICK"
DRY_RUN="${DRY_RUN:-0}"

STATIONS=(TS02 TS04 TS06 TS08 TS10 TS12 TS14 TS16 TS18 TS20 TS22 TS24)

# Picker filter — same band as geophones (1-12 kHz), STA/LTA appropriate
# for the 200 kHz sampling rate.  Tune trigOn / detecFilter if hydrophone
# sensitivity or noise floor is very different from the accelerometers.
PROFILE_CONTENT='# Defines the filter to be used for picking.
detecFilter = "RMHP(0.001)>>ITAPER(0.002)>>BW(4,1000,12000)>>STALTA(0.002,0.05)"

# For which value on the filtered waveform is a pick detected.
trigOn = 3
'

echo "dry_run=${DRY_RUN}"
echo ""

for sta in "${STATIONS[@]}"; do
    alias_name="scautopick_${sta}${CHANNEL}"
    profile_dir="${KEY_DIR}/${alias_name}"
    cfg_path="${SEISCOMP_ETC}/${alias_name}.cfg"
    station_key="${KEY_DIR}/station_${NET}_${sta}"

    echo "=== ${alias_name}  (${NET}.${sta}..${CHANNEL}) ==="

    # 1. Register alias if not already registered
    if [[ ! -f "${SEISCOMP_ETC}/init/${alias_name}.py" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo "  [dry] seiscomp alias create ${alias_name} scautopick"
        else
            "$SEISCOMP_BIN" alias create "${alias_name}" scautopick
            echo "  alias registered"
        fi
    else
        echo "  alias already exists"
    fi

    # 2. Binding profile (detecFilter + trigOn)
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [dry] write ${profile_dir}/profile_${PROFILE_NAME}"
    else
        mkdir -p "${profile_dir}"
        printf '%s' "$PROFILE_CONTENT" > "${profile_dir}/profile_${PROFILE_NAME}"
        echo "  binding profile written"
    fi

    # 3. Module config with streams.whitelist
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [dry] write ${cfg_path}"
        echo "    connection.primaryGroup = ${PRIMARY_GROUP}"
        echo "    streams.whitelist = ${NET}.${sta}..${CHANNEL}"
    else
        cat > "${cfg_path}" <<EOF
connection.primaryGroup = ${PRIMARY_GROUP}
useAllStreams = false
streams.whitelist = ${NET}.${sta}..${CHANNEL}
ringBufferSize = 1
timeCorrection = 0
initTime = 1
leadTime = 2
thresholds.deadTime = 0.05
thresholds.amplMaxTimeWindow = 0.05
thresholds.maxGapLength = 1.0
EOF
        echo "  module cfg written"
    fi

    # 4. Station key file — add scautopick and slarchive bindings if absent
    if [[ ! -f "$station_key" ]]; then
        echo "  WARNING: ${station_key} not found — skipping"
        continue
    fi

    scautopick_line="${alias_name}:${PROFILE_NAME}"
    slarchive_line="slarchive:SURF"

    if grep -q "^${alias_name}:" "$station_key"; then
        echo "  scautopick binding already present"
    else
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo "  [dry] add '${scautopick_line}' to $(basename $station_key)"
        else
            echo "${scautopick_line}" >> "$station_key"
            echo "  scautopick binding added"
        fi
    fi

    if grep -q "^slarchive:" "$station_key"; then
        echo "  slarchive binding already present"
    else
        if [[ "$DRY_RUN" -eq 1 ]]; then
            echo "  [dry] add '${slarchive_line}' to $(basename $station_key)"
        else
            echo "${slarchive_line}" >> "$station_key"
            echo "  slarchive binding added"
        fi
    fi

    echo ""
done

echo "All done. Next steps:"
echo ""
echo "  # Sync bindings into running SeisComP:"
echo "  sudo -u sysop /home/sysop/seiscomp/bin/seiscomp update-config"
echo ""
echo "  # Enable and start the 12 new picker aliases:"
ALIASES=""
for sta in "${STATIONS[@]}"; do ALIASES+=" scautopick_${sta}${CHANNEL}"; done
echo "  sudo -u sysop /home/sysop/seiscomp/bin/seiscomp enable${ALIASES}"
echo "  sudo -u sysop /home/sysop/seiscomp/bin/seiscomp start${ALIASES}"
