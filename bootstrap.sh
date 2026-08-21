#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -Eeuo pipefail

PLUGIN_NAME="Ayaneo3Companion"
PLUGIN_ROOT="/home/deck/homebrew/plugins"
MAP_TARGET="/etc/inputplumber/capability_maps.d/ayaneo_type7.yaml"
LEGACY_DEVICE_TARGET="/etc/inputplumber/devices.d/01-ayaneo3-companion.yaml"
LEGACY_MAP_TARGET_1="/etc/inputplumber/capability_maps.d/01-ayaneo3-companion-aya7.yaml"
LEGACY_MAP_TARGET_2="/etc/inputplumber/capability_maps.d/ayaneo3-companion.yaml"

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  printf 'Usage: %s [Ayaneo3Companion-version.zip]\n' "${0##*/}"
  printf 'Installs AYANEO 3 Companion and its AYANEO 3 key bindings without using QAM.\n'
  exit 0
fi

[[ "$(< /sys/class/dmi/id/sys_vendor)" == "AYANEO" ]] || die "this is not an AYANEO device"
[[ "$(< /sys/class/dmi/id/product_name)" == "AYANEO 3" ]] || die "AYANEO 3 is required"
command -v python3 >/dev/null || die "python3 is required"
command -v systemctl >/dev/null || die "systemd is required"
systemctl cat plugin_loader.service >/dev/null 2>&1 || die "install Decky Loader first"

archive="${1:-}"
if [[ -z "$archive" ]]; then
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  shopt -s nullglob
  archives=("$script_dir"/Ayaneo3Companion-*.zip)
  shopt -u nullglob
  ((${#archives[@]})) || die "place an Ayaneo3Companion ZIP next to this script or pass its path"
  archive="$(printf '%s\n' "${archives[@]}" | sort -V | tail -n 1)"
fi
archive="$(realpath -- "$archive")"
[[ -f "$archive" ]] || die "archive not found: $archive"

work_dir="$(mktemp -d --tmpdir ayaneo3-companion.XXXXXXXX)"
loader_stopped=false
cleanup() {
  rm -rf -- "$work_dir"
  if [[ "$loader_stopped" == true ]]; then
    sudo systemctl start plugin_loader.service || true
  fi
}
trap cleanup EXIT

python3 -m zipfile -e "$archive" "$work_dir"
source_dir="$work_dir/$PLUGIN_NAME"
[[ -f "$source_dir/plugin.json" ]] || die "invalid plugin archive"
[[ -f "$source_dir/main.py" && -f "$source_dir/dist/index.js" ]] || die "plugin files are incomplete"
[[ -f "$source_dir/assets/ayaneo3-companion.yaml" ]] || die "InputPlumber capability map is missing"

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$source_dir/plugin.json")"
printf 'Installing AYANEO 3 Companion %s...\n' "$version"

sudo -v
sudo systemctl stop plugin_loader.service
loader_stopped=true
sudo install -d -m 755 "$PLUGIN_ROOT/$PLUGIN_NAME"
sudo cp -a "$source_dir/." "$PLUGIN_ROOT/$PLUGIN_NAME/"
sudo chown -R root:root "$PLUGIN_ROOT/$PLUGIN_NAME"
sudo rm -f -- "$LEGACY_DEVICE_TARGET" "$LEGACY_MAP_TARGET_1" "$LEGACY_MAP_TARGET_2"
sudo install -D -m 644 "$source_dir/assets/ayaneo3-companion.yaml" "$MAP_TARGET"
sudo systemctl restart inputplumber.service
sudo systemctl start plugin_loader.service
loader_stopped=false

printf 'Installed AYANEO 3 Companion %s. Return to Game Mode and enable Fix Key Binding if needed.\n' "$version"
