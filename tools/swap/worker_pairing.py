"""Immutable HTTPS pairing for a regtest worker; fresh explicit setup only.

Pairing and worker journals are separate; callers lock pairing before worker.
Only read-only checks precede the atomic pairing save. No legacy state import,
credential rotation, automatic initialization or production activation.
"""
import copy
import json
import os
from pathlib import Path
import time

import bounded_authority as ba
import cross_contract as cc
import cross_worker as cw
import customer_https as https
import held_gate as hg

FORMAT = 'swap-worker-https-pairing-v1'


def binding(value):
    hg.need(type(value) is dict and set(value) == {'direction','nodes'} and type(value['nodes']) is dict
            and set(value['nodes']) == set(cw.KINDS), 'https_pairing_invalid')
    for n in value['nodes'].values(): https.configuration(n)
    cc.grant_pair({k:n['grant'] for k,n in value['nodes'].items()},value['direction'])
    hg.need(len({n['url'] for n in value['nodes'].values()}) == 2, 'https_endpoints_not_distinct')
    return value


def validate(value):
    hg.need(type(value) is dict and set(value) == {'schema','format','binding'}
            and type(value['schema']) is int and value['schema'] == 1 and value['format'] == FORMAT,
            'https_pairing_format_refused')
    if value['binding'] is not None: binding(value['binding'])
    return value


def initialize_fixture(directory):
    directory = Path(directory)
    hg.need(not any(p.is_symlink() for p in (directory,*directory.parents)), 'https_pairing_symlink_refused')
    directory.mkdir(mode=0o700)
    fd = os.open(directory/'state.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(dict(schema=1,format=FORMAT,binding=None),stream); stream.flush(); os.fsync(stream.fileno())
    for path in (directory,directory.parent):
        fd = os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


class Pairing:
    def __init__(self, journal, *, factory=https.Client, clock=time.time):
        self.journal,self.factory,self.clock = journal,factory,clock
        self.record = copy.deepcopy(validate(journal.read())); self.blocked = False

    def guard(self):
        hg.need(not self.blocked and self.journal.lock_fd is not None, 'https_pairing_blocked')
        hg.need(not os.path.lexists(self.journal.directory/'restored.json'), 'restored_pairing_blocked')

    def save(self, proposed, *, confirmed=False):
        self.guard(); hg.need(confirmed is True, 'confirmation_required')
        proposed = copy.deepcopy(binding(proposed))
        if self.record['binding'] is not None:
            hg.need(ba.equal(proposed,self.record['binding']), 'https_pairing_immutable')
            return self.status()
        for node in proposed['nodes'].values():
            g = node['grant']; observed = https.info(self.factory(node).call('info'),g)
            hg.need(observed['current'] is True and observed['paused'] is False and observed['remaining'] > 0,
                    'https_pairing_grant_unavailable')
        # Either grant can expire while waiting for the other node's reply.
        now = int(self.clock())
        hg.need(all(n['grant']['created_at'] <= now < n['grant']['expires_at']
                    for n in proposed['nodes'].values()), 'https_pairing_grant_unavailable')
        record = dict(self.record,binding=proposed); self.guard()
        try: self.journal.write(record)
        except Exception:
            self.blocked = True; raise hg.StorageError('https_pairing_write_uncertain') from None
        self.record = record
        return self.status()

    def worker(self, journal):
        # Constructing a reader must remain possible behind a restore barrier;
        # every executable worker method checks both guards before transport.
        hg.need(self.journal.lock_fd is not None, 'https_pairing_blocked')
        hg.need(self.record['binding'] is not None, 'https_pairing_required')
        saved = self.record['binding']
        clients = {k:self.factory(n) for k,n in saved['nodes'].items()}
        def call(kind, gid, op, body):
            self.guard()
            hg.need(kind in clients and gid == ba.digest(saved['nodes'][kind]['grant']), 'https_pairing_grant_changed')
            return clients[kind].call(op,body)
        return BoundWorker(journal,call,self,clock=self.clock)

    def check_contract(self, c):
        saved = self.record['binding']
        hg.need(saved is not None and c['direction'] == saved['direction']
                and ba.equal(c['grants'],{k:n['grant'] for k,n in saved['nodes'].items()}), 'https_pairing_contract_changed')

    def status(self):
        return dict(format=FORMAT,paired=self.record['binding'] is not None,
            blocked=self.blocked or os.path.lexists(self.journal.directory/'restored.json'),
            read_only=True,regtest_only=True)


class BoundWorker(cw.Worker):
    def __init__(self, journal, call, pairing, *, clock=time.time):
        self.pairing = pairing
        super().__init__(journal,call,clock=clock)
        for s in self.record['swaps'].values(): pairing.check_contract(s['contract'])

    def guard(self):
        self.pairing.guard(); super().guard()

    def prepare(self, contract):
        self.pairing.check_contract(cc.validate(contract))
        return super().prepare(contract)

    def status(self, cid):
        value = super().status(cid)
        value['blocked'] |= self.pairing.status()['blocked']
        return value
