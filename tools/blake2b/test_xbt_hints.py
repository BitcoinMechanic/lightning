"""Signed XBT hint planning and its receiving-API quote binding."""
import copy
import subprocess
import unittest
from unittest.mock import Mock, patch

import outgoing_xbt as xbt
import test_reverse_hints as fixture
import test_routed_receive_service as api_fixture
from service_manager import private_load


def invoice(hints=None):
    return dict(fixture.decoded(hints), currency='xbtrt')


class HintTests(unittest.TestCase):
    def test_native_xbt_private_tail_and_fee_budget(self):
        decoded=invoice();before=copy.deepcopy(decoded)
        rpc=Mock(side_effect=[fixture.unavailable(),fixture.prefix()])
        route,policy=xbt.plan(['xbt'],decoded,fixture.A,rpc)
        self.assertEqual(decoded,before)
        self.assertEqual(xbt.validate(route,xbt.AMOUNT,policy),5000)
        self.assertEqual(route[-1]['channel'],'8000000x1x0')
        self.assertIn('amount_msat=100005000',rpc.call_args.args)
        self.assertIn('maxfee_msat=5000',rpc.call_args.args)
        self.assertIn('final_cltv=46',rpc.call_args.args)
        self.assertTrue(all(c.args[1]=='getroutes' for c in rpc.call_args_list))

    def test_exact_fee_limit_and_over_budget_tail(self):
        rpc=Mock(side_effect=[fixture.unavailable(),fixture.prefix()])
        route,policy=xbt.plan(['xbt'],invoice(),fixture.A,rpc,max_fee_msat=5000)
        self.assertEqual(xbt.validate(route,xbt.AMOUNT,policy),5000)
        self.assertIn('maxfee_msat=0',rpc.call_args.args)
        rpc=Mock(side_effect=fixture.unavailable())
        with self.assertRaises(subprocess.CalledProcessError):
            xbt.plan(['xbt'],invoice(),fixture.A,rpc,max_fee_msat=4999)
        rpc.assert_called_once()

    def test_currency_features_metadata_rejected_before_queries(self):
        for changes in ({'currency':'bcrt'},{'currency':'xbt'},{'valid':False},
                        {'features':'1'},{'payment_metadata':''},{'min_final_cltv_expiry':41}):
            rpc=Mock()
            with self.assertRaises(ValueError):xbt.plan(['xbt'],dict(invoice(),**changes),fixture.A,rpc)
            rpc.assert_not_called()

    def test_hint_count_loops_and_delay_bounds(self):
        for hints in ('bad',[[fixture.hint()]]*9):
            rpc=Mock()
            with self.assertRaises(ValueError):xbt.plan(['xbt'],invoice(hints),fixture.A,rpc)
            rpc.assert_not_called()
        for hints in ([[fixture.hint(delta=41)]],[[fixture.hint(),fixture.hint()]],
                      [[fixture.hint(node=fixture.C)]]):
            rpc=Mock(side_effect=fixture.unavailable())
            with self.assertRaises(subprocess.CalledProcessError):xbt.plan(['xbt'],invoice(hints),fixture.A,rpc)
            rpc.assert_called_once()

    def test_public_route_preferred_and_transport_errors_propagate(self):
        rpc=Mock(return_value=fixture.prefix(destination=fixture.C,amount=xbt.AMOUNT,delay=40))
        route,_=xbt.plan(['xbt'],invoice(),fixture.A,rpc)
        self.assertEqual(route[-1]['channel'],'1x1x0');rpc.assert_called_once()
        for error in (fixture.unavailable(206),fixture.unavailable(215),subprocess.TimeoutExpired('private',1)):
            for prefix in (False,True):
                rpc=Mock(side_effect=[fixture.unavailable(),error] if prefix else error)
                with self.assertRaises(type(error)):xbt.plan(['xbt'],invoice(),fixture.A,rpc)
                self.assertEqual(rpc.call_count,2 if prefix else 1)

    def test_alternative_hint_and_multihop_tail(self):
        hints=[[fixture.hint(fixture.D)],[fixture.hint()]]
        rpc=Mock(side_effect=[fixture.unavailable(),fixture.unavailable(),fixture.prefix()])
        route,_=xbt.plan(['xbt'],invoice(hints),fixture.A,rpc)
        self.assertEqual(route[0]['id'],fixture.B);self.assertEqual(rpc.call_count,3)
        hints=[[fixture.hint(fixture.B,'2x1x0',base=1,ppm=3,delta=7),
                fixture.hint(fixture.D,'3x1x0',base=1,ppm=2,delta=8)]]
        rpc=Mock(side_effect=[fixture.unavailable(),fixture.prefix(amount=100000502,delay=55)])
        route,policy=xbt.plan(['xbt'],invoice(hints),fixture.A,rpc)
        self.assertEqual(xbt.validate(route,xbt.AMOUNT,policy),502)
        self.assertEqual([h['delay'] for h in route],[55,48,40])

    def test_api_saves_hint_route_fee_audit_and_funding_pin(self):
        f=api_fixture.ServiceTests();f.setUp();self.addCleanup(f.doCleanups)
        f.decoded.update(payee=fixture.C,routes=[[fixture.hint()]])
        f.outgoing['peer_id']=fixture.B
        queries=[]
        def rpc(cli,method,*args):
            if method=='getroutes':
                queries.append(args)
                if len(queries)==1:raise fixture.unavailable()
                return fixture.prefix(source='a')
            return f.rpc(cli,method,*args)
        with patch('routed_receive_service.RPC.call',side_effect=rpc):
            offer=f.quote()
        saved=private_load(f.directory/'quote.json')
        self.assertEqual(offer['btc_sats'],1501)
        self.assertEqual(saved['controller']['route'][-1]['channel'],'8000000x1x0')
        self.assertEqual(saved['controller']['route'][0]['amount_msat'],100005000)
        self.assertEqual(saved['controller']['xbt_first_hop']['peer_id'],fixture.B)
        self.assertEqual(saved['oracle']['xbt_sats'],100010)
        self.assertEqual(f.decoded['currency'],'xbtrt')
        self.assertEqual(len(queries),2)
        with patch('routed_receive_service.RPC.call',side_effect=AssertionError('must not replan')):
            self.assertEqual(f.quote(),offer)


if __name__=='__main__':unittest.main()
