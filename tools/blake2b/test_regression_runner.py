"""Runner exit handling and parallel port reservations; no live nodes."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from regression import Case, Runner, cases
from smoke_regtest import Lab, wait_until


class RunnerTests(unittest.TestCase):
    def test_catalog_unique(self):
        catalog = cases()
        self.assertEqual(len(catalog), len({case.name for case in catalog}))
        self.assertEqual(sum(c.live for c in catalog), 23)

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
