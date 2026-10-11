#!/usr/bin/env python3
"""Targeted harness checks for errors that would otherwise cost a VM rerun."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, MagicMock, patch

import test_gate_engine as fixture


class FixtureTests(unittest.TestCase):
    def test_restarted_commands_do_not_duplicate_plugin_options(self):
        lab = fixture.GateLab.__new__(fixture.GateLab)
        lab.gates,lab.commands = {'btc-receiver':fixture.ci.BTC},{}
        original = ['lightningd','--network=regtest','--important-plugin='+str(fixture.PLUGIN),
                    '--swap-gate-profile='+fixture.ci.BTC]
        with patch.object(fixture.Lab,'start',return_value='started') as start:
            lab.start(original,Path('/results/btc-receiver/refusal.log'))
        command = start.call_args.args[0]
        self.assertEqual(command.count('--important-plugin='+str(fixture.PLUGIN)),1)
        self.assertEqual(sum(str(a).startswith('--swap-gate-profile=') for a in command),1)

    def test_attempt_requires_one_full_part_and_stable_persisted_index(self):
        good = dict(payment_hash='33'*32,groupid=1,status='pending',amount_msat=fixture.AMOUNT,
                    amount_sent_msat=fixture.AMOUNT,created_index=12,created_at=1700000000,
                    destination='02'+'11'*32)
        lab = Mock()
        lab.rpc_named.return_value = {'payments':[good]}
        self.assertEqual(fixture.attempt(lab,{'cli':[]},'33'*32),good)
        for rows in ([],[good,good],[dict(good,partid=1)],[dict(good,groupid=2)],
                     [dict(good,amount_msat=1000000)],[dict(good,created_index=None)]):
            lab.rpc_named.return_value = {'payments':rows}
            with self.assertRaises(RuntimeError):
                fixture.attempt(lab,{'cli':[]},'33'*32)
        after = dict(good,status='complete',updated_index=20,payment_preimage='22'*32)
        self.assertEqual(fixture.attempt_identity(good),fixture.attempt_identity(after))
        self.assertNotEqual(fixture.attempt_identity(good),fixture.attempt_identity(dict(after,created_index=13)))

    def test_send_is_one_core_call_and_uncertain_reply_is_never_retried(self):
        lab = Mock()
        node_id = '02'+'11'*32
        receiver = {'identity':{'node_id':node_id}}
        payer = {'cli':[],'profile':fixture.ci.BTC}
        lab.rpc.return_value = dict(valid=True,type='bolt11 invoice',currency='bcrt',features='',
                                     payee=node_id,amount_msat=fixture.AMOUNT,payment_hash='33'*32,
                                     payment_secret='44'*32,min_final_cltv_expiry=120)
        lab.rpc_named.side_effect = RuntimeError('uncertain')
        with patch.object(fixture,'channel',return_value=dict(peer_id=node_id,short_channel_id='110x1x0')):
            with self.assertRaisesRegex(RuntimeError,'uncertain'):
                fixture.send_once(lab,payer,receiver,'lnbcrt-fixture','33'*32)
        self.assertEqual(lab.rpc_named.call_count,1)
        call = lab.rpc_named.call_args
        self.assertEqual(call.args[1],'sendpay')
        self.assertEqual(call.args[2]['route'],[dict(id=node_id,channel='110x1x0',
                                                   amount_msat=fixture.AMOUNT,delay=120)])
        self.assertEqual((call.args[2]['partid'],call.args[2]['groupid']),(0,1))

    def test_signed_invoice_fields_are_checked_before_registration(self):
        lab = Mock()
        receiver = {'cli':[],'profile':fixture.ci.BTC,
                    'identity':fixture.ci.binding(fixture.ci.BTC,'regtest','02'+'11'*32)}
        lab.rpc.side_effect = [{'bolt11':'lnbcrt-fixture'},dict(valid=True,type='bolt11 invoice',
             currency='bcrt',features='',payee=receiver['identity']['node_id'],amount_msat=fixture.AMOUNT,
             payment_hash='33'*32,payment_secret='44'*32,min_final_cltv_expiry=120,expiry=1800,created_at=10)]
        with self.assertRaisesRegex(RuntimeError,'signed_gate_invoice_mismatch'):
            fixture.make_quote(lab,receiver)
        lab.rpc_named.assert_not_called()

    def test_fault_injection_pauses_only_after_persisted_matching_terminal_state(self):
        import fixture_gate_plugin as wrapper
        journal = MagicMock()
        arm = journal.directory.__truediv__.return_value
        arm.is_file.return_value = True
        arm.read_text.return_value = json.dumps(dict(payment_hash='33'*32,phase='release_intent'))
        record = {'quotes':{'33'*32:{'phase':'release_intent'}}}
        calls = []
        with patch.object(wrapper,'original_write',side_effect=lambda *args:calls.append('durable')):
            with patch.object(wrapper,'pause_until_parent_closes_input',side_effect=SystemExit(0)):
                with self.assertRaises(SystemExit):
                    wrapper.write_and_pause(journal,record)
        self.assertEqual(calls,['durable'])
        arm.unlink.assert_called_once()
        arm.write_text.assert_called_once()

    def test_paused_real_process_holds_lock_until_pipe_eof_then_exits_without_reply(self):
        # Uses ordinary pipes, a real subprocess and a real flock. No daemon or
        # Unix socket is needed to reproduce the 0068 terminal-reply failure.
        source = Path(fixture.__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/'journal'
            chain = fixture.ci.binding(fixture.ci.BTC,'regtest','02'+'11'*32)
            fixture.hg.initialize_fixture(directory,chain)
            record = json.loads((directory/'state.json').read_text())
            record['quotes'] = {'33'*32:{'phase':'release_intent'}}
            (directory/'fixture-arm.json').write_text(json.dumps(dict(payment_hash='33'*32,phase='release_intent')))
            script = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import held_gate,fixture_gate_plugin
journal=held_gate.Journal(Path(sys.argv[2]))
try:
    fixture_gate_plugin.write_and_pause(journal,json.loads(sys.argv[3]))
    print('UNEXPECTED TERMINAL REPLY',flush=True)
finally:
    journal.close()
'''
            proc = subprocess.Popen([sys.executable,'-c',script,str(source),str(directory),json.dumps(record)],
                                     stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                end = time.monotonic()+5
                while not (directory/'fixture-paused.json').is_file():
                    self.assertIsNone(proc.poll(), 'paused child exited early')
                    self.assertLess(time.monotonic(),end,'child did not reach durable pause')
                    time.sleep(.01)
                with self.assertRaises(BlockingIOError):
                    fixture.hg.Journal(directory)
                self.assertEqual(json.loads((directory/'state.json').read_text()),record)
                # A queued RPC must not resume the pause or emit a response.
                proc.stdin.write(b'{"id":9,"method":"swap-gate-status"}\n\n')
                proc.stdin.flush()
                proc.stdin.close()
                proc.stdin = None
                out,err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode,0,err)
                self.assertEqual(out,b'')
                self.assertEqual(err,b'')
                replacement = fixture.hg.Journal(directory)
                try:
                    self.assertEqual(replacement.read(),record)
                finally:
                    replacement.close()
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)

    def test_writer_exit_wait_does_not_delete_or_ignore_an_existing_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver = dict(root=Path(tmp))
            (Path(tmp)/'regtest').mkdir()
            directory = fixture.private_dir(receiver)
            fixture.hg.initialize_fixture(directory,fixture.ci.binding(fixture.ci.BTC,'regtest','02'+'11'*32))
            first = fixture.hg.Journal(directory)
            inode = (directory/'writer.lock').stat().st_ino
            lab = Mock()
            def wait(fn, timeout):
                self.assertEqual(timeout,15)
                with self.assertRaises(BlockingIOError):
                    fn()
                self.assertEqual((directory/'writer.lock').stat().st_ino,inode)
                first.close()
                self.assertTrue(fn())
            lab.wait.side_effect = wait
            try:
                fixture.wait_for_gate_writer_exit(lab,receiver)
            finally:
                first.close()
            self.assertEqual((directory/'writer.lock').stat().st_ino,inode)


if __name__ == '__main__':
    unittest.main()
