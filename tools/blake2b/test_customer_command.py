"""Customer command intent persistence and read-only inspection boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import customer as c
from swap_controller import save


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'managed'
        self.intent = dict(id='receive-coffee', direction='receive', cli=['wallet'],
                           customer_id='customer', token_file='/private/token', url='http://127.0.0.1:19840',
                           xbt_sats=325000, max_btc_sats=1480)
        self.rpc = lambda cli, method, *args: {'network': 'xbt', 'id': 'customer'}
        self.emit = lambda value: None

    def test_saved_intent_precedes_workflow_and_repeat_uses_same_directory(self):
        def check(cli, directory, credentials, url, amount, cap, **kwargs):
            self.assertEqual(c.private_load(directory.parent/'intent.json'), self.intent)
            return {'outcome': 'awaiting_btc'}
        with patch.object(c, 'private_load', wraps=c.private_load) as load, \
                patch.object(c.customer_receive, 'workflow', side_effect=check) as workflow:
            load.side_effect = lambda path: {} if str(path) == '/private/token' else json.loads(path.read_text())
            for _ in range(2):
                c.start(self.root, self.intent, self.rpc, self.emit)
            self.assertEqual(workflow.call_args_list[0], workflow.call_args_list[1])
        self.assertEqual((self.root/'receive-coffee/intent.json').stat().st_mode & 0o077, 0)

    def test_changed_amount_cap_wallet_or_endpoint_refused(self):
        with patch.object(c, 'execute', return_value={}):
            c.start(self.root, self.intent, self.rpc, self.emit)
        for key, value in [('xbt_sats', 1), ('max_btc_sats', 2), ('cli', ['other']),
                           ('url', 'http://127.0.0.1:1'), ('customer_id', 'other')]:
            changed = dict(self.intent, **{key: value})
            with patch.object(c, 'execute') as execute, self.assertRaises(ValueError):
                c.start(self.root, changed, self.rpc, self.emit)
            execute.assert_not_called()

    def test_resume_uses_saved_intent_and_explicit_retry(self):
        with patch.object(c, 'execute', return_value={}):
            c.start(self.root, self.intent, self.rpc, self.emit)
        with patch.object(c, 'execute', return_value={}) as execute:
            c.resume(self.root, 'receive-coffee', self.rpc, self.emit, retry=True)
            self.assertEqual(execute.call_args.args[1], self.intent)
            self.assertTrue(execute.call_args.args[-1])

    def test_send_delegates_confirmation_and_original_limits(self):
        intent = dict(self.intent, direction='send', id='send-one', invoice='private-invoice',
                      max_xbt_sats=400000, max_delay=2016)
        confirm = lambda prompt: 'PAY'
        with patch.object(c.customer_swap, 'workflow', return_value={'outcome': 'complete'}) as workflow:
            c.start(self.root, intent, self.rpc, self.emit, confirm)
        self.assertEqual(workflow.call_args.args[0], 'private-invoice')
        self.assertIs(workflow.call_args.kwargs['confirm'], confirm)
        self.assertEqual(workflow.call_args.args[-2:], (400000, 2016))

    def test_wrong_wallet_on_resume_never_enters_workflow(self):
        with patch.object(c, 'execute', return_value={}):
            c.start(self.root, self.intent, self.rpc, self.emit)
        with patch.object(c.customer_receive, 'workflow') as workflow, self.assertRaises(ValueError):
            c.resume(self.root, self.intent['id'], lambda *a: dict(network='xbt', id='other'), self.emit)
        workflow.assert_not_called()

    def test_competing_command_refused_before_workflow(self):
        c.private_directory(self.root, create=True)
        with c.locked(self.root), patch.object(c, 'execute') as execute, self.assertRaises(BlockingIOError):
            c.start(self.root, self.intent, self.rpc, self.emit)
        execute.assert_not_called()

    def test_traversal_and_symlink_refused(self):
        for name in ('../secret', 'a/b', '.', '', 'UPPER'):
            with self.assertRaises(ValueError):
                c.identifier(name)
        self.root.symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            c.status(self.root)

    def test_empty_status_does_not_create_root_or_call_rpc(self):
        def fail(*a):
            self.fail('unexpected RPC')
        self.assertEqual(c.status(self.root, rpc=fail), dict(read_only=True, attempts=[]))
        self.assertFalse(self.root.exists())

    def test_unstarted_status_never_resumes_or_writes(self):
        with patch.object(c, 'execute', return_value={}):
            c.start(self.root, self.intent, self.rpc, self.emit)
        before = (self.root/'receive-coffee/intent.json').read_bytes()
        with patch.object(c, 'execute') as execute:
            answer = c.status(self.root, rpc=self.rpc)
        execute.assert_not_called()
        self.assertEqual(answer['attempts'][0]['outcome'], 'not_started')
        self.assertEqual((self.root/'receive-coffee/intent.json').read_bytes(), before)

    def test_receiving_status_only_reads_wallet_and_omits_secrets(self):
        work = c.private_directory(self.root, create=True)
        state = dict(cli=['wallet'], customer_id='customer', label='private-label', xbt_sats=325000,
                     xbt_invoice='private-invoice', offer=dict(expires_at=100, btc_sats=1470, btc_invoice='secret'))
        save(work/'receive.json', state)
        before = (work/'receive.json').read_bytes()
        calls = []
        def rpc(cli, method, *args):
            calls.append(method)
            if method == 'getinfo':
                return self.rpc(cli, method)
            self.assertEqual(method, 'listinvoices')
            return dict(invoices=[dict(amount_msat=325000000, bolt11='private-invoice',
                         status='paid', amount_received_msat=325000000)])
        answer = c.inspect(work, rpc)
        self.assertEqual(answer, dict(outcome='paid', received_xbt_sats=325000))
        self.assertEqual(calls, ['getinfo', 'listinvoices'])
        self.assertEqual((work/'receive.json').read_bytes(), before)

    def test_missing_receiving_invoice_never_recreates(self):
        work = c.private_directory(self.root, create=True)
        save(work/'receive.json', dict(cli=['wallet'], customer_id='customer', label='private'))
        def rpc(cli, method, *args):
            if method == 'getinfo':
                return self.rpc(cli, method)
            self.assertEqual(method, 'listinvoices')
            return {'invoices': []}
        self.assertEqual(c.inspect(work, rpc)['outcome'], 'wallet_invoice_missing')

    def test_submitted_status_uses_existing_proof_checker_without_workflow(self):
        work = c.private_directory(self.root, create=True)
        c.private_directory(work/'wallet', create=True)
        save(work/'wallet/customer.json', {'phase': 'submitted'})
        with patch.object(c, 'result', return_value={'outcome': 'pending'}) as result, \
                patch.object(c.customer_swap, 'workflow') as workflow:
            self.assertEqual(c.inspect(work, self.rpc), {'outcome': 'pending'})
        result.assert_called_once_with({'phase': 'submitted'}, self.rpc)
        workflow.assert_not_called()


if __name__ == '__main__':
    unittest.main()
