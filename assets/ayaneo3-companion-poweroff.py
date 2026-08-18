#!/usr/bin/python3
"""Cut AYANEO 3 controller power during a real system shutdown."""

from pathlib import Path

POWER_REGISTER = 0x2D
POWER_OFF = 0xFE


def dmi(name: str) -> str:
    try:
        return Path(f"/sys/class/dmi/id/{name}").read_text().strip().upper()
    except OSError:
        return ""


if dmi("sys_vendor") == "AYANEO" and dmi("product_name") == "AYANEO 3":
    paths = sorted(Path("/sys/kernel/debug/ec").glob("ec*/io"))
    if paths:
        with paths[0].open("r+b", buffering=0) as ec:
            ec.seek(POWER_REGISTER)
            ec.write(bytes([POWER_OFF]))
