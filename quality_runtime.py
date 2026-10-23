# SPDX-License-Identifier: BSD-3-Clause
"""Keep Decky calls and their workers alive until hardware transactions finish."""
import asyncio
import functools
import inspect
import time

import diagnostics


async def complete(operation):
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                try:
                    result = task.result()
                except Exception:
                    # Retrieve the worker error, but preserve the caller's
                    # cancellation so a monitor cannot catch it and retry.
                    raise asyncio.CancelledError from None
                break
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


READ_CALLS = frozenset({'get_state', 'get_battery_status', 'get_fan_status',
                        'get_version', 'get_game_profile'})
NETWORK_CALLS = frozenset({'check_for_updates', 'perform_update'})
# Polled every few seconds: log health transitions only, never each call.
QUIET_CALLS = READ_CALLS | {'set_active_app'}


def _logged_rpc(function, name):
    """Observe the existing boundary; never retry, cancel or change its result."""
    quiet = name in QUIET_CALLS

    @functools.wraps(function)
    async def logged(self, *args, **kwargs):
        with diagnostics.operation(name=name):
            started = time.monotonic()

            def duration():
                return round((time.monotonic() - started) * 1000, 1)

            def failed(**fields):
                if quiet:
                    diagnostics.transition('plugin', 'rpc_health', 'failed', level='warning',
                                           operation=name, duration_ms=duration(), **fields)
                else:
                    diagnostics.event('plugin', 'rpc_failed', level='warning', operation=name,
                                      outcome='not_confirmed', duration_ms=duration(), **fields)
            if not quiet:
                diagnostics.event('plugin', 'rpc_started', operation=name)
            try:
                result = await function(self, *args, **kwargs)
            except asyncio.CancelledError:
                # The underlying admitted transaction may still be draining.
                diagnostics.event('plugin', 'rpc_waiter_cancelled', operation=name,
                                  outcome='refresh_required', repeat=60)
                raise
            except Exception as error:
                failed(error=error)
                raise
            if isinstance(result, dict) and result.get('success') is False:
                failed(reason='backend_reported_failure')
            elif quiet:
                diagnostics.transition('plugin', 'rpc_health', 'ok', operation=name, initial=False)
            else:
                diagnostics.event('plugin', 'rpc_completed', operation=name, duration_ms=duration(),
                                  outcome='succeeded' if isinstance(result, dict)
                                  and result.get('success') is True else 'returned')
            return result
    return logged


def guard_public_calls(cls):
    for name, function in list(vars(cls).items()):
        if name.startswith('_') or not inspect.iscoroutinefunction(function):
            continue

        def wrap(fn, method):
            @functools.wraps(fn)
            async def guarded(self, *args, **kwargs):
                # A public action may call another public action or fetch its
                # final status. Preserve its admission across shutdown.
                if asyncio.current_task() is self._rpc_owner:
                    return await fn(self, *args, **kwargs)
                if self._closing:
                    raise RuntimeError('Plugin is shutting down')

                async def execute():
                    if method in READ_CALLS or method in NETWORK_CALLS:
                        return await fn(self, *args, **kwargs)
                    if method == 'set_fan':
                        # This operation has its own hardware lock and touches
                        # no shared settings. Serialize fan commands together,
                        # retaining admission checks and shutdown tracking.
                        async with self._fan_rpc_lock:
                            if self._closing:
                                raise RuntimeError('Plugin is shutting down')
                            self._check_write_allowed()
                            return await fn(self, *args, **kwargs)
                    async with self._rpc_lock:
                        if self._closing:
                            raise RuntimeError('Plugin is shutting down')
                        self._check_write_allowed()
                        self._rpc_owner = asyncio.current_task()
                        try:
                            return await fn(self, *args, **kwargs)
                        finally:
                            self._rpc_owner = None

                task = asyncio.create_task(execute())
                self._rpc_jobs.add(task)
                task.add_done_callback(self._rpc_jobs.discard)
                return await complete(task)
            return _logged_rpc(guarded, name)
        setattr(cls, name, wrap(function, name))
    return cls
