"""Runner exit handling and parallel port reservations; no live nodes."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from regression import Case, Runner, cases
from smoke_regtest import Lab, wait_until


class RunnerTests(unittest.TestCase):
    def test_lightning_waits_for_peer_listener_after_rpc_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = Lab(Path(directory), '/bitcoind', '/bitcoin-cli')
            process = MagicMock()
            process.poll.return_value = None
            connection = MagicMock()
            with patch.object(lab, 'port', return_value=19735), \
                    patch.object(lab, 'start', return_value=process), \
                    patch.object(lab, 'rpc', return_value={'network': 'regtest', 'id': 'test'}) as rpc, \
                    patch('smoke_regtest.socket.create_connection',
                          side_effect=[ConnectionRefusedError(), connection]) as connect, \
                    patch('smoke_regtest.time.sleep'):
                node = lab.lightning('btc', 'regtest', {'data': '/backend', 'port': 18443})
            self.assertEqual(node['id'], 'test')
            self.assertEqual(rpc.call_count, 1)
            self.assertEqual(connect.call_count, 2)
            connect.assert_called_with(('127.0.0.1', 19735), timeout=1)
            connection.__exit__.assert_called_once()

    def test_catalog_unique(self):
        catalog = cases()
        self.assertEqual(len(catalog), len({case.name for case in catalog}))
        self.assertEqual(sum(c.live for c in catalog), 28)

    def test_exit_codes_and_missing_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner(Path(directory), [])
            for code, status in ((0, 'PASS'), (3, 'FAIL')):
                case = Case(f'exit-{code}', (sys.executable, '-c', f'print("test log"); raise SystemExit({code})'))
                result = runner.run_case(code, case)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['returncode'], code)
                self.assertIn('test log', Path(result['log']).read_text())
            result = runner.run_case(4, Case('missing', (str(Path(directory) / 'missing-executable'),)))
            self.assertEqual(result['status'], 'FAIL')

    def test_cancel_active_and_skip_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = Runner(root, [])
            ready = root / 'ready'
            script = 'from pathlib import Path; import time; Path(' + repr(str(ready)) + ').touch(); time.sleep(30)'
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(runner.run_case, 0, Case('waiting', (sys.executable, '-c', script)))
                try:
                    wait_until(ready.exists, timeout=5)
                finally:
                    runner.cancel()
                self.assertEqual(future.result(timeout=5)['status'], 'FAIL')
            result = runner.run_case(1, Case('skipped', ('must-not-execute',)))
            self.assertEqual(result['status'], 'NOT RUN')

    def test_shared_port_registry(self):
        # Exercise file locking/reservations independently of OS socket access.
        def select_port(lab):
            return next(p for p in range(30000, 30100) if p not in lab.ports)
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / 'ports.txt'
            with patch.dict(os.environ, {'CLN_BLAKE2B_TEST_PORTS': str(registry)}), \
                    patch.object(Lab, '_port', select_port):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    ports = list(pool.map(lambda _: Lab(Path(directory), '', '').port(), range(20)))
            self.assertEqual(len(set(ports)), 20)
            self.assertEqual(set(map(int, registry.read_text().splitlines())), set(ports))


if __name__ == '__main__':
    unittest.main()
