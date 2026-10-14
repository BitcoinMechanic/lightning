"""Durable single-part cross-chain worker, explicit BTC/new-XBT regtest only.

The caller supplies authenticated, fixed-endpoint customer RPC adapters. This
module never accepts raw RPC, chooses routes, reprices or submits a payment
twice. A durable send intent with no observed attempt stays uncertain.
"""
import copy
import json
import os
from pathlib import Path
import time

import bounded_authority as ba
import cross_contract as cc
import customer_invoice as invoice
import held_gate as hg
import restore_barrier

FORMAT = 'swap-cross-worker-v1'
KINDS = ('outgoing', 'incoming')


def enrollment(c, kind):
    return ba.digest(dict(grant_id=ba.digest(c['grants'][kind]), payload=c[kind]))


def evidence(c, kind, value):
    g = c['grants'][kind]; gid = ba.digest(g); eid = enrollment(c, kind)
    hg.need(type(value) is dict and set(value) == {'entry_id', 'entry', 'status'}
            and value['entry_id'] == eid, 'worker_evidence_invalid')
    e = value['entry']
    ba.validate(dict(schema=1, format=ba.FORMAT, chain=g['chain'],
        current={k:gid if k == kind else None for k in ba.KINDS},
        grants={gid:dict(terms=g, paused=False)}, entries={eid:e}), g['chain'])
    hg.need(e['grant_id'] == gid and ba.equal(e['payload'], c[kind]), 'worker_enrollment_changed')
    state = ba.entry_state(e)
    hg.need(ba.equal(value['status'], dict(kind=kind, state=state, terminal='terminal' in e,
            regtest_only=True, blocked=False)), 'worker_evidence_status_invalid')
    return e


def monotonic(old, new):
    for key, value in old.items():
        hg.need(key in new, 'worker_evidence_regressed')
        if key == 'publication':
            hg.need(value == new[key] or (value == 'intent' and new[key] == 'registered'), 'worker_evidence_regressed')
        elif key == 'attempt':
            hg.need(ba.equal(value['identity'], new[key]['identity'])
                    and (value['state'] == 'pending' or ba.equal(value, new[key])), 'worker_attempt_changed')
        elif key == 'protection':
            monotonic(value, new[key])
        else:
            hg.need(ba.equal(value, new[key]), 'worker_evidence_changed')


def enrolled_admission(c, observations, now):
    cc.validate(c)
    hg.need(hg.integer(now, c['created_at'], c['pay_until']-1), 'contract_admission_expired')
    hg.need(type(observations) is dict and set(observations) == set(KINDS), 'contract_inspection_unavailable')
    for kind in KINDS:
        cc.inspection(c, kind, observations[kind], now, entry_id=enrollment(c, kind))


def publication(c, value):
    hg.need(type(value) is dict and set(value) == {'format', 'entry_id', 'invoice', 'expires_at'}
            and value['format'] == invoice.FORMAT and value['entry_id'] == enrollment(c, 'incoming')
            and type(value['invoice']) is str and value['invoice'].startswith('lnbcrt')
            and 100 <= len(value['invoice']) <= 16384
            and ba.equal(value['expires_at'], c['pay_until']), 'worker_invoice_invalid')
    # The scoped publisher authenticates every signed field with CLN decode.
    # Its returned bytes, once saved, can never be replaced by a new invoice.


