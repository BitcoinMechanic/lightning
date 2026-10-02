"""Customer-only receiving, repeat safety and signed offer validation."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from customer_receive import workflow
from receive_service import FORMAT


class CustomerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)/'receive'
        self.credentials = dict(token='ab'*32, payer_id='customer')
        self.invoices = []
        self.calls = []
        self.requests = []
        self.creations = 0
        self.lost_invoice = self.lost_reply = False
        self.offer = dict(format=FORMAT, xbt_invoice_sha256=hashlib.sha256(b'lnxbt-own').hexdigest(),
                          btc_invoice='lnbc-offer', btc_sats=1473, xbt_sats=325000, expires_at=1100)

    def rpc(self, cli, method, *args):
        self.assertEqual(cli, ['customer'])
        self.calls.append(method)
        if method == 'getinfo': return dict(network='xbt', id='customer')
        if method == 'listinvoices': return {'invoices':copy.deepcopy(self.invoices)}
        if method == 'invoice':
            self.creations += 1
            self.invoices = [dict(bolt11='lnxbt-own', amount_msat=325000000, status='unpaid', expires_at=2000)]
            if self.lost_invoice: raise TimeoutError('PRIVATE')
            return self.invoices[0]
        if method == 'decode':
            return dict(valid=True, type='bolt11 invoice', currency='xbt' if args[0]=='lnxbt-own' else 'bc',
                        amount_msat=325000000 if args[0]=='lnxbt-own' else 1473000,
                        payment_hash='11'*32, created_at=900, expiry=1000, min_final_cltv_expiry=300)
        self.fail('Forbidden wallet RPC: '+method)

    def send(self, url, token, body, endpoint_path):
        self.assertEqual(endpoint_path, '/v1/receive')
        self.requests.append(copy.deepcopy(body))
        if self.lost_reply: raise TimeoutError('PRIVATE')
        return copy.deepcopy(self.offer)

    def flow(self, **kwargs):
        options = dict(cli=['customer'], directory=self.directory, credentials=self.credentials,
                       url='http://127.0.0.1:19840', xbt_sats=325000, max_btc_sats=1480,
                       rpc=self.rpc, send=self.send, now=lambda:1000)
        options.update(kwargs)
        return workflow(**options)

    def test_repeat_uses_saved_offer_and_paid_status_needs_no_api(self):
        first = self.flow(); self.assertEqual(first['outcome'],'awaiting_btc')
        self.assertEqual(self.flow(), first)
        self.assertEqual((self.creations,len(self.requests)),(1,1))
        self.invoices[0].update(status='paid',amount_received_msat=325000000)
        self.assertEqual(self.flow(),dict(outcome='paid',received_xbt_sats=325000))
        self.assertEqual(len(self.requests),1)
        self.assertNotIn('pay',self.calls)

    def test_lost_invoice_reply_recovers_original_label(self):
        self.lost_invoice = True
        with self.assertRaises(TimeoutError): self.flow()
        self.lost_invoice = False
        self.flow(); self.assertEqual(self.creations,1)

    def test_lost_api_reply_reuses_original_request_id(self):
        self.lost_reply = True
        with self.assertRaises(TimeoutError): self.flow()
        self.lost_reply = False
        self.flow()
        self.assertEqual(self.requests[0],self.requests[1])
        self.assertEqual(self.creations,1)

    def test_changed_arguments_or_credentials_refused(self):
        self.flow()
        for options in (dict(max_btc_sats=1500),dict(xbt_sats=325001),dict(url='http://127.0.0.1:19841'),
                        dict(credentials=dict(self.credentials,token='cd'*32))):
            with self.assertRaises(ValueError): self.flow(**options)
        self.assertEqual(len(self.requests),1)

    def test_missing_or_changed_saved_wallet_invoice_refused(self):
        self.flow(); original=copy.deepcopy(self.invoices)
        self.invoices=[]
        with self.assertRaises(ValueError): self.flow()
        self.invoices=original; self.invoices[0]['bolt11']='lnxbt-other'
        with self.assertRaises(ValueError): self.flow()
        self.assertEqual(self.creations,1)

    def test_bad_response_amount_hash_and_cap_refused(self):
        for key,value in (('btc_sats',1481),('xbt_sats',325001),('xbt_invoice_sha256','bad'),('btc_invoice','not-invoice')):
            with tempfile.TemporaryDirectory() as temp:
                original=self.offer[key]; self.offer[key]=value
                with self.assertRaises(ValueError): self.flow(directory=Path(temp)/'receive')
                self.offer[key]=original

    def test_signed_hash_mismatch_refused(self):
        def bad_rpc(cli, method, *args):
            r=self.rpc(cli,method,*args)
            if method=='decode' and args[0]=='lnbc-offer': r['payment_hash']='22'*32
            return r
        with self.assertRaises(ValueError): self.flow(rpc=bad_rpc)

    def test_expired_offer_is_not_automatically_requoted(self):
        self.flow()
        self.assertEqual(self.flow(now=lambda:1100)['outcome'],'quote_expired')
        self.assertEqual(len(self.requests),1)

    def test_bad_caps_or_public_endpoint_never_call_wallet(self):
        for options in (dict(xbt_sats=True),dict(max_btc_sats=10001),dict(url='http://example.com:19840')):
            with self.assertRaises(ValueError): self.flow(**options)
        self.assertFalse(self.calls)


if __name__ == '__main__': unittest.main()
