"""Candidate timing arithmetic and asymmetric chain-progress cases; no nodes."""
import unittest

from reverse_timing import proposal, pre_spend_report, pending_report


class TimingTests(unittest.TestCase):
    def test_current_phoenix_route(self):
        p = proposal(448)
        self.assertEqual(p['minimum_xbt_remaining_blocks'], 598)
        self.assertEqual(p['proposed_xbt_invoice_cltv'], 622)
        self.assertTrue(p['fits_default_cltv_budget'])
        self.assertFalse(p['live_payment_enabled'])
        self.assertFalse(p['relative_chain_progress_guaranteed'])

    def test_v1_proposal_is_not_silently_reinterpreted(self):
        p = proposal(448)
        self.assertEqual(p['expected_xbt_blocks_per_btc_block'], 1)
        self.assertNotIn('assumed_max_xbt_blocks_per_btc_block', p)
        p['model'] = 'reverse-timing-candidate-v1'
        with self.assertRaises(ValueError):
            pre_spend_report(p, btc_height=1000, xbt_height=2000, xbt_expiry=2622)

    def test_exact_route_budget_boundary_never_clamps(self):
        self.assertEqual(proposal(1842)['proposed_xbt_invoice_cltv'], 2016)
        self.assertTrue(proposal(1842)['fits_default_cltv_budget'])
        self.assertEqual(proposal(1843)['proposed_xbt_invoice_cltv'], 2017)
        self.assertFalse(proposal(1843)['fits_default_cltv_budget'])
        self.assertTrue(proposal(576)['fits_default_cltv_budget'])

    def test_pre_spend_exact_minimum_and_maximum(self):
        p = proposal(448)
        for remaining in (598, 622, 2016):
            r = pre_spend_report(p, btc_height=969000, xbt_height=975000,
                                 xbt_expiry=975000+remaining)
            self.assertTrue(r['model_margin_met'])
            self.assertEqual(r['btc_planning_expiry_upper'], 969454)
        for remaining in (-1, 0, 597, 2017):
            r = pre_spend_report(p, btc_height=969000, xbt_height=975000,
                                 xbt_expiry=975000+remaining)
            self.assertFalse(r['model_margin_met'])

    def test_quote_drift_consumes_margin(self):
        p = proposal(448)
        for advance, expected in ((24, True), (25, False)):
            r = pre_spend_report(p, btc_height=969000,
                xbt_height=975000+advance, xbt_expiry=975622)
            self.assertEqual(r['model_margin_met'], expected)

    def test_fresh_btc_height_changes_planning_reference_not_xbt_margin(self):
        p = proposal(448)
        a = pre_spend_report(p, btc_height=1000, xbt_height=2000, xbt_expiry=2622)
        b = pre_spend_report(p, btc_height=1010, xbt_height=2000, xbt_expiry=2622)
        self.assertEqual(b['btc_planning_expiry_upper']-a['btc_planning_expiry_upper'], 10)
        self.assertEqual(a['xbt_remaining_blocks'], b['xbt_remaining_blocks'])

    def test_modified_candidate_refused(self):
        for key, value in (('expected_xbt_blocks_per_btc_block', 4),
                           ('minimum_xbt_remaining_blocks', 100),
                           ('fits_default_cltv_budget', True)):
            p = proposal(1843)
            p[key] = value
            with self.assertRaises(ValueError):
                pre_spend_report(p, btc_height=1000, xbt_height=2000, xbt_expiry=4000)

    def test_xbt_only_advancement_detects_breach(self):
        for xbt_height, breach in ((2000, False), (2001, True)):
            r = pending_report(btc_height=1000, btc_htlc_expiry=1448,
                               xbt_height=xbt_height, xbt_htlc_expiry=2592)
            self.assertEqual(r['model_margin_breached'], breach)
            self.assertFalse(r['permits_xbt_failure'])
            self.assertFalse(r['permits_btc_resend'])

    def test_btc_progress_reduces_model_requirement(self):
        r = pending_report(btc_height=1001, btc_htlc_expiry=1448,
                           xbt_height=2001, xbt_htlc_expiry=2592)
        self.assertFalse(r['model_margin_breached'])
        self.assertEqual(r['model_required_xbt_remaining_blocks'], 591)

    def test_btc_expiry_is_not_failure(self):
        for height in (1448, 1500):
            r = pending_report(btc_height=height, btc_htlc_expiry=1448,
                               xbt_height=2000, xbt_htlc_expiry=2144)
            self.assertTrue(r['btc_expiry_reached'])
            self.assertTrue(r['outcome_still_requires_reconciliation'])
            self.assertFalse(r['permits_xbt_failure'])
            self.assertFalse(r['permits_btc_resend'])
            self.assertTrue(r['recovery_reserve_reached'])

    def test_invalid_input_types_and_ranges(self):
        for value in (True, '448', 448.0, 0, -1, 2017):
            with self.assertRaises(ValueError):
                proposal(value)
        for value in (True, -1, '1000', 500000000):
            with self.assertRaises(ValueError):
                pending_report(btc_height=value, btc_htlc_expiry=1448,
                               xbt_height=2000, xbt_htlc_expiry=2622)


if __name__ == '__main__':
    unittest.main()
