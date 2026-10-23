# SPDX-License-Identifier: BSD-3-Clause
"""Shared test setup: the isolated Decky harness and the plugin path, imported before any backend."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _harness import install  # noqa: E402

install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import controller_backend  # noqa: E402


def controller_report(config):
    """The AYA_CHECK answer of a controller running `config`."""
    packet = controller_backend.controller_command(config)
    response = bytearray(64)
    response[3] = controller_backend.AYA_CHECK[4]
    response[7:15] = packet[8:16]
    response[21:24] = packet[22:25]
    return bytes(response)
