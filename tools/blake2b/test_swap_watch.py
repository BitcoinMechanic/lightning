"""Recovery watcher retry, termination, and restart checks; no live nodes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from smoke_regtest import wait_until
from swap_controller import save
from swap_watch import watch


class WatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'state.json'
        save(self.path, {'phase': 'outgoing_started'})
        self.events = []
        self.stop = Mock()
        self.stop.is_set.return_value = False

    def test_rpc_errors_busy_and_pending_then_terminal_without_secret(self):
        secret = 'ab' * 32
        results = [subprocess.CalledProcessError(1, ['secret-cli'], stderr=secret),
                   subprocess.TimeoutExpired(['secret-cli'], 20), {'outcome': 'busy'},
                   {'phase': 'outgoing_started', 'outcome': 'pending'},
                   {'phase': 'btc_released', 'payment_preimage': secret}]
        before = self.path.read_bytes()
        with patch('swap_watch.run', side_effect=results) as run:
            self.assertEqual(watch(self.path, stop=self.stop, emit=self.events.append), 0)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(self.stop.wait.call_count, 4)
        self.assertNotIn(secret, json.dumps(self.events))
        self.assertEqual(self.path.read_bytes(), before)

    def test_failure_terminal_exits(self):
        with patch('swap_watch.run', return_value={'phase': 'btc_failed', 'outcome': 'failed'}) as run:
            self.assertEqual(watch(self.path, stop=self.stop, emit=self.events.append), 0)
        run.assert_called_once()
        self.stop.wait.assert_not_called()

    def test_invariant_error_is_not_retried(self):
        with patch('swap_watch.run', side_effect=RuntimeError('binding mismatch')) as run:
            with self.assertRaises(RuntimeError):
                watch(self.path, stop=self.stop, emit=self.events.append)
        run.assert_called_once()
        self.stop.wait.assert_not_called()

    def test_prepared_state_never_starts_payment(self):
        save(self.path, {'phase': 'prepared'})
        with patch('swap_watch.run') as run:
            with self.assertRaises(RuntimeError):
                watch(self.path, stop=self.stop, emit=self.events.append)
        run.assert_not_called()

    def test_interval_and_stop(self):
        for interval in (0, -1, float('nan'), float('inf'), 61):
            with self.assertRaises(ValueError):
                watch(self.path, interval, emit=self.events.append)
        stop = threading.Event()
        stop.set()
        with patch('swap_watch.run') as run:
            self.assertEqual(watch(self.path, stop=stop, emit=self.events.append), 0)
        run.assert_not_called()

    def test_sigterm_interrupts_long_poll_wait(self):
        log = self.path.with_suffix('.log')
        script = ('import swap_watch; '
                  'swap_watch.run = lambda path: {"phase":"outgoing_started", "outcome":"pending"}; '
                  'raise SystemExit(swap_watch.main())')
        before = self.path.read_bytes()
        with log.open('w') as output:
            proc = subprocess.Popen([sys.executable, '-c', script, '--state', str(self.path),
                                     '--interval', '60'], cwd=Path(__file__).parent,
                                    stdout=output, stderr=subprocess.STDOUT)
            try:
                wait_until(lambda: 'reconciled' in log.read_text(), proc, timeout=5)
                proc.terminate()
                self.assertEqual(proc.wait(timeout=5), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
        self.assertIn('stopped', log.read_text())
        self.assertEqual(self.path.read_bytes(), before)

    def test_terminal_restart_needs_no_rpc(self):
        save(self.path, {'phase': 'btc_failed', 'payment_hash': 'ab' * 32})
        for _ in range(2):
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('swap_watch.py')),
                                     '--state', str(self.path)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['phase'], 'btc_failed')


if __name__ == '__main__':
    unittest.main()
