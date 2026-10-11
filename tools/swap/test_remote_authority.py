#!/usr/bin/env python3
"""Credential durability and remote dispatch boundaries; no daemon claim."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import authority_plugin as plugin
import bounded_authority as ba
import held_gate as hg
import remote_authority as remote
from test_bounded_authority import Node, bounds
from test_route_plan import NOW, PREIMAGE, pin


def rune_row(gid, kind, uid='1'):
    return dict(rune='A'*44+uid, unique_id=uid, restrictions=[dict(alternatives=[
        dict(fieldname=s.split('=')[0], condition='=', value=s.split('=', 1)[1], english='fixture')
        for s in group], english='fixture') for group in remote.restrictions(gid, kind)])


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.node = Node(); self.now = NOW
        ba.initialize_fixture(self.root/'authority', self.node.chain)
        remote.initialize_fixture(self.root/'remote', self.node.chain)
        self.rows, self.setup_calls, self.drop = [], [], None
        self.j = self.rj = None; self.open(); self.addCleanup(self.close)

    def rpc(self, method, **params):
        self.setup_calls.append(method)
        if method == 'showrunes': return dict(runes=copy.deepcopy(self.rows))
        self.assertEqual(method, 'createrune')
        self.rows.append(rune_row(self.gid, self.kind, str(len(self.rows)+1)))
        self.assertEqual(params, dict(restrictions=remote.restrictions(self.gid, self.kind)))
        if self.drop:
            self.drop = None
            raise TimeoutError('private details that must not be returned')
        return {k:self.rows[-1][k] for k in ('rune','unique_id')}

    def close(self):
        if self.rj: self.rj.close()
        if self.j: self.j.close()

    def open(self):
        self.close(); self.j = hg.Journal(self.root/'authority'); self.rj = hg.Journal(self.root/'remote')
        self.a = ba.Authority(self.j, self.node.chain, self.node, lambda:self.now)
        self.r = remote.Remote(self.a, self.rj, self.rpc)

    def grant(self, kind='outgoing'):
        self.kind = kind
        self.gid = self.a.create_grant(kind, [pin()], bounds(kind), NOW+3600, confirmed=True)
        return self.gid

    def call(self, operation, body=None, gid=None):
        return self.r.call(dict(grant_id=gid or self.gid, operation=operation, body=body or {}))

    def enroll(self, kind='outgoing'):
        self.grant(kind); self.r.enable(self.gid, confirmed=True)
        data = self.node.plan() if kind == 'outgoing' else self.node.quote_data()
        self.node.plan_value = data
        body = dict(data=data, confirmation=ba.digest(data))
        eid = self.call('enroll', body)['entry_id']
        return eid, body

    def test_lost_mint_reply_recovers_original_once_after_pause_expiry(self):
        self.grant(); self.drop = True
        with self.assertRaises(TimeoutError): self.r.enable(self.gid, confirmed=True)
        self.assertEqual(self.r.record['credentials'][self.gid], dict(intent=True))
        self.open(); self.a.pause(self.gid); self.now += 10000
        saved = self.r.enable(self.gid, confirmed=True)
        self.open(); self.assertEqual(self.r.enable(self.gid, confirmed=True), saved)
        self.assertEqual(self.setup_calls.count('createrune'), 1)

    def test_zero_inventory_after_durable_intent_never_remints(self):
        self.grant()
        original = self.r.commit
        def crash(record): original(record); raise RuntimeError('crash before mint')
        with patch.object(self.r, 'commit', side_effect=crash), self.assertRaises(RuntimeError):
            self.r.enable(self.gid, confirmed=True)
        self.open()
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'credential_outcome_unknown'):
                self.r.enable(self.gid, confirmed=True)
        self.assertNotIn('createrune', self.setup_calls)
        with self.assertRaisesRegex(ValueError, 'credential_setup_unfinished'): self.call('info')

    def test_storage_failure_freezes_before_mint_and_after_reply(self):
        self.grant()
        with patch.object(self.rj, 'write', side_effect=OSError('disk')), self.assertRaises(hg.StorageError):
            self.r.enable(self.gid, confirmed=True)
        self.assertNotIn('createrune', self.setup_calls)
        with self.assertRaisesRegex(ValueError, 'remote_blocked'): self.r.enable(self.gid, confirmed=True)
        self.open(); original = self.rj.write
        def after(record):
            original(record)
            if 'credential' in record['credentials'][self.gid]: raise OSError('lost fsync reply')
        with patch.object(self.rj, 'write', side_effect=after), self.assertRaises(hg.StorageError):
            self.r.enable(self.gid, confirmed=True)
        self.open(); self.r.enable(self.gid, confirmed=True)
        self.assertEqual(self.setup_calls.count('createrune'), 1)

    def test_changed_duplicate_missing_or_revoked_credential_refuses(self):
        self.grant(); saved = self.r.enable(self.gid, confirmed=True); original = copy.deepcopy(self.rows)
        variants = [[], original*2, [dict(original[0], blacklisted=True)],
                    [dict(original[0], stored=False)], [dict(original[0], our_rune=False)],
                    [dict(original[0], rune='B'*48)]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.rows = rows; self.open()
                with self.assertRaises(ValueError): self.r.enable(self.gid, confirmed=True)
        self.rows = original; self.open(); self.assertEqual(self.r.enable(self.gid, confirmed=True), saved)
        self.assertEqual(self.setup_calls.count('createrune'), 1)

    def test_unowned_rune_cannot_be_adopted(self):
        self.grant(); self.rows = [rune_row(self.gid, self.kind)]
        with self.assertRaisesRegex(ValueError, 'unowned_credential'): self.r.enable(self.gid, confirmed=True)
        self.assertEqual(self.r.record['credentials'], {})

    def test_dispatch_exact_fields_kind_and_grant_membership(self):
        eid, body = self.enroll()
        original = copy.deepcopy(self.a.record)
        for op in ('publish', 'release', 'fail', 'createrune', 'close', 'sendpay', '__dict__'):
            with self.assertRaises(ValueError): self.call(op, dict(entry_id=eid))
        for p in ([], dict(grant_id=self.gid, operation='info'),
                  dict(grant_id=self.gid, operation='info', body={}, extra=True)):
            with self.assertRaises(ValueError): self.r.call(p)
        with self.assertRaises(ValueError): self.call('send', dict(entry_id=eid, route=[]))
        with self.assertRaises(ValueError): self.call('enroll', dict(body, confirmation=True))
        with self.assertRaises(ValueError): self.call('enroll', dict(body, confirmation='0'*64))
        self.assertEqual(original, self.a.record)
        # A valid different-kind grant cannot inspect an outgoing enrollment.
        self.a.record['entries'] = {}  # Test setup only, then restore both grants.
        other = self.a.create_grant('incoming', [pin()], bounds('incoming'), NOW+3600, confirmed=True)
        self.a.record['entries'] = original['entries']; self.a.commit(copy.deepcopy(self.a.record))
        self.kind = 'incoming'; self.gid = other
        self.r.enable(other, confirmed=True)
        for op in ('evidence', 'reconcile', 'publish', 'fail'):
            with self.assertRaisesRegex(ValueError, 'enrollment_outside_grant'):
                self.call(op, dict(entry_id=eid))

    def test_readonly_info_and_scope_never_disclose_saved_credentials(self):
        self.grant(); self.r.enable(self.gid, confirmed=True)
        before = [p.read_bytes() for p in (self.root/'authority/state.json', self.root/'remote/state.json')]
        value = self.call('info'); value['grant']['channels'].clear()
        self.assertEqual(before, [p.read_bytes() for p in (self.root/'authority/state.json', self.root/'remote/state.json')])
        self.assertTrue(self.a.record['grants'][self.gid]['terms']['channels'])
        self.assertNotIn('rune', json.dumps(value)); self.assertNotIn('unique_id', json.dumps(value))

    def test_lost_send_exact_reenrollment_and_evidence_after_restart(self):
        eid, body = self.enroll(); self.node.fail_reply = 'sendpay'
        with self.assertRaises(TimeoutError): self.call('send', dict(entry_id=eid))
        self.open(); self.a.pause(self.gid); self.now += 10000
        self.assertEqual(self.call('enroll', body), dict(entry_id=eid))
        self.assertEqual(self.call('send', dict(entry_id=eid))['state'], 'pending')
        old = self.call('evidence', dict(entry_id=eid))['entry']
        self.node.finish(); self.open()
        final = self.call('evidence', dict(entry_id=eid))
        self.assertEqual(final['entry']['attempt']['identity'], old['attempt']['identity'])
        self.assertEqual(final['entry']['attempt']['preimage'], PREIMAGE)
        self.assertTrue(final['status']['terminal'])
        self.assertEqual(sum(m == 'sendpay' for m,p in self.node.calls), 1)

    def test_incoming_exact_release_recovery_and_opposite_refused(self):
        eid, _ = self.enroll('incoming'); self.call('publish', dict(entry_id=eid)); self.node.hold()
        original = self.call('evidence', dict(entry_id=eid))['entry']['incoming']
        for invalid in (None, '', True, '0'*64):
            with self.assertRaises(ValueError): self.call('release',dict(entry_id=eid,preimage=invalid))
        self.assertEqual(self.node.effective_resolutions,0)
        self.node.fail_reply = 'swap-gate-release'
        body = dict(entry_id=eid, preimage=PREIMAGE)
        with self.assertRaises(TimeoutError): self.call('release', body)
        self.open(); self.a.pause(self.gid); self.now += 10000
        self.call('release', body)
        with self.assertRaises(ValueError): self.call('fail', dict(entry_id=eid))
        self.node.finish(incoming=True)
        self.assertEqual(self.call('evidence', dict(entry_id=eid))['entry']['incoming'], original)
        self.assertEqual(self.node.effective_resolutions, 1)

    def test_restore_legacy_wrong_profile_and_confirmation_blocked(self):
        self.grant()
        with self.assertRaises(ValueError): self.r.enable(self.gid)
        self.r.enable(self.gid, confirmed=True)
        for folder in ('remote', 'authority'):
            path = self.root/folder/'restored.json'; path.symlink_to('absent')
            with self.assertRaises(ValueError): self.call('info')
            with self.assertRaises(ValueError): self.r.enable(self.gid, confirmed=True)
            path.unlink()
        for change in (dict(schema=True), dict(format='startos-forward-repeat-v1')):
            self.rj.write(dict(self.r.record, **change))
            with self.assertRaises(ValueError): remote.Remote(self.a, self.rj, self.rpc)
        self.rj.write(self.r.record)
        changed = copy.deepcopy(self.r.record); changed['chain']['profile'] = ba.ci.PRIVKEYIO_XBT
        self.rj.write(changed)
        with self.assertRaises(ValueError): remote.Remote(self.a, self.rj, self.rpc)

    def test_plugin_static_guard_and_rpc_allowlist(self):
        for config in (dict(network='bitcoin', startup=True), dict(network='regtest', startup=False)):
            with self.assertRaisesRegex(ValueError, 'static_regtest_required'):
                plugin.Service(config, ba.ci.BTC)
        rpc = plugin.RPC(dict(**{'rpc-file':'lightning-rpc', 'lightning-dir':str(self.root)}))
        for method in ('close', 'pay', 'xpay', 'sendonion', 'blacklistrune', 'getroutes'):
            with self.assertRaisesRegex(ValueError, 'local_rpc_refused'): rpc(method)
        self.assertFalse(plugin.manifest()['dynamic'])
        self.assertEqual(plugin.manifest()['hooks'], [])


if __name__ == '__main__': unittest.main()
