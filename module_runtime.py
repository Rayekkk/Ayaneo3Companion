# SPDX-License-Identifier: BSD-3-Clause
"""Drain hardware work before withdrawing a module, including cancelled workers.

Shared with LegionGo2Companion's module_runtime; the Legion's device tables and
its controller-firmware maintenance pause stay there.
"""
import asyncio
import contextlib
import contextvars
import os
import stat
import threading
import time

from diagnostics import event
from quality_runtime import complete  # noqa: F401  (re-exported for the ported modules)

DMI_ROOT = "/sys/class/dmi/id"

_workers = {}
_starting = contextvars.ContextVar('starting_module', default=None)


class StartStopped(RuntimeError):
    """A stop came while this module was starting; its start makes no further firmware call."""


class _FirmwareLock:
    """A start whose stop was requested finishes the firmware call in flight but
    begins no other: each call can take seconds, and Decky SIGKILLs a stop still
    waiting on the start's thread after 5 s."""

    def __init__(self):
        self._lock = threading.Lock()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self._lock.acquire()
        if _starting.get() in _stop_requested:
            self._lock.release()
            raise StartStopped('Stopped while starting; no further firmware call was made.')

    def __exit__(self, *exc_info):
        self._lock.release()


# Hold only around one firmware-backed write (SMU limits, EC), innermost, from
# worker threads only: never take another lock or block on I/O beyond it.
firmware_lock = _FirmwareLock()


class AdmissionClosed(RuntimeError):
    """A refused call: shutdown or startup closed admission and already reports why."""


_stop_requested = set()


def request_stop(module):
    """A stop came while the module was still starting: its start must apply nothing more."""
    _stop_requested.add(module)


def stop_requested(module):
    return module in _stop_requested


def begin_start(module):
    _stop_requested.discard(module)


@contextlib.contextmanager
def starting(module):
    """Mark a module's start; its threads and tasks inherit the mark (firmware_lock)."""
    token = _starting.set(module)
    try:
        yield
    finally:
        _starting.reset(token)


def background_task(operation):
    """Start a long-lived monitor outside a start's stop mark (firmware_lock); a failure is logged."""
    context = contextvars.copy_context()
    context.run(_starting.set, None)
    task = context.run(asyncio.create_task, operation)

    def completed(worker):
        if not worker.cancelled():
            error = worker.exception()
            if error is not None:
                event('runtime', 'background_task_failed', level='error', error=error, repeat=60)
    task.add_done_callback(completed)
    return task


async def offload(module, function, *args, **kwargs):
    # Registration contains no await: queued/running threads remain visible to drain.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    pending = _workers.setdefault(module, set())
    pending.add(task)

    def completed(worker):
        pending.discard(worker)
        if not worker.cancelled():
            error = worker.exception()  # Also after the caller was cancelled.
            if error is not None:
                event(module, 'worker_failed', level='warning', error=error, repeat=60)
    task.add_done_callback(completed)
    # Task cancellation cannot cancel the underlying worker or hide it from drain.
    return await asyncio.shield(task)


async def drain(module):
    while _workers.get(module):
        # Cancelling gather directly would cancel the Task around to_thread,
        # remove it from the registry and leave its actual thread running.
        workers = tuple(_workers[module])
        await complete(asyncio.gather(*workers, return_exceptions=True))
        _workers[module].difference_update(worker for worker in workers if worker.done())


def read_sysfs_text(path, limit=512, *, error=OSError, subject=None):
    """Read a small ASCII sysfs attribute without following a final symlink.

    Anything that is not a regular file, or is longer than `limit`, raises
    `error`; a missing file stays the OSError from open, bad bytes a UnicodeError.
    """
    subject = subject or f"sysfs attribute {path}"
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise error(f"{subject} changed during access.")
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise error(f"{subject} returned an oversized value.")
    return raw.decode("ascii", errors="strict").strip()


def suspend_offset():
    """CLOCK_BOOTTIME - CLOCK_MONOTONIC: grows by the time spent suspended."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None or not hasattr(time, "clock_gettime"):
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


class ResumeWatch:
    """Calling it reports whether at least a second of suspend passed since the
    last reading; an unreadable clock (None) keeps that baseline."""
    last = None

    def mark(self):
        self.last = suspend_offset()

    def __call__(self):
        current = suspend_offset()
        if current is None:
            return False
        previous, self.last = self.last, current
        return previous is not None and current - previous >= 1.0


class NotConfirmed(RuntimeError):
    """A backend reply that did not confirm success; ``code`` is its ``error`` field, if any."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def require_success(result):
    if not isinstance(result, dict) or result.get('success') is not True:
        detail = result if isinstance(result, dict) else {}
        raise NotConfirmed(detail.get('error') or detail.get('message')
                           or detail.get('stderr') or 'Hardware restoration was not confirmed.',
                           detail.get('error'))
