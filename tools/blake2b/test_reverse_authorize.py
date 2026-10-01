"""Automatic spending requires an exact operator authorization; recovery does not."""
import copy
import json
import unittest
from unittest.mock import patch

import reverse_authorize as auth
import reverse_controller as controller
import reverse_live as live
import reverse_service as service
import test_reverse_live as fixture
import test_reverse_quote_api as api_fixture
from reverse_quote_api import Quotes
from service_manager import private_load
from swap_controller import save


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LiveTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.settings = dict(self.f.settings, swap_root=str(self.f.root.parent))
        self.flag = patch.object(live, 'LIVE_EXECUTION_ENABLED', True)
        self.flag.start()
        self.addCleanup(self.flag.stop)

    def process(self):
        with patch.object(controller.RPC, 'call', side_effect=self.f.rpc):
            return auth.process_record(self.f.root, self.settings, rpc=self.f.rpc)

    def test_default_still_requires_manual_start(self):
        self.assertEqual(self.process()['outcome'], 'needs_manual_start')
        self.assertFalse(self.f.payments)

    def test_authorized_worker_sends_once_and_recovers(self):
        result = auth.authorize(self.f.root, self.settings)
        self.assertFalse(result['payment_started'])
        self.assertEqual(self.f.calls, [])
        self.assertEqual(self.process()['outcome'], 'pending')
        self.assertEqual(self.process()['outcome'], 'pending')
        self.f.payments[0].update(status='complete', payment_preimage=self.f.preimage)
        self.assertEqual(self.process()['phase'], 'xbt_released')
        self.assertEqual(self.process()['phase'], 'xbt_released')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.f.calls), 1)

    def test_expired_authorization_never_submits(self):
        auth.authorize(self.f.root, self.settings)
        with patch('reverse_authorize.time.time', return_value=self.f.terms['expires_at']):
            self.assertEqual(self.process()['outcome'], 'authorization_expired')
        self.assertEqual(self.f.calls, [])
        self.assertEqual(private_load(self.f.path)['phase'], 'prepared')

    def test_changed_quote_or_permit_refused_without_spend(self):
        auth.authorize(self.f.root, self.settings)
        path = self.f.root/'reverse-authorization.json'
        original = private_load(path)
        save(path, dict(original, quote_sha256='00'*32))
        with self.assertRaises(ValueError):
            self.process()
        save(path, original)
        quote = copy.deepcopy(self.f.quote)
        quote['inspection'] = {'changed': True}
        save(self.f.root/'reverse-quote.json', quote)
        with self.assertRaises(ValueError):
            self.process()
        self.assertEqual(self.f.calls, [])

    def test_started_payment_recovers_without_authorization(self):
        auth.authorize(self.f.root, self.settings)
        self.process()
        (self.f.root/'reverse-authorization.json').unlink()
        self.f.payments[0].update(status='complete', payment_preimage=self.f.preimage)
        self.assertEqual(self.process()['phase'], 'xbt_released')
        self.assertEqual(sum(m == 'sendpay' for m, _ in self.f.calls), 1)

    def test_wrong_directory_or_operator_cannot_authorize(self):
        for settings in (dict(self.settings, swap_root=str(self.f.root)),
                         dict(self.settings, receiver_id=fixture.D)):
            with self.assertRaises(ValueError):
                auth.authorize(self.f.root, settings)
        self.assertFalse((self.f.root/'reverse-authorization.json').exists())

    def test_step_rechecks_exact_quote_inside_service_lock(self):
        with self.assertRaises(ValueError):
            service.step(self.f.root, rpc=self.f.rpc, authorized_digest='00'*32)
        self.assertEqual(self.f.calls, [])

    def test_api_optin_is_pinned_before_quote_and_does_not_upgrade_old_requests(self):
        f = api_fixture.ApiTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.api.quote(f.body)
        with patch('reverse_authorize.authorize') as authorize:
            Quotes(f.settings, creator=f.creator, auto_process=True).quote(f.body)
            authorize.assert_not_called()
            newer = dict(f.body, request_id='22'*16)
            Quotes(f.settings, creator=f.creator, auto_process=True).quote(newer)
            authorize.assert_called_once()
            authorize.reset_mock()
            Quotes(f.settings, creator=f.creator).quote(newer)
            authorize.assert_not_called()
        stored = private_load(f.api.records/('22'*16+'.json'))
        self.assertTrue(stored['auto_process'])


if __name__ == '__main__':
    unittest.main()
