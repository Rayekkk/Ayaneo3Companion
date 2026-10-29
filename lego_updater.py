# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Kept only so that new releases stay downloadable and installable from older versions.

The updaters of 1.1.0 to 1.3.0 offer a release only when its ZIP contains this file, and bootstrap.sh of 1.2.0
and 1.3.0 extracts it next to bootstrap_installer.py (both by name). Updates are dk_update.py's job now and the
installer loads dk_update itself; nothing imports this module.
"""
