import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import customer_swap as w
from swap_controller import save


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root/'swap'
        self.token = self.root/'token.json'
        save(self.token, dict(token='private'))
        self.offer = dict(btc_sats=1500, xbt_sats=350000, expires_at=9999999999)
        self.checked = dict(final_cltv=622, payment_hash='private')
        self.state = dict(phase='reviewed', btc_invoice='invoice', cli=['own-wallet'],
                          max_xbt_sats=400000, max_delay=2016, network='xbt',
                          offer=self.offer, **self.checked)
        self.output = []
        self.calls = []
        def request(invoice, credentials, url, directory, cap):
            self.calls.append('request')
            directory.mkdir(mode=0o700)
            save(directory/'offer.json', self.offer)
        def review(offer, invoice, cli, directory, cap, delay):
            self.calls.append('review')
            directory.mkdir(mode=0o700)
            save(directory/'customer.json', self.state)
            return dict(self.offer, final_cltv=622)
        for name, implementation in [('request_quote', request), ('review', review)]:
            self.addCleanup(patch.stopall)
            patch.object(w, name, side_effect=implementation).start()
        self.pay = patch.object(w, 'pay', return_value=dict(outcome='pending')).start()
        self.status = patch.object(w, 'result', return_value=dict(outcome='complete')).start()
        self.validation = patch.object(w, 'validate', return_value=self.checked).start()

    def run_flow(self, answer='PAY', **kw):
        return w.workflow(kw.get('invoice', 'invoice'), ['own-wallet'], self.directory,
                          self.token, 'http://127.0.0.1:19840', 400000,
                          confirm=lambda prompt: answer, emit=self.output.append)

    def test_confirmation_only_after_review(self):
        self.assertEqual(self.run_flow()['outcome'], 'pending')
        self.assertEqual(self.calls, ['request', 'review'])
        self.pay.assert_called_once_with(self.directory/'wallet')
        displayed = json.loads(self.output[0])
        self.assertEqual(set(displayed), {'btc_sats','xbt_sats','final_cltv','expires_at'})

    def test_decline_then_resume_without_another_quote(self):
        self.assertEqual(self.run_flow('no')['outcome'], 'not_submitted')
        self.pay.assert_not_called()
        self.run_flow()
        self.assertEqual(self.calls.count('request'), 1)
        self.validation.assert_called_once()
        self.pay.assert_called_once()

    def test_submitted_resume_only_queries_wallet(self):
        self.run_flow('no')
        self.state['phase'] = 'submitted'
        save(self.directory/'wallet/customer.json', self.state)
        self.token.unlink()
        self.assertEqual(self.run_flow()['outcome'], 'complete')
        self.pay.assert_not_called()
        self.status.assert_called_once()
        self.assertEqual(len(self.output), 1)
        self.assertEqual(self.calls.count('request'), 1)

    def test_changed_invoice_refused(self):
        self.run_flow('no')
        with self.assertRaises(ValueError):
            self.run_flow(invoice='different')
        self.pay.assert_not_called()

    def test_expired_review_never_prompts_or_pays(self):
        self.run_flow('no')
        self.validation.side_effect = ValueError('expired')
        with self.assertRaises(ValueError):
            self.run_flow()
        self.pay.assert_not_called()
        self.assertEqual(len(self.output), 1)

    def test_eof_does_not_pay(self):
        with self.assertRaises(EOFError):
            w.workflow('invoice', ['own-wallet'], self.directory, self.token,
                       'http://127.0.0.1:19840', 400000,
                       confirm=lambda prompt: (_ for _ in ()).throw(EOFError()), emit=self.output.append)
        self.pay.assert_not_called()

    def test_changed_wallet_binding_refused(self):
        self.run_flow('no')
        self.validation.return_value = dict(self.checked, payment_hash='changed')
        with self.assertRaises(ValueError):
            self.run_flow()
        self.pay.assert_not_called()


if __name__ == '__main__':
    unittest.main()
