# SPDX-License-Identifier: BSD-3-Clause
"""Decky's SettingsManager API for a root plugin, on DeckyKit's dk_fs.

dk_fs does the file work: atomic private writes with fsync, symlinks and anything but a regular file refused, a
.last-good copy, a damaged file kept aside as .corrupt-<time>, "lost" when no good copy remains. This layer keeps
what the plugin relies on: setSetting() stays in memory until commit(), a failed commit puts the last saved state
back (a refused hardware change can never reach the disk with the next commit), replace() is all-or-nothing,
every value is a copy, an unsafe path blocks every later access, and recovery_error is readable without the lock
(a slow fsync must not stall Decky's event loop).
"""

from __future__ import annotations

import copy
import os
import stat
import threading
from typing import Any

import dk_fs

MAX_SETTINGS_BYTES = 1024 * 1024
LEGACY_BACKUP = ".bak"  # the copy 1.3.0 and older kept: becomes .last-good once


class UnsafeSettingsPath(RuntimeError):
    """The configured settings path is not a regular file/directory."""


class CorruptSettings(RuntimeError):
    """A settings file exists but does not contain a JSON object."""


def _log(level: str, message: str) -> None:
    try:
        import decky

        logger = getattr(decky, "logger", None)
        writer = getattr(logger, level, None)
        if callable(writer):
            writer(f"[ayaneo3companion-settings] {message}")
    except Exception:
        pass  # logging must never break settings access


class AtomicSettingsManager:
    """Small SettingsManager-compatible store with atomic explicit commits."""

    def __init__(self, name: str, settings_directory: str) -> None:
        if (not isinstance(name, str) or not name or name in (".", "..")
                or os.path.basename(name) != name or "\x00" in name):
            raise ValueError("settings name must be one safe path component")
        self.directory = os.path.abspath(os.fspath(settings_directory))
        self.path = os.path.join(self.directory, name + ".json")
        self.backup_path = self.path + dk_fs.LAST_GOOD
        self.settings: dict[str, Any] = {}
        self._committed_settings: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._blocked_reason = ""
        # Missing settings are a first install; corrupt settings with no usable
        # recovery copy are lost user intent. Consumers such as module gating
        # must be able to distinguish those cases before applying defaults.
        self._recovery_error = ""
        try:
            self.read()
        except UnsafeSettingsPath as exc:
            # Keep the module importable so the other companion modules can run,
            # but fail every later access closed instead of touching the path.
            self._blocked_reason = str(exc)
            _log("error", self._blocked_reason)

    @property
    def data(self) -> dict[str, Any]:
        """Compatibility alias for callers that inspect the full snapshot."""
        with self._lock:
            self._ensure_safe()
            return copy.deepcopy(self.settings)

    @data.setter
    def data(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise TypeError("settings root must be an object")
        with self._lock:
            self._ensure_safe()
            self.settings = copy.deepcopy(payload)

    def _ensure_safe(self) -> None:
        if self._blocked_reason:
            raise UnsafeSettingsPath(self._blocked_reason)

    @property
    def recovery_error(self) -> str:
        """Why all saved copies were lost; empty for new or recovered storage."""
        # Publish an immutable string under the writer's lock, but let async
        # admission/status checks read it without waiting for commit's fsync.
        # Acquiring the settings lock here stalls Decky's entire event loop,
        # including independent fan recovery, while storage is slow.
        return self._recovery_error

    def _adopt_legacy_backup(self) -> None:
        """1.3.0 and older kept <file>.bak: it becomes the .last-good copy, so a damaged file right after the update
        still has its recovery copy; also after a downgrade and an update again, when it is the newer one."""
        legacy = self.path + LEGACY_BACKUP
        try:
            info = os.lstat(legacy)
            if not stat.S_ISREG(info.st_mode):
                return
            try:
                if os.lstat(self.backup_path).st_mtime >= info.st_mtime:
                    return
            except FileNotFoundError:
                pass
            os.replace(legacy, self.backup_path)
        except FileNotFoundError:
            return

    def read(self) -> None:
        with self._lock:
            self._ensure_safe()
            try:
                self._adopt_legacy_backup()
                loaded = dk_fs.load_json(self.path, {}, MAX_SETTINGS_BYTES, validate=dk_fs.require_object,
                                         no_links=True, mode=0o600)
            except dk_fs.UnsafePath as exc:
                raise UnsafeSettingsPath(str(exc)) from exc
            if loaded.status == "recovered":
                _log("warning", f"restored settings from the last good copy: {loaded.detail}")
            elif loaded.status == "lost":
                _log("warning", f"no usable settings copy remains: {loaded.detail}")
            self._recovery_error = (f"No usable settings copy remains ({loaded.detail})."
                                    if loaded.status == "lost" else "")
            self.settings = loaded.value
            self._committed_settings = copy.deepcopy(loaded.value)

    def getSetting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._ensure_safe()
            return copy.deepcopy(self.settings.get(key, default))

    def setSetting(self, key: str, value: Any) -> None:
        with self._lock:
            self._ensure_safe()
            self.settings[key] = copy.deepcopy(value)

    def commit(self) -> None:
        with self._lock:
            self._ensure_safe()
            snapshot = copy.deepcopy(self.settings)
            try:
                dk_fs.write_json(self.path, snapshot, 0o600, limit=MAX_SETTINGS_BYTES, validate=dk_fs.require_object,
                                 no_links=True, folder_mode=0o755)
            except Exception as exc:
                # A failed user operation must not remain visible through
                # getSetting() when the durable file still holds the previous
                # transaction.  This also makes a later unrelated commit unable
                # to smuggle the rejected values onto disk.
                self.settings = copy.deepcopy(self._committed_settings)
                if isinstance(exc, dk_fs.UnsafePath):
                    raise UnsafeSettingsPath(str(exc)) from exc
                raise
            self._committed_settings = copy.deepcopy(snapshot)
            self._recovery_error = ""

    def replace(self, payload: dict[str, Any]) -> None:
        """Atomically replace the complete object in one durable commit."""
        if not isinstance(payload, dict):
            raise TypeError("settings root must be an object")
        with self._lock:
            self._ensure_safe()
            previous = copy.deepcopy(self.settings)
            self.settings = copy.deepcopy(payload)
            try:
                self.commit()
            except Exception:
                self.settings = previous
                raise


# Drop-in name for the existing plugin settings API.
SettingsManager = AtomicSettingsManager
