#!/bin/bash
# copy_scautopick_alias_configs.sh
#
# Copies the scautopick module config and binding profile from a source alias
# to all other scautopick aliases found in the SeisComP key directory, and
# adds the scautopick binding line to each corresponding station key file.
#
# Usage: DRY_RUN=1 ./copy_scautopick_alias_configs.sh   # preview only
#        ./copy_scautopick_alias_configs.sh              # apply changes

set -euo pipefail

SEISCOMP_ETC="/home/sysop/seiscomp/etc"
SOURCE_SUFFIX="AML1"
PROFILE_NAME="cussp"
DRY_RUN="${DRY_RUN:-0}"

SOURCE_ALIAS="scautopick_${SOURCE_SUFFIX}"
SOURCE_CFG="${SEISCOMP_ETC}/${SOURCE_ALIAS}.cfg"
SOURCE_PROFILE="${SEISCOMP_ETC}/key/${SOURCE_ALIAS}/profile_${PROFILE_NAME}"

for f in "$SOURCE_CFG" "$SOURCE_PROFILE"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: Source file not found: $f" >&2
        exit 1
    fi
done

echo "Source : ${SOURCE_ALIAS}  profile=${PROFILE_NAME}  dry_run=${DRY_RUN}"
echo ""

shopt -s nullglob
alias_dirs=( "${SEISCOMP_ETC}/key/scautopick_"*/ )

for alias_dir in "${alias_dirs[@]}"; do
    alias_name=$(basename "$alias_dir")
    target_suffix="${alias_name#scautopick_}"

    [[ "$alias_name" == "$SOURCE_ALIAS" ]] && continue

    echo "${alias_name}"

    target_cfg="${SEISCOMP_ETC}/${alias_name}.cfg"
    target_profile="${SEISCOMP_ETC}/key/${alias_name}/profile_${PROFILE_NAME}"
    binding_line="${alias_name}:${PROFILE_NAME}"

    # 1. Module config
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [dry] cp -> ${target_cfg}"
    else
        cp "$SOURCE_CFG" "$target_cfg"
        echo "  module cfg copied"
    fi

    # 2. Binding profile
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  [dry] cp -> ${target_profile}"
    else
        cp "$SOURCE_PROFILE" "$target_profile"
        echo "  binding profile copied"
    fi

    # 3. Station key file — insert or replace scautopick binding
    mapfile -t station_keys < <(
        find "${SEISCOMP_ETC}/key" -maxdepth 1 -name "station_*_${target_suffix}" -type f
    )

    if [[ ${#station_keys[@]} -eq 0 ]]; then
        echo "  WARNING: no station_*_${target_suffix} key file found — skipping"
        continue
    fi

    for station_key in "${station_keys[@]}"; do
        key_basename=$(basename "$station_key")
        if grep -q "^scautopick_" "$station_key"; then
            # Replace existing (wrong) scautopick binding
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo "  [dry] replace scautopick line in ${key_basename} -> ${binding_line}"
            else
                sed -i "s|^scautopick_.*|${binding_line}|" "$station_key"
                echo "  scautopick binding replaced in ${key_basename}"
            fi
        else
            # Insert after the global: line (mirrors station_CB_AML1 order)
            if [[ "$DRY_RUN" -eq 1 ]]; then
                echo "  [dry] insert '${binding_line}' after global: in ${key_basename}"
            else
                sed -i "/^global:/a ${binding_line}" "$station_key"
                echo "  scautopick binding added to ${key_basename}"
            fi
        fi
    done
done

echo ""
echo "Done."
