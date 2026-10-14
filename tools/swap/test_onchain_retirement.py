"""Synthetic retirement vectors and durable boundary tests; no funded claim."""
import copy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

import bounded_authority as ba
import coordinator_admin as admin
import cross_worker as cw
import customer_https as https
import customer_remote as remote
import deadline_protection as dp
import held_gate as hg
import onchain_retirement as retire
import restore_barrier
import test_chain_recovery as vectors
import test_deadline_protection as dt
import test_cross_worker as wt
from test_route_plan import NOW, PREIMAGE


def transactions(e,release=True):
    v=vectors.VerificationTests();v.setUp();amount=e['payload']['amount_msat'];expiry=e['incoming']['expiry']
    script=v.htlc.replace(hashlib.new('ripemd160',bytes.fromhex(v.spec['payment_hash'])).digest(),
        hashlib.new('ripemd160',bytes.fromhex(e['payload']['payment_hash'])).digest()).replace(
        vectors.v.script_number(300),vectors.v.script_number(expiry))
    pin=e['incoming']['binding']['channel'];start=expiry-71
    commit=vectors.rawtx(pin['funding_txid'],pin['funding_outnum'],[(amount,b'\x00\x20'+vectors.v.sha(script))])
    commit['blockheight']=start
    if release:
        success=vectors.rawtx(commit['hash'],0,[(amount-1000,b'\x00\x20'+vectors.v.sha(v.delayed))],
            [b'',b'sig',b'sig',bytes.fromhex(PREIMAGE),script]);success['blockheight']=start+1
        sweep=vectors.rawtx(success['hash'],0,[(amount-2000,b'\x00\x14'+b'a'*20)],
            [b'sig',b'',v.delayed],5);sweep['blockheight']=start+6
        rows=[commit,success,sweep]
        funds=[dict(txid=sweep['hash'],output=0,amount_msat=amount-2000,
                    scriptpubkey=(b'\x00\x14'+b'a'*20).hex(),status='confirmed',blockheight=start+6)]
    else:
        timeout=vectors.rawtx(commit['hash'],0,[(amount-1000,b'\x00\x14'+b'b'*20)],
            [b'sig',b'',script],0xfffffffd,expiry);timeout['blockheight']=expiry+1
        rows=[commit,timeout];funds=[]
    return rows,funds


