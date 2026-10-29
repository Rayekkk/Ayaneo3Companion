#!/usr/bin/env bash
# SPDX-License-Identifier: BSD-3-Clause
set -Eeuo pipefail

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  printf 'Usage: %s [Ayaneo3Companion-version.zip]\n' "${0##*/}"
  printf 'Installs into the plugin directory configured by Decky; rolls back failed updates.\n'
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
trap 'rm -rf -- "$work_dir"' EXIT
# Read only the installer and its validator into a private temporary directory.
# The helper validates the whole ZIP before extracting or stopping services.
python3 - "$archive" "$work_dir" <<'PY'
import pathlib, stat, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    for name in ("bootstrap_installer.py", "dk_update.py", "dk_net.py", "dk_fs.py", "dk_process.py"):
        key = "Ayaneo3Companion/" + name
        matches = [entry for entry in archive.infolist() if entry.filename == key]
        if len(matches) != 1:
            raise ValueError("Missing or duplicate installer file: " + name)
        entry = matches[0]
        mode = entry.external_attr >> 16
        if entry.is_dir() or not 0 < entry.file_size <= 1024 * 1024 or (
                stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
            raise ValueError("Invalid installer file: " + name)
        (pathlib.Path(sys.argv[2]) / name).write_bytes(archive.read(entry))
PY
python3 "$work_dir/bootstrap_installer.py" --archive "$archive" --check
sudo python3 "$work_dir/bootstrap_installer.py" --archive "$archive"
