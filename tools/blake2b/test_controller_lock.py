"""Exercise controller locking with real processes and atomic state replacement."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from smoke_regtest import wait_until
from swap_controller import run, save


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        save(self.path, {'phase': 'btc_released', 'payment_hash': '11' * 32,
                         'preimage': '22' * 32})

    def test_busy_across_state_replace_and_release_after_sigkill(self):
        ready = self.path.with_suffix('.ready')
        code = '''
import json, sys, time
from pathlib import Path
import swap_controller as controller
def hold(path, *args):
    controller.save(path, json.loads(path.read_text()))
    path.with_suffix('.ready').write_text('ready')
    time.sleep(60)
controller.run_locked = hold
controller.run(Path(sys.argv[1]))
'''
        proc = subprocess.Popen([sys.executable, '-c', code, str(self.path)],
                                cwd=Path(__file__).resolve().parent,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            wait_until(ready.exists, proc, timeout=5)
            lockfile = Path(str(self.path) + '.lock')
            inode = lockfile.stat().st_ino
            with patch('swap_controller.run_locked', side_effect=AssertionError('busy entered controller')):
                self.assertEqual(run(self.path), {'outcome': 'busy'})
                alias = self.path.with_name('alias.json')
                alias.symlink_to(self.path)
                self.assertEqual(run(alias), {'outcome': 'busy'})
            proc.kill()
            self.assertEqual(proc.wait(timeout=5), -9)
            self.assertEqual(run(self.path)['phase'], 'btc_released')
            self.assertEqual(lockfile.stat().st_ino, inode)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stderr.close()

    def test_exception_releases_lock(self):
        with patch('swap_controller.run_locked', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                run(self.path)
        self.assertEqual(run(self.path)['phase'], 'btc_released')


if __name__ == '__main__':
    unittest.main()
