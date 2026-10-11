#!/usr/bin/env python3
"""Security and durable-transition tests; funded daemon coverage is separate."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chain_identity as ci
import gate_plugin as plugin
import held_gate as hg

PREIMAGE = '53'*32
HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)/'journal'
        self.chain = ci.binding(ci.PRIVKEYIO_XBT, 'regtest', '02'+'11'*32)
        vector = lambda value: value.to_bytes((value.bit_length()+7)//8, 'big').hex()
        self.info = dict(id=self.chain['node_id'], network='regtest', blockheight=100,
                         our_features=dict(init=vector((1<<512)|(1<<515)),
                                           node=vector((1<<512)|(1<<515)), invoice=vector(1<<512)))
        self.row = dict(channel_id='aa'*32, short_channel_id='90x1x0', funding_txid='bb'*32,
                        funding_outnum=0, peer_id='03'+'22'*32, channel_type={'bits':[12,22,514]},
                        state='CHANNELD_NORMAL', peer_connected=True, reestablished=True, htlcs=[])
        self.quote = dict(chain=self.chain, payment_hash=HASH, payment_secret='44'*32, amount_msat=2000000,
                          expires_at=2000, min_cltv_delta=40, max_cltv_delta=100,
                          approved_channels=[hg.channel_pin(self.row, self.chain['profile'])])
        self.hook = dict(htlc=dict(short_channel_id='90x1x0', id=7, amount_msat=2000000,
                                  cltv_expiry=161, cltv_expiry_relative=61, payment_hash=HASH),
                         onion=dict(type='tlv', payment_secret='44'*32, forward_msat=2000000,
                                    total_msat=2000000, outgoing_cltv_value=160))
        self.now = 1000
        hg.initialize_fixture(self.directory, self.chain)
        self.journal = hg.Journal(self.directory)
        self.addCleanup(lambda:self.journal.close())
        self.gate = self.open_gate()

    def rpc(self, method):
        return copy.deepcopy(self.info if method == 'getinfo' else {'channels':[self.row]})

    def open_gate(self):
        return hg.Gate(self.journal, self.chain, self.rpc, lambda:self.now)

    def restart(self):
        self.journal.close()
        self.journal = hg.Journal(self.directory)
        self.gate = self.open_gate()

    def arriving(self):
        self.row['htlcs'] = [dict(direction='in', id=7, amount_msat=2000000,
                                  expiry=161, payment_hash=HASH, state='RCVD_ADD_ACK_REVOCATION')]

    def held(self):
        self.gate.register(self.quote)
        self.arriving()
        self.assertIsNone(self.gate.accept(self.hook, 'hook-1'))
        status = self.gate.status(HASH)
        self.assertEqual(status['phase'], 'held')
        self.assertTrue(status['hook_ready'])
        return status['binding']

    def test_restart_replays_exact_binding_while_disconnected_and_after_admission_expiry(self):
        original = self.held()
        self.now = 4000
        self.info['blockheight'] += 10
        self.info['warning_lightningd_sync'] = 'Still loading blocks'
        self.row['peer_connected'] = self.row['reestablished'] = False
        self.restart()
        self.assertFalse(self.gate.status(HASH)['hook_ready'])
        replay = copy.deepcopy(self.hook)
        replay['htlc']['cltv_expiry_relative'] -= 10
        self.assertIsNone(self.gate.accept(replay, 'new-process-hook'))
        self.assertEqual(self.gate.status(HASH)['binding'], original)
        hook, result = self.gate.resolve(HASH, original, preimage=PREIMAGE)
        self.assertEqual(hook, ('new-process-hook', dict(result='resolve', payment_key=PREIMAGE)))
        self.assertEqual(result, {'recorded':'release_intent'})

    def test_release_and_lost_reply_retry_persist_before_revealing_preimage(self):
        original = self.held()
        hook, _ = self.gate.resolve(HASH, original, preimage=PREIMAGE)
        self.assertEqual(self.journal.read()['quotes'][HASH]['preimage'], hook[1]['payment_key'])
        self.restart()
        self.assertEqual(self.gate.resolve(HASH, original, preimage=PREIMAGE), (None, {'recorded':'release_intent'}))
        self.assertEqual(self.gate.accept(self.hook, 'replay'), dict(result='resolve', payment_key=PREIMAGE))
        with self.assertRaises(hg.Refused):
            self.gate.resolve(HASH, original)

    def test_fail_intent_survives_lost_reply_and_cannot_be_released(self):
        original = self.held()
        self.gate.resolve(HASH, original)
        self.restart()
        self.assertEqual(self.gate.resolve(HASH, original), (None, {'recorded':'fail_intent'}))
        self.assertEqual(self.gate.accept(self.hook, 'replay'), dict(result='fail', failure_message='2002'))
        with self.assertRaises(hg.Refused):
            self.gate.resolve(HASH, original, preimage=PREIMAGE)

    def test_fresh_process_cannot_resolve_until_hook_replayed(self):
        original = self.held()
        self.restart()
        with self.assertRaises(hg.Refused):
            self.gate.resolve(HASH, original, preimage=PREIMAGE)
        self.assertEqual(self.journal.read()['quotes'][HASH]['phase'], 'held')

    def test_changed_replay_quarantines_and_revokes_existing_ready_hook(self):
        original = self.held()
        for where, key, value in [('htlc','id',8), ('htlc','short_channel_id','91x1x0'),
                                  ('htlc','amount_msat',2000001), ('htlc','cltv_expiry',162),
                                  ('onion','payment_secret','55'*32), ('onion','total_msat',4000000),
                                  ('onion','outgoing_cltv_value',159)]:
            with self.subTest(key=key):
                self.restart()
                self.gate.accept(self.hook, 'original')
                bad = copy.deepcopy(self.hook)
                bad[where][key] = value
                self.assertIsNone(self.gate.accept(bad, 'changed'))
                self.assertTrue(self.gate.status(HASH)['needs_attention'])
                self.assertFalse(self.gate.status(HASH)['hook_ready'])
                self.assertIsNone(self.gate.accept(self.hook, 'original'))
                with self.assertRaises(hg.Refused):
                    self.gate.resolve(HASH, original, preimage=PREIMAGE)

    def test_terminal_replay_checks_snapshot_too(self):
        original = self.held()
        self.gate.resolve(HASH, original, preimage=PREIMAGE)
        self.restart()
        self.hook['onion']['payment_secret'] = '55'*32
        self.assertIsNone(self.gate.accept(self.hook, 'changed'))
        self.assertTrue(self.gate.status(HASH)['needs_attention'])

    def test_funding_and_signing_type_changes_never_rebind(self):
        original = self.held()
        for field, value in [('funding_txid','cc'*32), ('funding_outnum',1),
                              ('channel_id','dd'*32), ('peer_id','02'+'33'*32),
                              ('channel_type',{'bits':[12,22]})]:
            with self.subTest(field=field):
                saved = copy.deepcopy(self.row)
                self.row[field] = value
                self.restart()
                self.assertIsNone(self.gate.accept(self.hook, 'changed'))
                self.assertEqual(self.gate.status(HASH)['binding'], original)
                self.assertFalse(self.gate.status(HASH)['hook_ready'])
                self.row = saved

    def test_resolution_rechecks_actual_committed_htlc(self):
        original = self.held()
        self.row['htlcs'][0]['local_trimmed'] = True
        with self.assertRaises(hg.Refused):
            self.gate.resolve(HASH, original, preimage=PREIMAGE)
        self.assertEqual(self.journal.read()['quotes'][HASH]['phase'], 'held')

    def test_admission_refuses_partial_amount_secret_forwarding_time_and_unapproved_channel(self):
        self.gate.register(self.quote)
        self.arriving()
        cases = [('htlc','amount_msat',1000000), ('onion','forward_msat',1000000),
                 ('onion','total_msat',4000000), ('onion','payment_secret','55'*32),
                 ('onion','next_node_id','03'+'33'*32), ('onion','short_channel_id','91x1x0'),
                 ('onion','outgoing_cltv_value',139), ('htlc','cltv_expiry',201),
                 ('htlc','short_channel_id','91x1x0'), ('htlc','id',True)]
        for where, key, value in cases:
            with self.subTest(key=key):
                bad = copy.deepcopy(self.hook)
                bad[where][key] = value
                self.assertEqual(self.gate.accept(bad, 'bad')['result'], 'fail')
                self.assertEqual(self.gate.status(HASH)['phase'], 'quoted')
        self.now = self.quote['expires_at']
        self.assertEqual(self.gate.accept(self.hook, 'late')['result'], 'fail')

    def test_admission_requires_durable_untrimmed_and_reestablished_channel(self):
        self.gate.register(self.quote)
        self.arriving()
        for item in ({'local_trimmed':True}, {'state':'RCVD_ADD_COMMIT'}, {'payment_hash':'11'*32}, {'expiry':162}):
            saved = copy.deepcopy(self.row)
            self.row['htlcs'][0].update(item)
            self.assertEqual(self.gate.accept(self.hook, 'bad')['result'], 'fail')
            self.row = saved
        self.row['reestablished'] = False
        self.assertEqual(self.gate.accept(self.hook, 'bad')['result'], 'fail')

    def test_ordinary_payments_continue_without_reading_channels(self):
        self.assertEqual(self.gate.accept({'htlc':{'payment_hash':'99'*32}}, 'ordinary'), {'result':'continue'})
        self.assertEqual(self.journal.read()['quotes'], {})

    def test_registration_is_immutable_exact_retry_and_single_active_quote(self):
        self.gate.register(self.quote)
        self.gate.register(self.quote)
        for changes in ({'amount_msat':2000001}, {'expires_at':2100}, {'payment_secret':'55'*32},
                        {'payment_hash':'99'*32}):
            with self.assertRaises(hg.Refused):
                self.gate.register(dict(self.quote, **changes))
        self.assertEqual(len(self.journal.read()['quotes']), 1)

    def test_wrong_preimage_or_binding_never_writes_release(self):
        original = self.held()
        for b, preimage in ((dict(original,htlc_id=8), PREIMAGE), (original,'99'*32), (original,'')):
            with self.assertRaises(hg.Refused):
                self.gate.resolve(HASH, b, preimage=preimage)
        self.assertEqual(self.journal.read()['quotes'][HASH]['phase'], 'held')

    def test_chain_node_and_legacy_journals_are_refused(self):
        original = self.journal.read()
        cases = [{}, {'schema':1,'quotes':{}}, dict(original, schema=True),
                 dict(original, chain=ci.binding(ci.BTC,'regtest',self.chain['node_id'])),
                 dict(original, chain=ci.binding(ci.LEGACY_XBT,'regtest',self.chain['node_id'])),
                 dict(original, chain=ci.binding(ci.PRIVKEYIO_XBT,'regtest','03'+'55'*32))]
        for case in cases:
            self.journal.write(case)
            with self.assertRaises((ValueError, KeyError)):
                self.open_gate()
        self.journal.write(original)
        self.info['id'] = '03'+'55'*32
        with self.assertRaises(ValueError):
            self.open_gate()

    def test_mainnet_and_wrong_protocol_features_refused(self):
        with self.assertRaises(hg.Refused):
            hg.binding(ci.binding(ci.PRIVKEYIO_XBT,'mainnet',self.chain['node_id']))
        self.info['our_features']['init'] = ''
        with self.assertRaises(ValueError):
            self.open_gate()

    def test_inconsistent_journal_is_not_repaired_or_upgraded(self):
        self.held()
        original = self.journal.read()
        for field in ('binding', 'accepted'):
            invalid = copy.deepcopy(original)
            del invalid['quotes'][HASH][field]
            self.journal.write(invalid)
            with self.assertRaises((ValueError,KeyError)):
                self.open_gate()
            self.assertEqual(self.journal.read(), invalid)

    def test_lost_write_before_hold_blocks_process_and_never_marks_hook_ready(self):
        self.gate.register(self.quote)
        self.arriving()
        with patch.object(self.journal, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(hg.StorageError):
                self.gate.accept(self.hook, 'held')
        self.assertTrue(self.gate.blocked)
        self.assertFalse(self.gate.status(HASH)['hook_ready'])
        self.assertEqual(self.journal.read()['quotes'][HASH]['phase'], 'quoted')
        self.assertIsNone(self.gate.accept({'htlc':{'payment_hash':'99'*32}}, 'ordinary'))

    def test_post_replace_fsync_failure_does_not_reveal_but_recovers_durable_intent(self):
        original = self.held()
        real_fsync = os.fsync
        count = 0
        def interrupted(fd):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('directory fsync reply unavailable')
            return real_fsync(fd)
        with patch.object(hg.os, 'fsync', side_effect=interrupted):
            with self.assertRaises(hg.StorageError):
                self.gate.resolve(HASH, original, preimage=PREIMAGE)
        self.assertTrue(self.gate.blocked)
        self.assertFalse(self.gate.status(HASH)['hook_ready'])
        self.restart()
        self.assertEqual(self.gate.accept(self.hook, 'replayed'), dict(result='resolve',payment_key=PREIMAGE))

    def test_private_atomic_journal_single_writer_and_no_temp_leaks(self):
        self.held()
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.directory.glob('.state-*')), [])
        with self.assertRaises(BlockingIOError):
            hg.Journal(self.directory)

    def test_missing_symlinked_public_and_duplicate_key_journals_refused(self):
        self.journal.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.open_gate()
        target = Path(self.tmp.name)/'external'
        target.write_text('{}')
        self.journal.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.open_gate()
        self.journal.path.unlink()
        self.journal.path.write_text('{}')
        self.journal.path.chmod(0o644)
        with self.assertRaises(hg.Refused):
            self.open_gate()
        self.journal.path.chmod(0o600)
        self.journal.path.write_text('{"schema":2,"schema":1}')
        with self.assertRaises(hg.Refused):
            self.open_gate()

    def test_status_omits_secrets_and_preimage(self):
        original = self.held()
        self.gate.resolve(HASH, original, preimage=PREIMAGE)
        status = json.dumps(self.gate.status(HASH))
        self.assertNotIn(self.quote['payment_secret'], status)
        self.assertNotIn(PREIMAGE, status)
        self.assertNotIn('accepted', status)
        self.assertIn('release_intent', status)

    def test_dispatch_emits_durable_hook_reply_before_operator_ack(self):
        original = self.held()
        replies = plugin.dispatch(self.gate, dict(id='release',method='swap-gate-release',
                                   params=dict(payment_hash=HASH,binding=original,preimage=PREIMAGE)))
        self.assertEqual([r['id'] for r in replies], ['hook-1','release'])
        self.assertEqual(self.journal.read()['quotes'][HASH]['phase'], 'release_intent')
        retry = plugin.dispatch(self.gate, dict(id='retry',method='swap-gate-release',
                                   params=[HASH,original,PREIMAGE]))
        self.assertEqual([r['id'] for r in retry], ['retry'])

    def test_plugin_manifest_and_network_refusal_run_as_real_subprocess(self):
        payload = [dict(jsonrpc='2.0',id=1,method='getmanifest',params={}),
                   dict(jsonrpc='2.0',id=2,method='init',params=dict(
                       configuration=dict(network='bitcoin',startup=True),options={}))]
        result = subprocess.run([sys.executable,str(Path(plugin.__file__))],
                                 input='\n\n'.join(map(json.dumps,payload))+'\n\n',
                                 text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode, 1)
        manifest = json.loads(result.stdout)['result']
        self.assertFalse(manifest['dynamic'])
        self.assertEqual(manifest['hooks'], [{'name':'htlc_accepted'}])
        self.assertIn('startup or framing refused',result.stderr)

    def test_plugin_frames_and_exact_arguments(self):
        messages = list(plugin.requests(io.StringIO('{\n"id":1,\n"method":"hello"\n}\n\n')))
        self.assertEqual(messages, [dict(id=1,method='hello')])
        with self.assertRaises(hg.Refused):
            plugin.arguments('swap-gate-fail', {'payment_hash':HASH,'binding':{},'extra':True})


if __name__ == '__main__':
    unittest.main()
