#!/usr/bin/env python3
"""Fixture dependency failures must be early and must keep RPC arguments private."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_engine as engine


class FixtureTests(unittest.TestCase):
    def test_funding_address_does_not_depend_on_deprecated_default(self):
        lab = Mock()
        node = {'cli':['lightning-cli']}
        def newaddr(cli,method,*args):
            self.assertEqual(method,'newaddr')
            # Pinned newaddr contract: the default exposes p2tr only, whereas
            # an explicit bech32 request exposes the intended P2WPKH address.
            return {'bech32':'bcrt1q-fixture-address'} if args==('bech32',) else {'p2tr':'bcrt1p-fixture-address'}
        lab.rpc.side_effect = newaddr
        self.assertEqual(engine.funding_address(lab,node),'bcrt1q-fixture-address')
        lab.rpc.side_effect = None
        for reply in ({'p2tr':'bcrt1p-fixture-address'},{'bech32':None},{'bech32':''}):
            lab.rpc.return_value = reply
            with self.assertRaisesRegex(RuntimeError,'fixture_bech32_address_missing'):
                engine.funding_address(lab,node)

    def test_missing_invoice_or_topology_rpc_is_reported_by_capability_preflight(self):
        rows = [{'command': name + ' [arguments]'} for name in engine.REQUIRED_RPCS]
        engine.check_capabilities({'help': rows})
        for method in ('decode', 'listincoming', 'listinvoices', 'sendpay', 'waitsendpay'):
            with self.subTest(method=method):
                missing = [row for row in rows if not row['command'].startswith(method + ' ')]
                with self.assertRaisesRegex(RuntimeError, '^fixture_required_RPC_missing: ' + method + '$'):
                    engine.check_capabilities({'help': missing})
        with self.assertRaisesRegex(RuntimeError, 'fixture_required_RPC_missing'):
            engine.check_capabilities({})

    def test_fixture_needs_neither_legacy_pay_nor_getroute(self):
        # Explicit RPC fixture drawn from the audited runtime dependencies,
        # not constructed by echoing REQUIRED_RPCS back to the implementation.
        methods = ('getinfo invoice decode signinvoice newaddr listfunds connect '
                   'fundchannel multifundchannel listpeerchannels sendpay waitsendpay listincoming '
                   'listinvoices help plugin listconfigs').split()
        engine.check_capabilities({'help':[{'command': m+' [args]'} for m in methods]})
        self.assertNotIn('getroute', engine.REQUIRED_RPCS)
        self.assertNotIn('pay', engine.REQUIRED_RPCS)
        self.assertNotIn('pay', engine.FIXTURE_PLUGINS)

    def test_receiver_confirmation_requires_exact_paid_invoice(self):
        good = dict(payment_hash='33'*32,status='paid',amount_received_msat=1000000)
        engine.check_received({'invoices':[good]},'33'*32,1000000)
        for changes in ({'status':'unpaid'}, {'payment_hash':'44'*32},
                        {'amount_received_msat':999999}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError,'fixture_recipient_payment_not_confirmed'):
                    engine.check_received({'invoices':[dict(good,**changes)]},'33'*32,1000000)
        with self.assertRaisesRegex(RuntimeError,'fixture_recipient_payment_not_confirmed'):
            engine.check_received({'invoices':[]},'33'*32,1000000)

    def test_registered_plugin_is_not_ready_until_init_completes(self):
        rows = [{'name':'/fixture/plugins/'+name,'active':True}
                for name in ('bcli','offers','topology','txprepare','spenderp')]
        self.assertTrue(engine.plugins_ready({'plugins':rows}))
        rows[1]['active'] = False
        self.assertFalse(engine.plugins_ready({'plugins':rows}))
        self.assertFalse(engine.plugins_ready({'plugins':[]}))

    def test_rpc_error_preserves_details_without_printing_invoice(self):
        invoice = 'lnbcrt-fixture-only-sentinel'
        reply = json.dumps({'code': -32601, 'message': 'Unknown command decode', 'data': invoice})
        with tempfile.TemporaryDirectory() as tmp:
            lab = engine.Lab.__new__(engine.Lab)
            lab.root = Path(tmp)
            result = subprocess.CompletedProcess(['lightning-cli', 'decode', invoice], 1, reply, '')
            with patch.object(engine.subprocess, 'run', return_value=result):
                with self.assertRaises(engine.FixtureRPCError) as caught:
                    lab.rpc(['lightning-cli'], 'decode', invoice)
            error = caught.exception
            self.assertIn('decode', str(error))
            self.assertIn('-32601', str(error))
            self.assertNotIn(invoice, str(error))
            self.assertNotIn(invoice, str(error.cmd))
            self.assertEqual(error.stdout, reply)
            details = json.loads((lab.root/'rpc-errors.jsonl').read_text())
            self.assertEqual(details['method'], 'decode')
            self.assertEqual(details['stdout'], reply)

    def test_rpc_timeout_does_not_print_invoice(self):
        invoice = 'lnbcrt-fixture-only-sentinel'
        lab = engine.Lab.__new__(engine.Lab)
        with patch.object(engine.subprocess, 'run', side_effect=subprocess.TimeoutExpired(
                ['lightning-cli', 'pay', invoice], 90)):
            with self.assertRaisesRegex(RuntimeError, 'fixture RPC pay timed out') as caught:
                lab.rpc(['lightning-cli'], 'pay', invoice, timeout=90)
        self.assertNotIn(invoice, str(caught.exception))

    def payment_fixture(self):
        node = {'cli':['lightning-cli','--notifications=none']}
        peer = {'identity':{'node_id':'02'+'ab'*32}}
        created = dict(bolt11='lnbcrt-fixture-only',payment_hash='33'*32)
        channel = dict(state='CHANNELD_NORMAL',peer_connected=True,reestablished=True,
                       peer_id=peer['identity']['node_id'],short_channel_id='107x1x0',
                       channel_type={'bits':[12,22,514]})
        decoded = dict(valid=True,type='bolt11 invoice',currency='bcrt',
                       features='01'+'00'*64,amount_msat=1000000,
                       payment_hash=created['payment_hash'],payment_secret='44'*32,
                       payee=peer['identity']['node_id'],min_final_cltv_expiry=40)
        pending = dict(payment_hash=created['payment_hash'],groupid=1,
                       destination=peer['identity']['node_id'],status='pending',
                       amount_msat=1000000,amount_sent_msat=1000000)
        lab = Mock()
        lab.rpc.return_value = decoded
        lab.rpc_named.side_effect = [pending,dict(pending,status='complete')]
        return lab,node,peer,channel,created,pending

    def test_direct_payment_waits_for_same_full_attempt(self):
        lab,node,peer,channel,created,_ = self.payment_fixture()
        engine.direct_payment(lab,node,peer,channel,created,1000000)
        calls = lab.rpc_named.call_args_list
        self.assertEqual([c.args[1] for c in calls],['sendpay','waitsendpay'])
        sent, waited = (c.args[2] for c in calls)
        self.assertEqual(sent['route'],[dict(id=peer['identity']['node_id'],
                         channel='107x1x0',amount_msat=1000000,delay=40)])
        self.assertEqual(sent['payment_secret'],'44'*32)
        self.assertEqual(sent['bolt11'],created['bolt11'])
        for key,value in dict(payment_hash='33'*32,partid=0,groupid=1).items():
            self.assertEqual(sent[key],value)
            self.assertEqual(waited[key],value)
        self.assertEqual(waited['timeout'],60)

    def test_direct_payment_refuses_mismatches_and_never_retries_uncertain_replies(self):
        for changes in ({'status':'pending'},{'payment_hash':'55'*32},{'partid':1},
                        {'groupid':2},{'destination':'03'+'cd'*32},
                        {'amount_msat':999999},{'amount_sent_msat':1000001}):
            with self.subTest(changes=changes):
                lab,node,peer,channel,created,pending = self.payment_fixture()
                lab.rpc_named.side_effect = [pending,dict(pending,status='complete')|changes]
                with self.assertRaisesRegex(RuntimeError,'fixture_payment_attempt_mismatch'):
                    engine.direct_payment(lab,node,peer,channel,created,1000000)
                self.assertEqual([c.args[1] for c in lab.rpc_named.call_args_list],['sendpay','waitsendpay'])
        for failure_at in ('sendpay','waitsendpay'):
            with self.subTest(failure_at=failure_at):
                lab,node,peer,channel,created,pending = self.payment_fixture()
                error = RuntimeError('lost reply')
                lab.rpc_named.side_effect = [error] if failure_at=='sendpay' else [pending,error]
                with self.assertRaisesRegex(RuntimeError,'lost reply'):
                    engine.direct_payment(lab,node,peer,channel,created,1000000)
                self.assertEqual(sum(c.args[1]=='sendpay' for c in lab.rpc_named.call_args_list),1)

    def test_direct_payment_checks_invoice_and_actual_channel_before_send(self):
        cases = [('invoice', {'payee':'03'+'cd'*32}),('invoice', {'amount_msat':999999}),
                 ('invoice', {'payment_hash':'55'*32}),('invoice', {'payment_secret':None}),
                 ('invoice', {'min_final_cltv_expiry':None}),('invoice', {'features':''}),
                 ('channel', {'peer_id':'03'+'cd'*32}),('channel', {'peer_connected':False}),
                 ('channel', {'reestablished':False}),
                 ('channel', {'state':'CHANNELD_SHUTTING_DOWN'}),('channel', {'short_channel_id':None}),
                 ('channel', {'channel_type':{'bits':[12,22]}})]
        for target,changes in cases:
            with self.subTest(target=target,changes=changes):
                lab,node,peer,channel,created,_ = self.payment_fixture()
                (channel if target=='channel' else lab.rpc.return_value).update(changes)
                with self.assertRaises((RuntimeError,engine.ci.IdentityError)):
                    engine.direct_payment(lab,node,peer,channel,created,1000000)
                lab.rpc_named.assert_not_called()

    def test_named_rpc_keeps_json_types_and_strings_intact(self):
        lab = engine.Lab.__new__(engine.Lab)
        params = dict(payment_hash='33'*32,partid=0,groupid=1,
                      route=[dict(channel='107x1x0',amount_msat=1000000,delay=40)],
                      bolt11='lnbcrt-fixture-only')
        result = subprocess.CompletedProcess([],0,'{"status":"pending"}','')
        with patch.object(engine.subprocess,'run',return_value=result) as run:
            lab.rpc_named(['lightning-cli'],'sendpay',params,timeout=90)
        command = run.call_args.args[0]
        self.assertEqual(command[:3],['lightning-cli','-k','sendpay'])
        self.assertEqual({k:json.loads(v) for k,v in (p.split('=',1) for p in command[3:])},params)
        self.assertEqual(run.call_args.kwargs['timeout'],90)


if __name__ == '__main__':
    unittest.main()
