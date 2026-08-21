#!/usr/bin/env python3
"""Build a validated Cirrus Smart Amp EFI calibration payload.

This utility only writes a regular output file. It deliberately has no code for
writing efivarfs, so preparing and reviewing a candidate cannot modify firmware.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import pathlib
import struct
import sys


EFI_ATTRIBUTES_SIZE = 4
EFI_HEADER = struct.Struct("<II")
CAL_RECORD = struct.Struct("<QQbBH")
EXPECTED_ATTRIBUTES = 0x00000007
EXPECTED_COUNT = 2
EXPECTED_PAYLOAD_SIZE = EFI_HEADER.size + EXPECTED_COUNT * CAL_RECORD.size
FILETIME_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)


def parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def datetime_to_filetime(value: dt.datetime) -> int:
    delta = value.astimezone(dt.timezone.utc) - FILETIME_EPOCH
    return (
        delta.days * 86400 * 10_000_000
        + delta.seconds * 10_000_000
        + delta.microseconds * 10
    )


def decode_payload(blob: bytes) -> tuple[int, int, list[tuple[int, int, int, int, int]]]:
    if len(blob) != EFI_ATTRIBUTES_SIZE + EXPECTED_PAYLOAD_SIZE:
        raise ValueError(f"expected 52 bytes, got {len(blob)}")

    attributes = struct.unpack_from("<I", blob, 0)[0]
    size, count = EFI_HEADER.unpack_from(blob, EFI_ATTRIBUTES_SIZE)
    if attributes != EXPECTED_ATTRIBUTES:
        raise ValueError(f"unexpected EFI attributes: 0x{attributes:08x}")
    if size != EXPECTED_PAYLOAD_SIZE or count != EXPECTED_COUNT:
        raise ValueError(f"unexpected header: size={size}, count={count}")

    records = []
    offset = EFI_ATTRIBUTES_SIZE + EFI_HEADER.size
    for index in range(count):
        record = CAL_RECORD.unpack_from(blob, offset + index * CAL_RECORD.size)
        target, timestamp, ambient, status, cal_r = record
        if target == 0:
            raise ValueError(f"amplifier {index} has an empty target UID")
        if timestamp == 0:
            raise ValueError(f"amplifier {index} has an empty calibration timestamp")
        if status != 1:
            raise ValueError(f"amplifier {index} has invalid status {status}")
        if cal_r == 0:
            raise ValueError(f"amplifier {index} has an empty CAL_R")
        records.append(record)

    return size, count, records


def build_candidate(
    original: bytes,
    cal_r_values: tuple[int, int],
    ambient: int,
    timestamp: dt.datetime,
) -> bytes:
    size, count, records = decode_payload(original)
    if not -128 <= ambient <= 127:
        raise ValueError("ambient must fit in a signed 8-bit field")
    if any(not 1 <= value <= 0xFFFF for value in cal_r_values):
        raise ValueError("CAL_R values must be between 1 and 65535")

    candidate = bytearray(original)
    filetime = datetime_to_filetime(timestamp)
    offset = EFI_ATTRIBUTES_SIZE + EFI_HEADER.size
    for index, ((target, _, _, _, _), cal_r) in enumerate(zip(records, cal_r_values)):
        CAL_RECORD.pack_into(
            candidate,
            offset + index * CAL_RECORD.size,
            target,
            filetime,
            ambient,
            1,
            cal_r,
        )

    # Re-parse the output to catch accidental layout changes before writing it.
    output_size, output_count, output_records = decode_payload(bytes(candidate))
    if output_size != size or output_count != count:
        raise AssertionError("candidate header changed unexpectedly")
    if [record[0] for record in output_records] != [record[0] for record in records]:
        raise AssertionError("candidate target UIDs changed unexpectedly")
    return bytes(candidate)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=pathlib.Path, help="52-byte efivarfs backup")
    parser.add_argument("output", type=pathlib.Path, help="regular file to create")
    parser.add_argument("--left", type=int, required=True, help="left amplifier CAL_R")
    parser.add_argument("--right", type=int, required=True, help="right amplifier CAL_R")
    parser.add_argument("--ambient", type=int, required=True, help="ambient temperature in Celsius")
    parser.add_argument("--timestamp", type=parse_timestamp, required=True)
    args = parser.parse_args()

    output_resolved = args.output.resolve()
    if str(output_resolved).startswith("/sys/firmware/efi"):
        parser.error("refusing to write directly to efivarfs")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")

    original = args.original.read_bytes()
    candidate = build_candidate(
        original,
        (args.left, args.right),
        args.ambient,
        args.timestamp,
    )
    args.output.write_bytes(candidate)
    print(f"Wrote {len(candidate)} bytes to {args.output}")
    print(f"SHA256 {hashlib.sha256(candidate).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
