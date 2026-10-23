# SPDX-License-Identifier: BSD-3-Clause
"""Lifecycle diagnostics: Decky's stop hang, log pruning, runtime identity and RPC tracing."""
import asyncio
import collections
import errno
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_logic
import main
import companion_common
import diagnostics


class DeckyListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_decky_listener_ends_at_eof_instead_of_starving_unload(self):
        class Socket:  # Decky 3.2.10 UnixSocket's listener, with a bounded spin.
            async def _read_single_line(self, reader):
                try:
                    return (await reader.readuntil()).decode()
                except asyncio.IncompleteReadError as err:
                    return err.partial.decode()

            async def listen(self, reader, handled):
                for _ in range(1000):
                    line = await self._read_single_line(reader)
                    handled.append(line)
                return 'spun'
        socket = Socket()

        def closure(sandboxed):
            async def emit(event, *args):
                await sandboxed._socket.write(event)
            return emit
        reader, handled = asyncio.StreamReader(), []
        with patch.object(main.decky, 'emit', closure(SimpleNamespace(_socket=socket))):
            main.Plugin()
        reader.feed_data(b'{"id": 1}\n')
        reader.feed_eof()
        listener = asyncio.ensure_future(socket.listen(reader, handled))
        unload = asyncio.ensure_future(asyncio.sleep(0, 'unloaded'))  # Needs the loop to run again.
        await asyncio.wait([listener, unload])
        self.assertTrue(listener.cancelled())
        self.assertEqual(handled, ['{"id": 1}\n'])
        self.assertEqual(unload.result(), 'unloaded')

    async def test_decky_listener_that_cannot_be_found_is_reported(self):
        async def emit(_event, *_args):
            pass
        with patch.object(main.decky, 'emit', emit), patch.object(main.diagnostics, 'event') as event:
            main._end_decky_listener_at_eof()
        event.assert_called_once_with('plugin', 'decky_listener_unpatched', level='warning')


class RuntimeIdentityTests(unittest.TestCase):
    def test_runtime_event_identifies_device_firmware_and_software_without_ec_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, value in (('product_name', 'AYANEO 3'), ('bios_version', '3.02'),
                                ('ec_firmware_release', '0.21')):
                (root / name).write_text(value + '\n')
            (root / 'os-release').write_text('ID=steamos\nVERSION_ID=3.8.28\nBUILD_ID="20260922.1"\n')
            with patch.object(main.decky, 'DECKY_VERSION', 'v3.2.10', create=True):
                identity = main._runtime_identity(str(root), str(root / 'os-release'))
            self.assertEqual(identity, {'model': 'AYANEO 3', 'bios': '3.02', 'ec': '0.21', 'steamos': '3.8.28',
                                        'steamos_build': '20260922.1', 'decky': 'v3.2.10'})
            missing = main._runtime_identity(str(root / 'absent'), str(root / 'absent'))
        self.assertEqual(set(missing.values()), {None})


