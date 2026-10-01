"""Transport contract, no mutation retries, and runtime/harness separation."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from swap_rpc import RPC, wait_until


class RpcTests(unittest.TestCase):
    def test_argv_is_not_interpreted_by_shell(self):
        value = 'space " quote $HOME $(not-a-command) ; test'
        result = RPC.call([sys.executable, '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))'], value, 7)
        self.assertEqual(result, [value, '7'])

    def test_json_null_empty_and_text_results_preserved(self):
        for output, expected in (('{"ok":true}', {'ok':True}), ('null', None),
                                  ('\n', None), ('  raw-result\n', 'raw-result'), ('12', 12)):
            result = subprocess.CompletedProcess(['cli'], 0, output, '')
            with patch('swap_rpc.subprocess.run', return_value=result) as call:
                self.assertEqual(RPC.call(['cli'], 'method'), expected)
                self.assertEqual(call.call_args.kwargs['timeout'], 20)

    def test_nonzero_rpc_error_is_never_retried(self):
        result = subprocess.CompletedProcess(['cli', 'sendpay'], 1, 'private-output', 'private-error')
        with patch('swap_rpc.subprocess.run', return_value=result) as call:
            with self.assertRaises(subprocess.CalledProcessError):
                RPC.call(['cli'], 'sendpay')
        call.assert_called_once()

    def test_timeout_is_never_retried(self):
        with patch('swap_rpc.subprocess.run', side_effect=subprocess.TimeoutExpired(['cli'], 20)) as call:
            with self.assertRaises(subprocess.TimeoutExpired):
                RPC.call(['cli'], 'sendpay')
        call.assert_called_once()

    def test_wait_does_not_disclose_failed_rpc_arguments(self):
        fn = Mock(side_effect=subprocess.CalledProcessError(1, ['SECRET']))
        with patch('swap_rpc.time.monotonic', side_effect=[0, 0, 1]), patch('swap_rpc.time.sleep'):
            with self.assertRaises(RuntimeError) as error:
                wait_until(fn, timeout=0.5)
        self.assertNotIn('SECRET', str(error.exception))

    def test_runtime_imports_do_not_load_regtest_harness(self):
        code = '''
import sys
import swap_controller, swap_service, swap_watch, quote_plugin
import market_check, market_setup, market_policy, receive_workflow
import service_manager, service_runtime, remote_receiver
assert not any(n in sys.modules for n in ('smoke_regtest', 'funded_regtest', 'swap_regtest', 'regression'))
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
