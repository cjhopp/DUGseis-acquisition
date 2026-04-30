#!/bin/bash
# copy_scautopick_alias_configs.sh
#
# Creates one scautopick alias per channel (Z/X/Y) for each 3-component station,
# registers the aliases with SeisComP, writes per-channel module configs with
# streams.whitelist, and updates station key files.
#
# Layout produced:
#   scautopick_AML1Z  streams.whitelist = CB.AML1..XNZ
#   scautopick_AML1X  streams.whitelist = CB.AML1..XNX
#   scautopick_AML1Y  streams.whitelist = CB.AML1..XNY
#   ... (48 aliases total for 16 stations)
#
# Usage: DRY_RUN=1 ./copy_scautopick_alias_configs.sh   # preview only
#        ./copy_scautopick_alias_configs.sh              # apply changes

set -euo pipefail

SEISCOMP_ETC="/home/sysop/seiscomp/etc"
SEISCOMP_BIN="/home/sysop/seiscomp/bin/seiscomp"
KEY_DIR="${SEISCOMP_ETC}/key"
NET="CB"
PROFILE_NAME="cussp"
PRIMARY_GROUP="UNFILTERED_PICK"
DRY_RUN="${DRY_RUN:-0}"

STATIONS=(AML1 AML2 AML3 AML4 AMU1 AMU2 AMU3 AMU4 DML1 DML2 DML3 DML4 DMU1 DMU2 DMU3 DMU4)
declare -A CHA_SUFFIX=(["XNZ"]="Z" ["XNX"]="X" ["XNY"]="Y")
CHANNELS=(XNZ XNX XNY)

PROFILE_CONTENT='# Defines the filter to be used for picking.
detecFilter = "RMHP(0.001)>>ITAPER(0.002)>>BW(4,1000,12000)>>STALTA(0.002,0.05)"

# For which value on the filtered waveform is a pick detected.
trigOn = 3
'

echo "dry_run=${DRY_RUN}"
echo ""

for sta in "${STATIONS[@]}"; do
    for cha in "${CHANNELS[@]}"; do
        comp="${CHA_SUFFIX[$cha]}"
        alias_name="scautopick_${sta}${comp}"
        profile_dir="${KEY_DIR}/${alias_name}"
        cfg_path="${SEISCOMP_ETC}/${alias_name}.cfg"

        echo "${alias_name}  (${NET}.${sta}..${cha})"

        # 1. Register alias if not already registered
        if [[ ! -f "${SEISCOMP_ETC}/init/${alias_name}.py" ]]; then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo "  [dry] seiscomp alias create ${alias_name} scautopick"
            else
                "$SEISCOMP_BIN" alias create "${alias_name}" scautopick
                echo "  alias registered"
            fi
        fi

        # 2. Binding profile
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
        else
            cat > "${cfg_path}" <<EOF
connection.primaryGroup = ${PRIMARY_GROUP}
useAllStreams = false
streams.whitelist = ${NET}.${sta}..${cha}
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

        # 4. Station key file — ensure all 3 per-channel bindings present
        station_key="${KEY_DIR}/station_${NET}_${sta}"
        if [[ ! -f "$station_key" ]]; then
            echo "  WARNING: ${station_key} not found — skipping"
            continue
        fi
        binding_line="${alias_name}:${PROFILE_NAME}"
        if grep -q "^${alias_name}:" "$station_key"; then
            echo "  binding already in $(basename $station_key)"
        else
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo "  [dry] add '${binding_line}' to $(basename $station_key)"
            else
                sed -i "/^global:/a ${binding_line}" "$station_key"
                echo "  binding added to $(basename $station_key)"
            fi
        fi
    done
done

echo ""
echo "Done. Now run:"
echo "  seiscomp update-config"
echo "  seiscomp enable scautopick_AML1Z scautopick_AML1X ...  (or all 48)"
echo "  seiscomp start scautopick"