def certificate(e,release=True):
    rows,funds=transactions(e,release);height=max(t['blockheight'] for t in rows)+100
    proof=vectors.v.prove(dict(channel=e['incoming']['binding']['channel'],payment_hash=e['payload']['payment_hash'],
        incoming_amount_msat=e['payload']['amount_msat'],expiry=e['incoming']['expiry']),rows,funds,height)
    value=dict(format=retire.FORMAT,entry_id=ba.digest(dict(grant_id=e['grant_id'],payload=e['payload'])),
        grant_id=e['grant_id'],channel=e['incoming']['binding']['channel'],htlc=e['htlc'],decision=e['decision'],
        phase=proof['phase'],proof=proof['proof'])
    return dict(evidence=value,confirmation=ba.digest(value),confirmed_height=height,confirmed_at=NOW)


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.t=dt.Tests();self.t.setUp();self.addCleanup(self.t.doCleanups)
        t=self.t;t.setup_entry();dp.arm(t.a,t.eid);t.node.info['blockheight']=483;dp.protect(t.a,t.eid)
        t.a.resolve(t.eid,payment_preimage=PREIMAGE)
        self.entry=copy.deepcopy(t.a.record['entries'][t.eid]);self.rows,self.funds=transactions(self.entry)
        self.closed=copy.deepcopy(t.node.rows[0]);self.closed.update(leased=False,funding_withheld=False,
            closer='local',last_commitment_txid=self.rows[0]['hash'])
        t.node.rows=[];t.node.history_rows=[];t.node.info['blockheight']=700
        self.available=True;self.rowlist=[self.closed]
        original=t.node
        def rpc(method,**params):
            if method in ('listclosedchannels','listtransactions','listfunds'):
                if not self.available:raise TimeoutError('node unavailable')
                t.node.calls.append((method,copy.deepcopy(params)))
                if method=='listclosedchannels':return copy.deepcopy(dict(closedchannels=self.rowlist))
                if method=='listtransactions':return copy.deepcopy(dict(transactions=self.rows))
                self.assertEqual(params,dict(spent=True));return copy.deepcopy(dict(outputs=self.funds))
            if method=='listhtlcs':raise AssertionError('closed HTLC rows must not be required')
            return original(method,**params)
        self.rpc=rpc;self.open()
    def open(self):
        self.t.open();self.t.a.rpc=self.rpc
    def review(self):return retire.review(self.t.a,self.t.eid)
    def confirm(self):
        review=self.review();return retire.confirm(self.t.a,self.t.eid,review['confirmation'],confirmed=True)
    def test_readonly_review_and_lost_reply_preserve_exact_entry(self):
        before=self.t.j.read();review=self.review();self.assertEqual(self.t.j.read(),before)
        self.assertFalse(review['confirmed'])
        original=self.t.j.write
        def lost(record):original(record);raise OSError('lost durable write reply')
        with patch.object(self.t.j,'write',side_effect=lost),self.assertRaises(hg.StorageError):
            retire.confirm(self.t.a,self.t.eid,review['confirmation'],confirmed=True)
        self.open();saved=self.t.j.read()
        retire.confirm(self.t.a,self.t.eid,review['confirmation'],confirmed=True)
        self.assertEqual(self.t.j.read(),saved);self.t.a.prior()
        for k,v in self.entry.items():self.assertEqual(saved['entries'][self.t.eid][k],v)
        self.assertEqual(saved['entries'][self.t.eid]['terminal'],'onchain_claimed')
        self.assertEqual(len(self.t.closes()),1)
    def test_claim_and_timeout_are_distinct_and_fully_confirmed(self):
        self.t.a.record['entries'][self.t.eid]['decision']=dict(action='fail')
        self.t.node.gate['phase']='fail_intent';self.rows,self.funds=transactions(self.t.a.record['entries'][self.t.eid],False)
        self.closed['last_commitment_txid']=self.rows[0]['hash']
        self.confirm();self.assertEqual(self.t.a.status(self.t.eid)['state'],'onchain_timed_out')
    def test_pending_or_shallow_or_missing_closed_channel_refuses(self):
        baseline=copy.deepcopy(self.closed)
        changes=[lambda:self.t.node.rows.append(copy.deepcopy(self.closed)),
            lambda:self.rowlist.clear(),lambda:self.rowlist.append(copy.deepcopy(self.closed)),
            lambda:self.closed.update(funding_txid='99'*32),
            lambda:self.closed.update(channel_type=dict(bits=[])),
            lambda:self.closed.update(last_commitment_txid='88'*32),
            lambda:self.closed.update(leased=True),
            lambda:self.t.node.info.update(blockheight=max(r['blockheight'] for r in self.rows)+98)]
        for change in changes:
            self.t.node.rows=[];self.closed=copy.deepcopy(baseline);self.rowlist=[self.closed];self.t.node.info['blockheight']=700
            change()
            with self.assertRaises(ValueError):self.review()
            self.assertNotIn('terminal',self.t.a.record['entries'][self.t.eid])
    def test_reorg_or_outage_between_review_confirm_never_retires(self):
        review=self.review();saved=copy.deepcopy(self.rows)
        self.rows[-1]['blockheight']=0
        with self.assertRaises(ValueError):retire.confirm(self.t.a,self.t.eid,review['confirmation'],confirmed=True)
        self.rows=saved;self.available=False
        with self.assertRaises(TimeoutError):retire.confirm(self.t.a,self.t.eid,review['confirmation'],confirmed=True)
        self.assertNotIn('terminal',self.t.a.record['entries'][self.t.eid])
    def test_wrong_confirmation_or_missing_explicit_review_refuses(self):
        for token,confirmed in [('0'*64,True),(self.review()['confirmation'],False)]:
            with self.assertRaises(ValueError):retire.confirm(self.t.a,self.t.eid,token,confirmed=confirmed)
        self.assertNotIn('terminal',self.t.a.record['entries'][self.t.eid])
    def test_retired_history_rechecks_and_spent_wallet_output_remains_proven(self):
        self.confirm();saved=self.t.j.read()
        self.funds[0]['status']='spent';self.funds[0].pop('blockheight');self.open();self.t.a.prior()
        self.assertEqual(self.t.j.read(),saved)
        self.rows[-1]['blockheight']=0
        with self.assertRaises(ValueError):self.t.a.prior()
        self.assertEqual(self.t.j.read(),saved)
    def test_height_rollback_and_reconfirmation_change_require_attention(self):
        self.confirm();self.t.node.info['blockheight']-=1
        with self.assertRaisesRegex(ValueError,'retirement_evidence_regressed'):self.t.a.prior()
        self.t.node.info['blockheight']=700;self.rows[-1]['blockheight']+=1;self.funds[0]['blockheight']+=1
        with self.assertRaisesRegex(ValueError,'retirement_evidence_regressed'):self.t.a.prior()
    def test_restore_refuses_review_confirm_and_grant_reuse_before_rpc(self):
        token=self.review()['confirmation'];before=copy.deepcopy(self.t.node.calls)
        restore_barrier.fence(self.t.root)
        for f in (self.review,lambda:retire.confirm(self.t.a,self.t.eid,token,confirmed=True),self.t.a.prior):
            with self.assertRaisesRegex(ValueError,'restored_authority_blocked'):f()
        self.assertEqual(self.t.node.calls,before)
    def test_storage_failure_leaves_history_nonterminal(self):
        token=self.review()['confirmation']
        with patch.object(self.t.j,'write',side_effect=OSError('disk')),self.assertRaises(hg.StorageError):
            retire.confirm(self.t.a,self.t.eid,token,confirmed=True)
        self.open();self.assertNotIn('terminal',self.t.a.record['entries'][self.t.eid])
    def test_existing_runes_never_gain_retirement_mutation(self):
        g=self.t.a.record['grants'][self.t.gid]['terms']
        for op in ('recovery_review','recovery_confirm','retire-chain','close'):
            self.assertNotIn(op,remote.operations(g))
            with self.assertRaises(ValueError):https.parameters('incoming',op,dict(entry_id=self.t.eid),g)
    def test_local_admin_has_finite_retirement_actions(self):
        t=self.t;path=t.root/admin.DIRECTORY;admin.initialize(path,t.node.chain)
        t.close()
        request=dict(action='recovery_review',entry_id=t.eid)
        reviewed=admin.execute(t.root,t.node.chain,self.rpc,request,clock=lambda:NOW)
        self.assertTrue(reviewed['read_only'])
        confirmed=admin.execute(t.root,t.node.chain,self.rpc,dict(action='recovery_confirm',entry_id=t.eid,
            confirmation=reviewed['confirmation'],confirmed=True),clock=lambda:NOW)
        self.assertTrue(confirmed['confirmed']);self.open()
        with self.assertRaises(ValueError):admin.execute(t.root,t.node.chain,self.rpc,dict(request,channel_id='f'*64))
    def test_renewal_needs_new_funding_and_preserves_used_budget(self):
        t=self.t;g=t.a.record['grants'][t.gid]['terms'];self.confirm();before=copy.deepcopy(t.a.record['entries'])
        with self.assertRaises(ValueError):t.a.create_grant('incoming',g['channels'],g['limits'],NOW+3600,confirmed=True)
        row=copy.deepcopy(self.closed);row.update(channel_id='12'*32,funding_txid='13'*32,short_channel_id='200x1x0',
            state='CHANNELD_NORMAL',peer_connected=True,reestablished=True,htlcs=[])
        t.node.rows=[row];pin=hg.channel_pin(row,t.node.chain['profile'])
        new=t.a.create_grant('incoming',[pin],g['limits'],NOW+3600,confirmed=True)
        self.assertNotEqual(new,t.gid);self.assertEqual(t.a.record['entries'],before)
        self.assertIn(t.gid,t.a.record['grants']);self.assertEqual(t.a.record['current']['incoming'],new)


    def test_closed_reconciliation_keeps_saved_htlc_when_core_prunes_it(self):
        self.assertEqual(self.t.a.reconcile(self.t.eid)['state'],'onchain_recovery')
        self.assertEqual(self.t.a.record['entries'][self.t.eid]['htlc'],self.entry['htlc'])
        with self.assertRaisesRegex(ValueError,'previous_enrollment_requires_recovery'):self.t.a.prior()


