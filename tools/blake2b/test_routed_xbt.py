"""Assertions for the routed XBT fixture; no node RPCs or payments."""
import copy
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from routed_xbt_regtest import receipt, route_ready, wait_for_route


def fixture():
    proof = 'ab'*32
    terms = dict(payment_hash=hashlib.sha256(bytes.fromhex(proof)).hexdigest(),
                 xbt_amount_msat=350000000,
                 timing=dict(proposed_xbt_invoice_cltv=622))
    quote = dict(terms=terms, xbt_invoice='lnxbtrt-fixture')
    row = dict(bolt11=quote['xbt_invoice'], destination='operator',
               payment_hash=terms['payment_hash'], amount_msat=350000000,
               amount_sent_msat=350005000, preimage=proof,
               number_of_parts=1, status='complete')
    route = dict(routes=[dict(amount_msat=350000000, final_cltv=622, path=[
        dict(node_id_in='payer', node_id_out='relay', amount_in_msat=350005000,
             amount_out_msat=350005000, cltv_in=628, cltv_out=628),
        dict(node_id_in='relay', node_id_out='operator', amount_in_msat=350005000,
             amount_out_msat=350000000, cltv_in=628, cltv_out=622)])])
    return quote, row, route


class RoutedTests(unittest.TestCase):
    def ready(self, route):
        return route_ready(route, 'payer', 'relay', 'operator', 350000000, 622)

    def test_two_hop_route_and_stale_fee(self):
        _, _, route = fixture()
        self.assertTrue(self.ready(route))
        for index, key, value in [(0, 'node_id_out', 'operator'),
                                  (1, 'node_id_in', 'other'),
                                  (1, 'amount_out_msat', 1),
                                  (1, 'amount_in_msat', 350000000),
                                  (0, 'amount_in_msat', 350006000),
                                  (1, 'cltv_in', 629), (1, 'cltv_out', 621)]:
            changed = copy.deepcopy(route)
            changed['routes'][0]['path'][index][key] = value
            self.assertFalse(self.ready(changed))
        self.assertFalse(self.ready({'routes': []}))
        self.assertFalse(self.ready({'routes': route['routes']*2}))
        route['routes'][0]['path'].pop()
        self.assertFalse(self.ready(route))

    def test_route_wait_only_queries_with_fee_and_delay_bounds(self):
        quote, _, route = fixture()
        rpc = Mock(return_value=route)
        payer = dict(id='payer', cli=['payer-cli'], proc=None)
        with patch('routed_xbt_regtest.wait_until', side_effect=lambda fn,*a,**kw: self.assertTrue(fn())):
            wait_for_route(SimpleNamespace(rpc=rpc), payer, dict(id='relay'),
                           dict(id='operator'), quote['terms'])
        args = rpc.call_args.args
        self.assertEqual(args[:2], (['payer-cli','-k'], 'getroutes'))
        for arg in ('maxfee_msat=10000', 'maxdelay=2016', 'maxparts=1', 'final_cltv=622'):
            self.assertIn(arg, args)
        self.assertEqual(rpc.call_count, 1)

    def test_receipt_verifies_proof_and_exact_fee(self):
        quote, row, _ = fixture()
        result = receipt([row], quote, 'operator')
        self.assertEqual(result['xbt_sent_sats'], 350005)
        self.assertEqual(result['xbt_routing_fee_msat'], 5000)
        self.assertTrue(result['matching_preimage_verified'])
        self.assertEqual(receipt([row], quote, 'operator'), result)
        del row['number_of_parts']
        self.assertEqual(receipt([row], quote, 'operator'), result)

    def test_wrong_identity_amount_fee_or_proof_refused(self):
        quote, row, _ = fixture()
        for key, value in [('bolt11','other'), ('destination','other'),
                           ('payment_hash','00'*32), ('amount_msat',1),
                           ('amount_sent_msat',350000000), ('number_of_parts',2),
                           ('preimage','cd'*32), ('preimage','bad'),
                           ('status','pending')]:
            with self.subTest(key=key, value=value), self.assertRaises(AssertionError):
                receipt([dict(row, **{key:value})], quote, 'operator')
        for rows in ([], [row,row]):
            with self.assertRaises(AssertionError):
                receipt(rows, quote, 'operator')

    def test_definite_failure_has_no_proof(self):
        quote, row, _ = fixture()
        row['status'] = 'failed'
        with self.assertRaises(AssertionError):
            receipt([row], quote, 'operator', failed=True)
        del row['preimage']
        del row['amount_sent_msat']
        del row['amount_msat']
        result = receipt([row], quote, 'operator', failed=True)
        self.assertEqual(result, dict(outcome='failed', automatic_resubmission=False))
        row['status'] = 'pending'
        with self.assertRaises(AssertionError):
            receipt([row], quote, 'operator', failed=True)


if __name__ == '__main__':
    unittest.main()