@unittest.skipUnless(os.name == 'posix', 'dir_fd and O_NOFOLLOW semantics')
class LogRotationTests(unittest.TestCase):
    """Decky chowns the plugin log directory to deck; root pruning must stay inside it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir()

    def make_logs(self, folder, count):
        for i in range(count):
            path = folder / f'{i:02}.log'
            path.write_text('x')
            os.utime(path, (1000 + i, 1000 + i))

    def rotate(self, log_dir, current='00.log'):
        with patch.object(main.decky, 'DECKY_PLUGIN_LOG_DIR', str(log_dir), create=True), \
                patch.object(main.decky, 'DECKY_PLUGIN_LOG', str(log_dir / current), create=True):
            main._rotate_logs(keep=10)

    def test_keeps_the_newest_ten_and_the_current_log(self):
        self.make_logs(self.logs, 13)
        (self.logs / 'notes.txt').write_text('keep')
        self.rotate(self.logs)
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()),
                         ['00.log'] + [f'{i:02}.log' for i in range(3, 13)] + ['notes.txt'])

    def test_a_log_that_disappears_during_the_scan_is_skipped_quietly(self):
        self.make_logs(self.logs, 13)
        real_stat = os.stat

        def vanishing(name, *args, **kwargs):
            if name == '05.log':
                raise FileNotFoundError(name)
            return real_stat(name, *args, **kwargs)
        with patch.object(main.os, 'stat', side_effect=vanishing), patch.object(main.diagnostics, 'event') as event:
            self.rotate(self.logs)
        event.assert_not_called()
        self.assertFalse((self.logs / '01.log').exists())   # the rest of the pass still ran

    def test_a_symlinked_log_directory_or_log_entry_is_never_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.make_logs(outside, 13)
        link = self.root / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        with patch.object(main.diagnostics, 'event'):
            self.rotate(link)
        self.assertEqual(len(list(outside.iterdir())), 13)
        self.make_logs(self.logs, 11)
        (self.logs / 'aa.log').symlink_to(outside / '05.log')
        os.utime(self.logs / 'aa.log', (1, 1), follow_symlinks=False)
        self.rotate(self.logs, current='none.log')
        self.assertTrue((outside / '05.log').exists())
        self.assertTrue((self.logs / 'aa.log').is_symlink())
        self.assertFalse((self.logs / '00.log').exists())


class RpcLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.records = []
        seen = patch.object(diagnostics, '_seen', collections.OrderedDict())
        seen.start()
        self.addCleanup(seen.stop)
        sink = patch.object(diagnostics, '_sink', lambda text, level: self.records.append(json.loads(text)))
        sink.start()
        self.addCleanup(sink.stop)
        state = patch.object(main.Plugin, '_state', {'supported': True})
        state.start()
        self.addCleanup(state.stop)
        self.plugin = main.Plugin()

    async def test_failure_and_success_keep_result_operation_and_privacy(self):
        update = AsyncMock(return_value={'success': False, 'error': 'token=SECRET'})
        with patch.object(companion_common, 'updater', SimpleNamespace(download=lambda _version: update.return_value)):
            failure = await self.plugin.perform_update('1.2.1')
        self.assertFalse(failure['success'])
        with patch.object(companion_common, 'updater', SimpleNamespace(
                download=lambda _version: {'success': True, 'private': 'not logged'})):
            success = await self.plugin.perform_update('1.2.1')
        self.assertTrue(success['success'])
        terminals = [r for r in self.records if r['event'] in ('rpc_failed', 'rpc_completed')]
        self.assertEqual([r['event'] for r in terminals], ['rpc_failed', 'rpc_completed'])
        self.assertNotEqual(terminals[0]['operation_id'], terminals[1]['operation_id'])
        self.assertNotIn('SECRET', json.dumps(self.records))
        self.assertNotIn('not logged', json.dumps(self.records))

    async def test_repeated_read_failure_and_recovery_are_coalesced(self):
        with patch.object(main.Plugin, '_snapshot', side_effect=ConnectionResetError(errno.ECONNRESET, 'peer reset')), \
             patch.object(self.plugin, '_schedule_tdp_diagnostic'):
            for _ in range(6):
                with self.assertRaises(ConnectionResetError):
                    await self.plugin.get_state()
        with patch.object(main.Plugin, '_snapshot', return_value={'supported': True}), \
             patch.object(self.plugin, '_schedule_tdp_diagnostic'):
            await self.plugin.get_state()
        health = [r for r in self.records if r['event'] == 'rpc_health']
        self.assertEqual([r['context']['status'] for r in health], ['failed', 'ok'])
        self.assertEqual(health[0]['error']['errno'], errno.ECONNRESET)
        self.assertFalse(any(r['event'] == 'rpc_started' for r in self.records))

    async def test_a_caller_that_goes_away_is_context_not_a_problem(self):
        with patch.object(self.plugin, '_offload', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin.perform_update('1.2.1')
        row, = [r for r in self.records if r['event'] == 'rpc_waiter_cancelled']
        self.assertEqual(row['level'], 'info')

    async def test_failed_logging_cannot_change_success_or_failure(self):
        with patch.object(diagnostics, '_sink', side_effect=OSError(errno.ENOSPC, 'full')), \
             patch.object(companion_common, 'updater', SimpleNamespace(download=lambda _v: {'success': True})):
            self.assertTrue((await self.plugin.perform_update('1.2.1'))['success'])
            with self.assertRaises(ValueError):
                await self.plugin.perform_update(None)

    async def test_uninstall_keeps_logging_after_inner_unload_until_cleanup_finishes(self):
        with patch.object(self.plugin, '_remove_owned', AsyncMock(side_effect=self.plugin._unload)):
            await self.plugin._uninstall()
        events = [r['event'] for r in self.records]
        for name in ('uninstall_started', 'shutdown_started', 'shutdown_completed', 'uninstall_completed'):
            self.assertIn(name, events)
        self.assertLess(events.index('shutdown_completed'), events.index('uninstall_completed'))
        self.assertFalse(self.plugin._uninstalling)


if __name__ == '__main__':
    unittest.main()