class RetiredRemote(dt.ProtectedRemote):
    def __init__(self):super().__init__();self.offline=False
    def __call__(self,*args):
        if self.offline:raise TimeoutError('offline')
        result=super().__call__(*args)
        kind,gid,op,body=args
        if op=='release' or op=='fail':
            e=self.entries['incoming'];e.pop('terminal',None);e['closing']=True
            e['protection']['close']=dict(height=e['incoming']['expiry']-72,
                channel=e['incoming']['binding']['channel'],htlc=e['htlc'])
        return result
    def retired(self,release=True):
        e=self.entries['incoming'];e['onchain_retirement']=certificate(e,release)
        e['terminal']='onchain_claimed' if release else 'onchain_timed_out'


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.t=wt.Tests();self.t.setUp();self.addCleanup(self.t.doCleanups)
        t=self.t;t.remote=RetiredRemote();t.open();t.setup_swap();t.remote.hold()
        t.w.step(t.cid);t.w.step(t.cid);t.remote.complete();t.w.step(t.cid);t.w.step(t.cid)
        t.remote.retired();t.w.observe(t.cid)
    def test_explicit_separate_confirmation_then_fresh_process_idempotence(self):
        t=self.t;review=t.w.recovery_review(t.cid)
        self.assertFalse(t.w.status(t.cid)['terminal']);self.assertEqual(t.w.status(t.cid)['state'],'onchain_retirement_review')
        with self.assertRaises(ValueError):t.w.recovery_confirm(t.cid,review['confirmation'])
        t.w.recovery_confirm(t.cid,review['confirmation'],True);t.open();before=t.journal.read()
        self.assertEqual(t.w.recovery_confirm(t.cid,review['confirmation'],True)['state'],'onchain_settled')
        self.assertEqual(t.journal.read(),before);self.assertEqual(t.sends(),1)
    def test_changed_attempt_or_uncertainty_refuses_confirmation(self):
        t=self.t;token=t.w.recovery_review(t.cid)['confirmation'];t.remote.offline=True
        with self.assertRaises(TimeoutError):t.w.recovery_confirm(t.cid,token,True)
        t.remote.offline=False;t.remote.entries['outgoing']['attempt']['identity']['created_index']+=1
        with self.assertRaises(ValueError):t.w.recovery_confirm(t.cid,token,True)
        self.assertNotIn('recovery_retirement',t.w.record['swaps'][t.cid])
    def test_lost_local_write_reply_recovers_once_without_rpc_mutation(self):
        t=self.t;token=t.w.recovery_review(t.cid)['confirmation'];old=t.journal.write
        def lost(r):
            old(r)
            if 'recovery_retirement' in r['swaps'][t.cid]:raise OSError('crash after replace')
        with patch.object(t.journal,'write',side_effect=lost),self.assertRaises(hg.StorageError):t.w.recovery_confirm(t.cid,token,True)
        t.open();before=len(t.remote.calls);t.w.recovery_confirm(t.cid,token,True)
        self.assertTrue(all(op=='evidence' for _,op,_ in t.remote.calls[before:]));self.assertEqual(t.sends(),1)
    def test_restored_worker_cannot_retire(self):
        t=self.t;token=t.w.recovery_review(t.cid)['confirmation'];restore_barrier.fence(t.root.parent)
        with self.assertRaisesRegex(ValueError,'restored_worker_blocked'):t.w.recovery_confirm(t.cid,token,True)
    def test_retired_worker_step_requires_fresh_original_evidence(self):
        t=self.t;token=t.w.recovery_review(t.cid)['confirmation'];t.w.recovery_confirm(t.cid,token,True)
        t.remote.offline=True
        with self.assertRaises(TimeoutError):t.w.step(t.cid)
        self.assertEqual(t.sends(),1)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        import test_service as st
        from test_customer_https import nodes
        self.t=st.Tests();self.t.setUp();self.addCleanup(self.t.doCleanups)
        t=self.t;t.remote=RetiredRemote();t.open();t.bind=dict(direction='forward',nodes=nodes(t.remote));t.enrolled()
        t.remote.hold();t.s.tick('11'*32);t.s.tick('11'*32);t.remote.complete()
        t.s.tick('11'*32);t.s.tick('11'*32);t.remote.retired();t.s.tick('11'*32)
    def test_service_actions_retirement_and_admission_recheck_original_pairing(self):
        import service_cli
        t=self.t;t.close()
        def action(**request):return service_cli.execute(t.root,request,factory=t.factory,clock=lambda:NOW)
        r=action(action='recovery_review',cid=t.cid)
        self.assertFalse(r['confirmed'])
        result=action(action='recovery_confirm',cid=t.cid,confirmation=r['confirmation'],confirmed=True)
        self.assertEqual(result['state'],'onchain_settled')
        t.open();t.remote.offline=True
        with self.assertRaises(TimeoutError):t.s.prepare('forward','different-invoice')
        self.assertEqual(sum(op=='send' for _,op,_ in t.remote.calls),1)
    def test_service_tick_rechecks_retired_history_and_flags_attention(self):
        t=self.t;r=t.s.recovery_review(t.cid);t.s.recovery_confirm(t.cid,r['confirmation'],True)
        t.remote.offline=True;t.s.tick('11'*32)
        self.assertTrue(t.s.cached(t.cid)['blocked']);self.assertTrue(t.s.cached(t.cid)['recovery_recheck_required'])
        t.remote.offline=False;t.s.tick('11'*32)
        self.assertFalse(t.s.cached(t.cid)['blocked']);self.assertNotIn('recovery_recheck_required',t.s.cached(t.cid))

if __name__=='__main__':unittest.main()