def validate(record):
    hg.need(type(record) is dict and set(record) == {'schema', 'format', 'swaps'}
            and type(record['schema']) is int and record['schema'] == 1 and record['format'] == FORMAT
            and type(record['swaps']) is dict and len(record['swaps']) <= ba.MAX_RECORDS, 'worker_format_refused')
    hashes = set()
    for cid, s in record['swaps'].items():
        hg.need(type(s) is dict and {'contract', 'enroll', 'remote'} <= set(s)
            and set(s) <= {'contract','enroll','remote','approval','publish','invoice','send','decision','retire'},
            'worker_record_invalid')
        c = cc.validate(s['contract']); hg.need(cid == cc.digest(c), 'worker_contract_changed')
        ph = c['outgoing']['payment_hash']; hg.need(ph not in hashes, 'worker_hash_reused'); hashes.add(ph)
        hg.need(type(s['enroll']) is dict and set(s['enroll']) <= set(KINDS)
                and all(v in ('intent','done') for v in s['enroll'].values())
                and type(s['remote']) is dict and set(s['remote']) <= set(KINDS), 'worker_record_invalid')
        if 'approval' not in s:
            hg.need(set(s) == {'contract','enroll','remote'} and not s['enroll'] and not s['remote'],
                    'worker_unapproved_mutation')
            continue
        a = s['approval']
        hg.need(type(a) is dict and set(a) == {'digest','at'} and a['digest'] == cid
                and hg.integer(a['at'], c['created_at'], c['pay_until']-1), 'worker_approval_changed')
        if 'incoming' in s['enroll']:
            hg.need(s['enroll'].get('outgoing') == 'done', 'worker_enrollment_order')
        for kind, value in s['remote'].items():
            hg.need(s['enroll'].get(kind) == 'done', 'worker_unowned_evidence')
            e = evidence(c, kind, value)
            if kind == 'outgoing' and 'send' in e: hg.need('send' in s, 'worker_unowned_send')
            if kind == 'incoming' and 'decision' in e:
                hg.need(ba.equal(e['decision'], s.get('decision', {}).get('resolution')), 'worker_unowned_resolution')
        if 'publish' in s:
            hg.need(s['publish'] is True and s['enroll'] == dict.fromkeys(KINDS,'done'), 'worker_publish_invalid')
        if 'invoice' in s:
            hg.need(s.get('publish') is True, 'worker_invoice_without_publication'); publication(c, s['invoice'])
        if 'send' in s:
            send = s['send']
            hg.need('invoice' in s and type(send) is dict
                    and set(send) == {'at','incoming','outgoing','observations'}, 'worker_send_invalid')
            enrolled_admission(c, send['observations'], send['at'])
            cc.held_margin(c, send['incoming'], incoming_height=send['observations']['incoming']['blockheight'])
            if ba.protected(c['grants']['incoming']):
                hg.need('protection' in send['incoming']['entry'], 'worker_send_without_protection')
            out = evidence(c, 'outgoing', send['outgoing'])
            hg.need(set(out) == {'grant_id','payload'}, 'worker_send_not_fresh')
            hg.need('incoming' in s['remote'] and 'outgoing' in s['remote'], 'worker_send_evidence_missing')
            monotonic(send['incoming']['entry'], s['remote']['incoming']['entry'])
            monotonic(send['outgoing']['entry'], s['remote']['outgoing']['entry'])
        if 'decision' in s:
            d = s['decision']; hg.need(type(d) is dict and set(d) == {'resolution','reason'}, 'worker_decision_invalid')
            hg.need('incoming' in s['remote'] and 'incoming' in s['remote']['incoming']['entry']
                    and 'outgoing' in s['remote'], 'worker_decision_without_evidence')
            out = s['remote']['outgoing']['entry']; resolution = d['resolution']
            if d['reason'] == 'outgoing_complete':
                hg.need('send' in s and out.get('terminal') == 'complete'
                        and ba.equal(resolution,dict(action='release',preimage=out['attempt']['preimage'])),
                        'worker_release_without_settlement')
            elif d['reason'] == 'outgoing_failed':
                hg.need('send' in s and out.get('terminal') == 'failed'
                        and resolution == dict(action='fail'), 'worker_fail_without_settlement')
            else:
                hg.need(d['reason'] in ('cancelled','admission_expired') and 'send' not in s
                        and 'send' not in out and resolution == dict(action='fail'), 'worker_unsafe_cancellation')
        if 'retire' in s:
            hg.need(s['retire'] is True and 'send' not in s, 'worker_retirement_invalid')
    return record


