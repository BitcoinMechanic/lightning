"""Synthetic transaction vectors; no real-chain acceptance claim."""
import copy,hashlib,unittest
import chain_recovery as v

def vector(b):
    assert len(b)<253
    return bytes([len(b)])+b


def rawtx(txid,index,outputs,witness=(),sequence=0xffffffff,locktime=0):
    body=b'\x01'+bytes.fromhex(txid)[::-1]+index.to_bytes(4,'little')+b'\x00'+sequence.to_bytes(4,'little')
    body+=bytes([len(outputs)])+b''.join((amount//1000).to_bytes(8,'little')+vector(script) for amount,script in outputs)
    version=b'\x02\x00\x00\x00';lock=locktime.to_bytes(4,'little')
    raw=version+(b'\x00\x01' if witness else b'')+body
    if witness:raw+=bytes([len(witness)])+b''.join(vector(w) for w in witness)
    raw+=lock
    return dict(hash=v.sha(v.sha(version+body+lock))[::-1].hex(),rawtx=raw.hex())


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.preimage=b'\xab'*32
        self.spec=dict(direction='forward',node_ids=dict(btc='02'+'a'*64,xbt='03'+'b'*64),
            channel=dict(channel_id='c'*64,funding_txid='d'*64,funding_outnum=0,peer_id='02'+'e'*64,short_channel_id='1x1x1'),
            htlc_id=7,payment_hash=v.sha(self.preimage).hex(),expiry=300,incoming_amount_msat=100000000,
            outgoing_amount_msat=200000000,groupid=1,partid=0)
        # Synthetic witnesses exercise local structural checks. Signatures and
        # confirmations remain trusted node evidence, not script validation.
        self.htlc=(b'\x76\xa9\x14'+b'r'*20+b'\x87\x63\xac\x67\x21'+b'\x02'+b'k'*32+
            b'\x7c\x82\x01\x20\x87\x63\xa9\x14'+hashlib.new('ripemd160',bytes.fromhex(self.spec['payment_hash'])).digest()+
            b'\x88\x52\x7c\x21'+b'\x03'+b'l'*32+b'\x52\xae\x67\x75'+v.script_number(300)+b'\xb1\x75\xac\x68\x51\xb2\x75\x68')
        self.delayed=b'\x63\x21'+b'\x02'+b'\x11'*32+b'\x67\x55\xb2\x75\x21'+b'\x03'+b'\x22'*32+b'\x68\xac'
        self.commit=rawtx('d'*64,0,[(100000000,b'\x00\x20'+v.sha(self.htlc))]);self.commit['blockheight']=100
        self.success=rawtx(self.commit['hash'],0,[(99000000,b'\x00\x20'+v.sha(self.delayed))],
            [b'',b'sig1',b'sig2',self.preimage,self.htlc]);self.success['blockheight']=101
        self.sweep=rawtx(self.success['hash'],0,[(98000000,b'\x00\x14'+b'\x33'*20)],
            [b'sig',b'',self.delayed],5);self.sweep['blockheight']=106
        self.rows=[self.commit,self.success,self.sweep]
        self.funds=[dict(txid=self.sweep['hash'],output=0,amount_msat=98000000,scriptpubkey=(b'\x00\x14'+b'\x33'*20).hex(),status='confirmed',blockheight=106)]
    def prove(self):return v.prove(self.spec,self.rows,self.funds,110)
    def test_complete_chain_and_pending_stages(self):
        self.assertTrue(self.prove()['verified'])
        self.assertEqual(self.prove()['proof']['csv_delay'],5)
        for count,phase in ((0,'awaiting_commitment'),(1,'awaiting_htlc_success'),(2,'awaiting_csv_sweep')):
            self.assertEqual(v.prove(self.spec,self.rows[:count],self.funds,110)['phase'],phase)
    def test_claim_reconfirmation_restarts_csv(self):
        self.success['blockheight']=0;self.sweep['blockheight']=0
        self.assertEqual(self.prove()['phase'],'awaiting_htlc_success')
        self.success['blockheight']=104
        self.assertEqual(self.prove()['phase'],'awaiting_csv_sweep')
        self.sweep['blockheight']=106
        with self.assertRaisesRegex(ValueError,'premature_csv_sweep'):self.prove()
        self.sweep['blockheight']=109;self.funds[0]['blockheight']=109
        result=self.prove()
        self.assertTrue(result['verified']);self.assertEqual(result['proof']['heights'],[100,104,109])

    def test_commitment_rollback_and_ordered_reconfirmation(self):
        for tx in self.rows:tx['blockheight']=0
        self.assertEqual(self.prove()['phase'],'awaiting_commitment')
        self.commit['blockheight']=103
        self.assertEqual(self.prove()['phase'],'awaiting_htlc_success')
        self.success['blockheight']=104
        self.assertEqual(self.prove()['phase'],'awaiting_csv_sweep')
        self.sweep['blockheight']=109;self.funds[0]['blockheight']=109
        result=self.prove()
        self.assertTrue(result['verified']);self.assertEqual(result['proof']['heights'],[103,104,109])
        self.assertEqual(result['proof']['commitment'],self.commit['hash'])

    def test_unconfirmed_sweep_never_verifies(self):
        self.sweep['blockheight']=0;self.assertFalse(self.prove()['verified'])
    def test_other_funding_not_claimed(self):
        self.spec['channel']['funding_txid']='e'*64;self.assertFalse(self.prove()['verified'])
    def test_wrong_hash_or_expired_claim_refused(self):
        self.spec['payment_hash']='a'*64
        with self.assertRaises(ValueError):self.prove()
        self.spec['payment_hash']=v.sha(self.preimage).hex();self.spec['expiry']=101
        with self.assertRaises(ValueError):self.prove()
    def test_early_sweep_and_wrong_wallet_evidence_refused(self):
        self.sweep['blockheight']=105
        with self.assertRaises(ValueError):self.prove()
        self.sweep['blockheight']=106;self.funds[0]['amount_msat']-=1000
        with self.assertRaises(ValueError):self.prove()
        self.funds=[]
        with self.assertRaises(ValueError):self.prove()
    def test_raw_hash_mismatch_and_duplicate_refused(self):
        self.success['hash']='f'*64
        with self.assertRaises(ValueError):self.prove()
        self.success['hash']=v.transaction(self.success['rawtx'])['txid'];self.rows.append(self.success)
        with self.assertRaises(ValueError):self.prove()
    def test_parser_truncation_trailing_and_noncanonical_refused(self):
        for raw in (self.success['rawtx'][:-2],self.success['rawtx']+'00','02000000fd0100'+'00'*60):
            with self.assertRaises(ValueError):v.transaction(raw)
    def test_csv_template_and_sequence_refused(self):
        with self.assertRaises(ValueError):v.csv_delay(self.delayed+b'\x00')
        for seq in (4,1<<31,1<<22):
            t=rawtx(self.success['hash'],0,[(98000000,b'\x00\x14'+b'\x33'*20)],[b'sig',b'',self.delayed],seq)
            t['blockheight']=106
            with self.assertRaises(ValueError):v.prove(self.spec,self.rows[:2]+[t],self.funds,110)
    def test_timeout_confirms_only_after_exact_script_deadline(self):
        tx=rawtx(self.commit['hash'],0,[(99000000,b'\x00\x14'+b't'*20)],
                 [b'sig',b'',self.htlc],sequence=0xfffffffd,locktime=300)
        tx['blockheight']=301
        self.assertEqual(v.prove(self.spec,[self.commit,tx],[],310)['phase'],'timeout_verified')
        tx['blockheight']=300
        with self.assertRaisesRegex(ValueError,'premature_htlc_timeout'):v.prove(self.spec,[self.commit,tx],[],310)
        tx['blockheight']=0
        self.assertFalse(v.prove(self.spec,[self.commit,tx],[],310)['verified'])
    def test_full_script_template_and_expiry_are_required(self):
        for script in (self.htlc+b'\x00', self.htlc[:9], self.htlc.replace(v.script_number(300),v.script_number(301))):
            with self.assertRaises(ValueError):v.received_script(script,self.spec['payment_hash'],300)

if __name__=='__main__':unittest.main()
