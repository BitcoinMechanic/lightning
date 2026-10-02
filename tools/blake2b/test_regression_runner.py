"""Runner exit handling and parallel port reservations; no live nodes."""
from concurrent.futures import ThreadPoolExecutor
import os
import io
from contextlib import redirect_stdout, redirect_stderr
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

    def test_multi_receiver_accepts_runner_directory_and_preserves_failure_logs(self):
        import receive_multi_regtest as multi
        for fail in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)/'case'
                lab = MagicMock()
                def execute(instance, reject, any_btc):
                    self.assertIs(instance, lab)
                    self.assertTrue(reject)
                    self.assertTrue(any_btc)
                    (root/'retained.log').write_text('test diagnostic')
                    if fail:
                        raise RuntimeError('fixture failure')
                with patch.object(multi, 'Lab', return_value=lab), \
                        patch.object(multi, 'run', side_effect=execute), redirect_stdout(io.StringIO()):
                    args = ['--bitcoind', '/bitcoind', '--bitcoin-cli', '/bitcoin-cli',
                            '--work-dir', str(root), '--fail-second', '--any-btc']
                    if fail:
                        with self.assertRaises(RuntimeError): multi.main(args)
                    else:
                        multi.main(args)
                lab.close.assert_called_once()
                self.assertEqual((root/'retained.log').read_text(), 'test diagnostic')
                with patch.object(multi, 'Lab') as create, self.assertRaises(FileExistsError):
                    multi.main(args)
                create.assert_not_called()

    def test_multi_receiver_standalone_directory_is_temporary(self):
        import receive_multi_regtest as multi
        roots = []
        def create(root, *args):
            roots.append(root)
            return MagicMock()
        with patch.object(multi, 'Lab', side_effect=create), patch.object(multi, 'run'), \
                redirect_stdout(io.StringIO()):
            multi.main(['--bitcoind', '/bitcoind', '--bitcoin-cli', '/bitcoin-cli'])
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())

    def test_parallel_limit_accepts_32_and_refuses_outside_bounds(self):
        import regression
        with tempfile.TemporaryDirectory(prefix='cxj-') as temporary:
            case = Case('quick', (sys.executable, '-c', 'pass'))
            args = ['regression.py', '--jobs', '32', '--output', str(Path(temporary)/'out')]
            with patch.object(regression, 'cases', return_value=[case]), \
                    patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()):
                self.assertEqual(regression.main(), 0)
        for jobs in ('0', '33'):
            with patch.object(sys, 'argv', ['regression.py', '--jobs', jobs]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                regression.main()
            self.assertEqual(error.exception.code, 2)

    def test_catalog_unique(self):
        catalog = cases()
        self.assertEqual(len(catalog), len({case.name for case in catalog}))
        self.assertEqual(sum(c.live for c in catalog), 64)
        customers = [c for c in catalog if c.name.startswith('reverse-customer-routed-xbt-')]
        self.assertEqual(len(customers), 2)
        for case in customers:
            self.assertIn('--customer-routed-xbt', case.command)
            self.assertIn('--auto-process', case.command)
        routed = [c for c in catalog if c.name.startswith('reverse-routed-xbt-')]
        self.assertEqual(len(routed), 2)
        for case in routed:
            self.assertIn('--routed-xbt', case.command)
            self.assertIn('--auto-process', case.command)

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