def initialize_fixture(directory):
    directory = Path(directory)
    hg.need(not any(p.is_symlink() for p in (directory,*directory.parents)), 'worker_symlink_refused')
    hg.need(not restore_barrier.blocked(directory), "restored_initialization_blocked")
    directory.mkdir(mode=0o700)
    fd = os.open(directory/'state.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(dict(schema=1,format=FORMAT,swaps={}),stream); stream.flush(); os.fsync(stream.fileno())
    for p in (directory,directory.parent):
        fd = os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


class Worker:
    """Caller owns the journal lock and fixed authenticated transport.

    call(kind, grant_id, operation, body) must authenticate the pinned node and
    use only the saved customer credential. Exceptions never imply failure.
    """
    def __init__(self, journal, call, clock=time.time):
        self.journal, self.call, self.clock = journal, call, clock
        self.record = copy.deepcopy(validate(journal.read())); self.blocked = False

    def guard(self):
        hg.need(not self.blocked and self.journal.lock_fd is not None, 'worker_blocked')
        hg.need(not restore_barrier.blocked(self.journal.directory), 'restored_worker_blocked')

    def commit(self, cid, swap):
        self.guard(); record = copy.deepcopy(self.record); record['swaps'][cid] = copy.deepcopy(swap); validate(record)
        try: self.journal.write(record)
        except Exception:
            self.blocked = True; raise hg.StorageError('worker_write_uncertain') from None
        self.record = record

    def swap(self, cid):
        self.guard(); hg.need(type(cid) is str and cid in self.record['swaps'], 'worker_unknown_swap')
        return copy.deepcopy(self.record['swaps'][cid])

    def rpc(self, s, kind, op, body=None):
        self.guard()
        return self.call(kind, ba.digest(s['contract']['grants'][kind]), op, copy.deepcopy(body or {}))

    def inspections(self, s, *, enrolled=False):
        c = s['contract']
        return {k:self.rpc(s,k,'inspect',dict(entry_id=enrollment(c,k)) if enrolled else
            dict(amount_msat=c['incoming']['amount_msat']) if k == 'incoming' else {}) for k in KINDS}

    def prepare(self, contract):
        self.guard(); c = copy.deepcopy(cc.validate(contract)); cid = cc.digest(c)
        if cid in self.record['swaps']: return cid
        hg.need(all(self.status(k)['terminal'] for k in self.record['swaps']), 'worker_previous_swap_requires_recovery')
        s = dict(contract=c,enroll={},remote={})
        cc.admission(c,self.inspections(s),now=int(self.clock()))
        self.commit(cid,s); return cid

    def confirm(self, cid, confirmation):
        s = self.swap(cid); hg.need(confirmation == cid and type(confirmation) is str, 'exact_confirmation_required')
        if 'approval' not in s:
            # Replies may cross a wall-clock second or consume the quote's
            # remaining lifetime. Validate and timestamp only after both arrive.
            reports = self.inspections(s); now = int(self.clock())
            cc.confirm(s['contract'],confirmation,reports,now=now)
            s['approval'] = dict(digest=cid,at=now); self.commit(cid,s)
        return self.status(cid)

    def observe(self, cid):
        s = self.swap(cid)
        for kind in KINDS:
            hg.need(s['enroll'].get(kind) == 'done', 'worker_setup_unfinished')
            value = self.rpc(s,kind,'evidence',dict(entry_id=enrollment(s['contract'],kind)))
            e = evidence(s['contract'],kind,value)
            if kind in s['remote']: monotonic(s['remote'][kind]['entry'],e)
            s['remote'][kind] = copy.deepcopy(value); self.commit(cid,s)
        return s

    def decide(self, cid, s, reason):
        hg.need('decision' not in s, 'worker_resolution_changed')
        r = dict(action='release',preimage=s['remote']['outgoing']['entry']['attempt']['preimage']) \
            if reason == 'outgoing_complete' else dict(action='fail')
        s['decision'] = dict(reason=reason,resolution=r); self.commit(cid,s)

    def cancel(self, cid, confirmation):
        hg.need(type(confirmation) is str and confirmation == cid, 'exact_confirmation_required')
        s = self.observe(cid)
        hg.need('send' not in s and 'send' not in s['remote']['outgoing']['entry'], 'worker_send_uncertain')
        if 'decision' not in s: self.decide(cid,s,'cancelled')
        else: hg.need(s['decision']['reason'] == 'cancelled', 'worker_resolution_changed')
        return self.status(cid)

    def step(self, cid):
        s = self.swap(cid); c = s['contract']
        if 'approval' not in s: return self.status(cid)
        for kind in KINDS:
            if s['enroll'].get(kind) != 'done':
                if kind not in s['enroll']:
                    # Budget checks only apply to a new enrollment. Replaying
                    # an exact persisted intent must recover the same record.
                    reports = self.inspections(s); now = int(self.clock())
                    cc.admission(c,reports,now=now,enrolled=True)
                    g = c['grants'][kind]; info = reports[kind]['info']
                    hg.need(info['remaining'] > 0 and info['reserved_msat']+ba.payload(c[kind],g)
                            <= g['limits']['total_amount_msat'], 'contract_budget_exhausted')
                    s['enroll'][kind] = 'intent'; self.commit(cid,s)
                reply = self.rpc(s,kind,'enroll',dict(data=c[kind],confirmation=ba.digest(c[kind])))
                hg.need(ba.equal(reply,dict(entry_id=enrollment(c,kind))), 'worker_enrollment_reply_invalid')
                s['enroll'][kind] = 'done'; self.commit(cid,s); return self.status(cid)
        if 'invoice' not in s:
            if 'publish' not in s:
                cc.admission(c,self.inspections(s),now=int(self.clock()),enrolled=True)
                s['publish'] = True; self.commit(cid,s)
            reply = self.rpc(s,'incoming','publish',dict(entry_id=enrollment(c,'incoming')))
            hg.need(ba.equal(reply,dict(registered=True)), 'worker_publication_uncertain')
            value = self.rpc(s,'incoming','invoice',dict(entry_id=enrollment(c,'incoming')))
            publication(c,value); s['invoice'] = copy.deepcopy(value); self.commit(cid,s)
            return self.status(cid)
        if 'send' in s and ba.protected(c['grants']['incoming']):
            # Protect before contacting the outgoing node. Its transport may
            # be down while the independently timed incoming HTLC expires.
            self.rpc(s,'incoming','protect',dict(entry_id=enrollment(c,'incoming')))
        s = self.observe(cid); inc = s['remote']['incoming']['entry']; out = s['remote']['outgoing']['entry']
        if 'decision' in s:
            if 'terminal' not in inc:
                r = s['decision']['resolution']; body = dict(entry_id=enrollment(c,'incoming'))
                if r['action'] == 'release': body['preimage'] = r['preimage']
                self.rpc(s,'incoming',r['action'],body)
            elif 'send' not in s and 'terminal' not in out and int(self.clock()) >= c['outgoing']['expires_at']:
                self.retire(cid,s)
        elif 'send' in s:
            if out.get('terminal') in ('complete','failed'):
                self.decide(cid,s,'outgoing_'+out['terminal'])
            # Never call send again, even if remote evidence has no send intent.
        elif 'incoming' in inc:
            if int(self.clock()) >= c['pay_until']:
                self.decide(cid,s,'admission_expired')
            else:
                reports = self.inspections(s,enrolled=True); now = int(self.clock())
                enrolled_admission(c,reports,now)
                cc.held_margin(c,s['remote']['incoming'],incoming_height=reports['incoming']['blockheight'])
                if ba.protected(c['grants']['incoming']) and 'protection' not in inc:
                    self.rpc(s,'incoming','arm',dict(entry_id=enrollment(c,'incoming')))
                    return self.status(cid)  # Fresh observations before the sole send.
                s['send'] = dict(at=now,incoming=copy.deepcopy(s['remote']['incoming']),
                    outgoing=copy.deepcopy(s['remote']['outgoing']),observations=reports)
                self.commit(cid,s)
                self.rpc(s,'outgoing','send',dict(entry_id=enrollment(c,'outgoing')))
        elif inc.get('terminal') == 'expired' and 'terminal' not in out and int(self.clock()) >= c['outgoing']['expires_at']:
            self.retire(cid,s)
        return self.status(cid)

    def retire(self, cid, s):
        s['retire'] = True; self.commit(cid,s)
        eid = enrollment(s['contract'],'outgoing')
        self.rpc(s,'outgoing','retire',dict(entry_id=eid,confirmation=eid))

    def payment_invoice(self, cid):
        s = self.swap(cid); hg.need('invoice' in s, 'worker_invoice_unavailable')
        return copy.deepcopy(s['invoice'])

    def status(self, cid):
        hg.need(type(cid) is str and cid in self.record['swaps'], 'worker_unknown_swap')
        s = self.record['swaps'][cid]; remote = s['remote']
        inc = remote.get('incoming',{}).get('entry',{}); out = remote.get('outgoing',{}).get('entry',{})
        terminal = False
        if inc.get('terminal') == 'released' and out.get('terminal') == 'complete': state,terminal = 'settled',True
        elif inc.get('terminal') == 'failed' and out.get('terminal') in ('failed','retired'): state,terminal = 'failed',True
        elif inc.get('terminal') == 'expired' and out.get('terminal') == 'retired': state,terminal = 'expired',True
        elif inc.get('closing'): state = 'onchain_recovery'
        elif inc.get('protection', {}).get('close'): state = 'close_outcome_pending'
        elif 'decision' in s: state = 'resolving'
        elif 'send' in s: state = 'outgoing_'+out.get('attempt',{}).get('state','uncertain')
        elif inc.get('terminal') == 'expired': state = 'waiting_for_retirement'
        elif 'incoming' in inc: state = 'incoming_held'
        elif 'invoice' in s: state = 'waiting_for_payment'
        elif 'approval' in s: state = 'setting_up'
        else: state = 'ready_for_confirmation'
        return dict(format=FORMAT,direction=s['contract']['direction'],state=state,terminal=terminal,
            blocked=self.blocked or restore_barrier.blocked(self.journal.directory),regtest_only=True)
