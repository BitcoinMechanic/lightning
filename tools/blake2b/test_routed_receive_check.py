"""Live routed receiving inspection: no mutations, private output and strict bounds."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import routed_receive_check as check
from reverse_check import CheckError, DiagnosticError, private_invoice
from reverse_route import validate, plan_with_hints
from test_reverse_hints import A, B, C, D, hint, unavailable, prefix


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.settings=dict(btc_cli=['btc'],xbt_cli=['xbt'],node_ids=[A,B])
        self.decoded=dict(valid=True,type='bolt11 invoice',currency='xbt',amount_msat=350000000,
                          payment_hash='ab'*32,payment_secret='cd'*32,payee=D,
                          min_final_cltv_expiry=18,created_at=900,expiry=1000)
        self.incoming=dict(short_channel_id='3x1x0',state='CHANNELD_NORMAL',peer_connected=True,htlcs=[],receivable_msat=10000000,
                           feerate={'perkw':1250},dust_limit_msat=546000)
        self.outgoing=dict(self.incoming,peer_id=C,short_channel_id='1x1x0',spendable_msat=400000000)
        self.routes=dict(routes=[dict(amount_msat=350000000,final_cltv=40,path=[
            dict(short_channel_id_dir='1x1x0/0',node_id_in=B,node_id_out=C,
                 amount_in_msat=350005000,amount_out_msat=350005000,cltv_in=46,cltv_out=46),
            dict(short_channel_id_dir='2x1x0/0',node_id_in=C,node_id_out=D,
                 amount_in_msat=350005000,amount_out_msat=350000000,cltv_in=46,cltv_out=40)])])
        self.remote=dict(short_channel_id='2x1x0',source=C,destination=D,direction=0,active=True,
                         htlc_minimum_msat=1000,htlc_maximum_msat=500000000,
                         base_fee_millisatoshi=5000,fee_per_millionth=0,delay=6)
        self.ticker=dict(success=True,pair='BTCB2_BTC',ticker=dict(bestBid='0.0045',bestAsk='0.0045',computedAt=1000000))
        self.book=dict(success=True,pair='BTCB2_BTC',asks=[dict(price='0.0045',quantity='1')])
        self.calls=[];self.unknown=False;self.network='xbt';self.reserve=50000000

    def rpc(self,cli,method,*args):
        self.calls.append((cli,method,args))
        self.assertIn(cli,(['btc'],['xbt'],['xbt','-k']))
        self.assertIn(method,check.READ_METHODS)
        if method=='getinfo':return dict(id=A if cli==['btc'] else B,network='bitcoin' if cli==['btc'] else self.network)
        if method=='decode':return copy.deepcopy(self.decoded)
        if method=='listpeerchannels':return {'channels':[self.incoming if cli==['btc'] else self.outgoing]}
        if method=='listfunds':return {'outputs':[dict(status='confirmed',reserved=False,amount_msat=self.reserve)]}
        if method=='getroutes':return copy.deepcopy(self.routes)
        if method=='listchannels':return {'channels':[] if self.unknown else [self.remote]}
        raise AssertionError(method)

    def invoke(self,**kw):
        return check.check('lnxbt-private',self.settings,rpc=self.rpc,
                           market_fetch=lambda k:self.ticker if k=='ticker' else self.book,
                           now=lambda:1000,monotonic=lambda:0,**kw)

    def test_public_route_price_liquidity_timing_and_private_output(self):
        before=copy.deepcopy(self.settings)
        r=self.invoke()
        self.assertTrue(r['feasible']);self.assertFalse(r['live_payment_enabled'])
        self.assertEqual(r['xbt_budget_sats'],350010)
        self.assertEqual(r['estimated_btc_sats'],1591)
        self.assertEqual(r['routing_fee_msat'],5000)
        self.assertEqual(r['timing_proposal']['minimum_btc_remaining_blocks'],196)
        self.assertEqual(r['timing_proposal']['proposed_btc_invoice_cltv'],220)
        self.assertFalse(r['timing_proposal']['relative_chain_progress_guaranteed'])
        self.assertEqual(self.settings,before)
        for secret in (A,B,C,D,'ab'*32,'cd'*32,'lnxbt-private','1x1x0','2x1x0'):
            self.assertNotIn(secret,json.dumps(r))

    def test_liquidity_caps_and_reserves_are_explicit_reasons(self):
        self.outgoing['spendable_msat']=350000000;self.incoming['receivable_msat']=1000;self.reserve=0
        r=self.invoke(max_xbt_sats=350000,max_btc_sats=1500)
        self.assertFalse(r['feasible']);self.assertEqual(len(r['reasons']),5)
        self.assertFalse(r['operator_reserves_met'])

    def test_unknown_or_disabled_remote_policy_blocks_feasibility(self):
        self.unknown=True;r=self.invoke()
        self.assertTrue(r['route_found']);self.assertFalse(r['feasible'])
        self.assertEqual(r['remote_xbt_policy_hops_unknown'],1)
        self.unknown=False;self.remote['active']=False
        r=self.invoke();self.assertIn('remote hop advertised disabled',r['reasons'])

    def test_private_hint_uses_native_large_xbt_amount_and_remaining_fee_budget(self):
        self.decoded['routes']=[[hint(C,'2x1x0')]];self.unknown=True
        original=self.rpc;calls=[]
        def rpc(cli,method,*args):
            if method=='getroutes':
                calls.append(args)
                if len(calls)==1:raise unavailable()
                return prefix(source=B,destination=C,amount=350005000)
            return original(cli,method,*args)
        self.rpc=rpc;r=self.invoke()
        self.assertTrue(r['route_found']);self.assertFalse(r['feasible'])
        self.assertIn('amount_msat=350005000',calls[1]);self.assertIn('maxfee_msat=5000',calls[1])
        self.assertEqual(self.decoded['currency'],'xbt')

    def test_live_inspection_bounds_do_not_change_controller_or_btc_bounds(self):
        policy=dict(source=B,destination=D,max_fee_msat=10000,max_delay=80,max_hops=4,final_cltv=40)
        route=[dict(id=D,channel='1x1x0',amount_msat=350000000,delay=40)]
        for kw in ({},{'_inspection':True},{'_xbt_inspection':True}):
            with self.assertRaises(ValueError):validate(route,350000000,policy,**kw)
        self.assertEqual(validate(route,350000000,policy,_inspection=True,_xbt_inspection=True),0)
        route[0]['amount_msat']=500000001
        with self.assertRaises(ValueError):validate(route,500000001,policy,_inspection=True,_xbt_inspection=True)
        with self.assertRaises(ValueError):plan_with_hints(['xbt'],350000000,policy,[],lambda *a:self.fail('RPC'),_xbt_inspection=True)

    def test_invoice_and_node_validation_before_market_or_route(self):
        original=copy.deepcopy(self.decoded)
        for values in ({'currency':'xbtrt'},{'valid':False},{'amount_msat':500000001},
                       {'payee':B},{'expiry':1},{'payment_metadata':''},{'features':'1'}):
            self.decoded=dict(original,**values);self.calls=[]
            with self.assertRaises(CheckError):self.invoke()
            self.assertNotIn('getroutes',[m for _,m,_ in self.calls])
        self.decoded=original;self.network='xbt-regtest'
        with self.assertRaises(CheckError):self.invoke()

    def test_no_route_and_rpc_error_privacy(self):
        original=self.rpc
        def no_path(cli,method,*args):
            if method=='getroutes':raise unavailable()
            return original(cli,method,*args)
        self.rpc=no_path;r=self.invoke();self.assertFalse(r['route_found']);self.assertFalse(r['feasible'])
        def broken(cli,method,*args):
            if method=='getroutes':raise subprocess.CalledProcessError(1,['private-invoice'],json.dumps({'code':215,'message':'private-address'}))
            return original(cli,method,*args)
        self.rpc=broken
        with self.assertRaises(DiagnosticError) as e:self.invoke()
        self.assertEqual(e.exception.details['rpc_code'],215)
        self.assertNotIn('private-address',str(e.exception)+json.dumps(e.exception.details))

    def test_invalid_caps_refuse_before_rpc(self):
        for kw in ({'max_delay':2017},{'max_xbt_routing_fee_sats':101},{'max_btc_sats':True},
                   {'margin_bps':501},{'max_xbt_sats':0}):
            with self.assertRaises(CheckError):self.invoke(**kw)
        self.assertEqual(self.calls,[])

    def test_route_fee_and_continuity_are_checked(self):
        self.routes['routes'][0]['path'][0]['amount_out_msat']+=1
        with self.assertRaises(CheckError):self.invoke()
        self.routes['routes'][0]['path'][0]['amount_out_msat']-=1
        with self.assertRaises(CheckError):self.invoke(max_xbt_routing_fee_sats=4)

    def test_private_xbt_file_and_safe_main_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'invoice';p.write_text('lnxbt-private\n');p.chmod(0o600)
            self.assertEqual(private_invoice(p,currency='xbt'),'lnxbt-private')
            with self.assertRaises(CheckError):private_invoice(p)
            p.chmod(0o644)
            with self.assertRaises(CheckError):private_invoice(p,currency='xbt')
            p.chmod(0o600);link=Path(tmp)/'link';link.symlink_to(p)
            with self.assertRaises(OSError):private_invoice(link,currency='xbt')
            output=io.StringIO()
            with patch.object(check,'private_load',side_effect=ValueError('PRIVATE')),redirect_stdout(output):
                self.assertEqual(check.main(['--invoice-file',str(p),'--settings',str(Path(tmp)/'settings')]),1)
            self.assertNotIn('PRIVATE',output.getvalue());self.assertNotIn('lnxbt-private',output.getvalue())

    def test_stale_market_and_timing_boundary(self):
        self.ticker['ticker']['computedAt']=0
        with self.assertRaises(CheckError):self.invoke()
        self.assertTrue(check.timing(1842)['fits_default_cltv_budget'])
        self.assertFalse(check.timing(1843)['fits_default_cltv_budget'])
        for delay in (0,2017,True):
            with self.assertRaises(CheckError):check.timing(delay)


if __name__=='__main__':unittest.main()
