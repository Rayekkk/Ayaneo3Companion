# SPDX-License-Identifier: BSD-3-Clause
"""Keep Decky calls and their workers alive until hardware transactions finish."""
import asyncio
import functools


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
                result = task.result()
                break
    if cancelled:
        raise asyncio.CancelledError
    return result


READ_CALLS = frozenset({'get_state', 'get_battery_status', 'get_version', 'get_game_profile'})
NETWORK_CALLS = frozenset({'check_for_updates', 'perform_update'})


def guard_public_calls(cls):
    for name, function in list(vars(cls).items()):
        if name.startswith('_') or not asyncio.iscoroutinefunction(function):
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
            return guarded
        setattr(cls, name, wrap(function, name))
    return cls
