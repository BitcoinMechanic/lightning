"""Customer routing budget, immutable review and lost-reply recovery."""
import json
import subprocess
import unittest
from unittest.mock import patch

import customer as command
import customer_swap as workflow
import reverse_customer as customer
import test_reverse_customer
from swap_controller import save


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.f = test_reverse_customer.CustomerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.calls = []
        self.fee_cap = 10
        self.send_fee = 5000
        self.lost_reply = False
        self.missing_record = False
        self.channels = [dict(peer_id='relay', state='CHANNELD_NORMAL', peer_connected=True,
                              htlcs=[], spendable_msat=350010000)]

    def rpc(self, cli, method, *args):
        self.assertEqual(cli[0], '/customer')
        self.calls.append((method,args))
        if method == 'listpeerchannels':
            return dict(channels=self.channels)
        if method == 'pay':
            state = json.loads((self.f.directory/'customer.json').read_text())
            self.assertEqual(state['phase'], 'submitted')
            self.assertEqual(state['max_xbt_routing_fee_sats'], self.fee_cap)
            self.assertIn('maxfee='+str(self.fee_cap*1000)+'msat', args)
            self.assertIn('maxdelay=2016', args)
            self.assertIn('retry_for=0', args)
            if not self.missing_record:
                self.f.rows = [dict(payment_hash=self.f.hash, bolt11=self.f.offer['xbt_invoice'],
                    destination=test_reverse_customer.B, amount_msat=350000000,
                    amount_sent_msat=350000000+self.send_fee, status='pending')]
            if self.lost_reply:
                raise subprocess.TimeoutExpired('PRIVATE',30)
            return {}
        return self.f.rpc(cli,method,*args)

    def review(self, fee=10, cap=400000):
        return customer.review(self.f.offer,'lnbc-original',['/customer'],self.f.directory,
                               cap,rpc=self.rpc,now=lambda:1000,max_xbt_routing_fee_sats=fee)

    def pay(self):
        return customer.pay(self.f.directory,rpc=self.rpc,now=lambda:1000)

    def test_routed_review_is_read_only_and_displays_total_budget(self):
        answer=self.review()
        self.assertEqual(answer['max_xbt_routing_fee_sats'],10)
        self.assertEqual(answer['max_total_xbt_sats'],350010)
        self.assertFalse(answer['payment_started'])
        self.assertNotIn('pay',[m for m,a in self.calls])
        state=json.loads((self.f.directory/'customer.json').read_text())
        self.assertEqual(state['max_xbt_routing_fee_sats'],10)
        self.assertNotIn('relay',json.dumps(state))

    def test_fee_cap_and_total_cap_refused_before_rpc(self):
        for fee in (-1,True,1.5,1001,'10'):
            with self.subTest(fee=fee),self.assertRaises(ValueError): self.review(fee)
        with self.assertRaises(ValueError): self.review(10,350009)
        self.assertEqual(self.calls,[])
        self.review(10,350010)

    def test_liquidity_includes_full_cap_and_does_not_aggregate(self):
        for channels in ([dict(self.channels[0],spendable_msat=350009999)],
                         [dict(self.channels[0],spendable_msat=200000000)]*2,
                         [dict(self.channels[0],peer_connected=False)],
                         [dict(self.channels[0],state='ONCHAIN')],
                         [dict(self.channels[0],htlcs=[{}])]):
            self.channels=channels
            with self.assertRaises(ValueError): self.review()
        self.assertFalse(self.f.directory.exists())

    def test_legacy_direct_policy_is_not_silently_upgraded(self):
        with self.assertRaises(ValueError): self.review(None)
        self.assertFalse(self.f.directory.exists())

    def test_explicit_zero_allows_free_route(self):
        self.fee_cap=0
        self.send_fee=0
        self.review(0)
        self.pay()
        self.f.rows[0].update(status='complete',preimage=self.f.preimage)
        self.assertEqual(self.pay()['xbt_routing_fee_msat'],0)

    def test_pending_and_complete_recovery_never_resend_or_replan(self):
        self.review()
        self.lost_reply=True
        self.assertEqual(self.pay()['outcome'],'pending')
        self.calls.clear()
        self.channels=[]
        self.assertEqual(self.pay()['outcome'],'pending')
        self.f.rows[0].update(status='complete',preimage=self.f.preimage,amount_sent_msat=350005123)
        answer=self.pay()
        self.assertEqual(answer['xbt_sent_msat'],350005123)
        self.assertEqual(answer['xbt_routing_fee_msat'],5123)
        self.assertTrue(answer['matching_preimage_verified'])
        self.assertEqual({m for m,a in self.calls},{'getinfo','listpays'})
        self.assertNotIn(self.f.preimage,json.dumps(answer))

    def test_missing_and_failed_outcomes_do_not_resubmit(self):
        self.review()
        self.missing_record=self.lost_reply=True
        self.assertEqual(self.pay()['outcome'],'unknown')
        self.assertEqual(self.pay()['outcome'],'unknown')
        self.f.rows=[dict(payment_hash=self.f.hash,bolt11=self.f.offer['xbt_invoice'],
                          destination=test_reverse_customer.B,status='failed')]
        self.assertEqual(self.pay()['outcome'],'failed')
        self.assertEqual(sum(m=='pay' for m,a in self.calls),1)

    def test_liquidity_is_rechecked_before_submission(self):
        self.review()
        self.channels=[]
        with self.assertRaises(ValueError): self.pay()
        self.assertEqual(json.loads((self.f.directory/'customer.json').read_text())['phase'],'reviewed')
        self.assertNotIn('pay',[m for m,a in self.calls])

    def test_completion_over_fee_or_total_limit_is_not_accepted(self):
        self.review()
        self.pay()
        row=self.f.rows[0]
        row.update(status='complete',preimage=self.f.preimage)
        for amount in (350010001,349999999,True,'350005000'):
            row['amount_sent_msat']=amount
            with self.assertRaises(ValueError): self.pay()
        row['amount_sent_msat']=350005000
        state=json.loads((self.f.directory/'customer.json').read_text())
        state['max_xbt_sats']=350004
        with self.assertRaises(ValueError): customer.result(state,rpc=self.rpc)

    def test_workflow_pins_cap_and_shows_it_before_confirmation(self):
        root=self.f.root/'workflow'
        root.mkdir(mode=0o700)
        wallet=root/'wallet'
        self.f.directory=wallet
        self.review()
        output=[]
        args=('lnbc-original',['/customer'],root,self.f.root/'missing-token','http://127.0.0.1:19840',400000)
        def confirm(prompt):
            self.assertEqual(json.loads(output[-1])['max_total_xbt_sats'],350010)
            return 'no'
        real_validate=customer.validate
        with patch.object(workflow,'validate',side_effect=lambda *a,**kw: real_validate(*a,rpc=self.rpc,now=lambda:1000,**kw)), \
                patch.object(workflow,'request_quote') as request,patch.object(workflow,'pay') as pay:
            answer=workflow.workflow(*args,max_xbt_routing_fee_sats=10,emit=output.append,confirm=confirm)
            self.assertEqual(answer['outcome'],'not_submitted')
            for cap in (None,0,11):
                with self.assertRaises(ValueError): workflow.workflow(*args,max_xbt_routing_fee_sats=cap)
            request.assert_not_called();pay.assert_not_called()

    def test_fresh_workflow_forwards_fee_cap_to_review(self):
        root=self.f.root/'workflow'
        self.f.directory=root/'wallet'
        token=self.f.root/'token.json'
        save(token,{'token':'private'})
        output=[]
        def request(invoice,credentials,url,directory,cap):
            directory.mkdir(mode=0o700)
            save(directory/'offer.json',self.f.offer)
        real_review=customer.review
        with patch.object(workflow,'request_quote',side_effect=request), \
                patch.object(workflow,'review',side_effect=lambda *a,**kw: real_review(*a,rpc=self.rpc,now=lambda:1000,**kw)), \
                patch.object(workflow,'pay') as pay:
            answer=workflow.workflow('lnbc-original',['/customer'],root,token,
                'http://127.0.0.1:19840',400000,max_xbt_routing_fee_sats=10,
                confirm=lambda prompt:'no',emit=output.append)
            self.assertEqual(answer['outcome'],'not_submitted')
            self.assertEqual(json.loads(output[-1])['max_xbt_routing_fee_sats'],10)
            self.assertEqual(json.loads((self.f.directory/'customer.json').read_text())['max_xbt_routing_fee_sats'],10)
            pay.assert_not_called()

    def test_submitted_workflow_only_reads_with_original_cap(self):
        root=self.f.root/'workflow';root.mkdir(mode=0o700)
        self.f.directory=root/'wallet';self.review();self.pay()
        args=('lnbc-original',['/customer'],root,self.f.root/'missing','http://127.0.0.1:19840',400000)
        with patch.object(workflow,'result',return_value={'outcome':'pending'}) as result, \
                patch.object(workflow,'request_quote') as request,patch.object(workflow,'pay') as pay:
            self.assertEqual(workflow.workflow(*args,max_xbt_routing_fee_sats=10),{'outcome':'pending'})
            with self.assertRaises(ValueError): workflow.workflow(*args,max_xbt_routing_fee_sats=11)
            result.assert_called_once();request.assert_not_called();pay.assert_not_called()

    def test_unified_command_passes_saved_cap_on_resume(self):
        root=self.f.root/'managed'
        intent=dict(id='send-test',direction='send',cli=['/customer'],customer_id=test_reverse_customer.A,
                    token_file='/not-read',url='http://127.0.0.1:19840',invoice='lnbc-original',
                    max_xbt_sats=400000,max_delay=2016,max_xbt_routing_fee_sats=10)
        with patch.object(command.customer_swap,'workflow',return_value={'outcome':'pending'}) as flow:
            command.start(root,intent,rpc=self.rpc,emit=lambda x:None)
            command.resume(root,'send-test',rpc=self.rpc,emit=lambda x:None)
            self.assertEqual(flow.call_args.kwargs['max_xbt_routing_fee_sats'],10)
            with self.assertRaises(ValueError): command.start(root,dict(intent,max_xbt_routing_fee_sats=11),rpc=self.rpc)
            self.assertEqual(flow.call_count,2)


if __name__=='__main__':
    unittest.main()
