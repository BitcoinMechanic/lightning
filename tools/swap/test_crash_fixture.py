#!/usr/bin/env python3
"""Guards for the process-kill fixture; no funded daemon needed."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_plugin_crash as crash


class CrashFixtureTests(unittest.TestCase):
    def test_read_only_snapshot_rejects_missing_resolved_or_multiple_HTLCs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'regtest').mkdir()
            receiver = dict(root=root)
            path = root/'regtest/lightningd.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE channel_htlcs (id INTEGER, hstate INTEGER, payment_key BLOB, msatoshi INTEGER)')
                db.execute('INSERT INTO channel_htlcs VALUES (1,14,NULL,2000000)')
            before = path.read_bytes()
            self.assertEqual(crash.pending_snapshot(receiver)['id'],1)
            self.assertEqual(path.read_bytes(),before)
            with self.assertRaises(sqlite3.OperationalError):
                crash.database(receiver,'DELETE FROM channel_htlcs')
            with sqlite3.connect(path) as db:
                db.execute('UPDATE channel_htlcs SET payment_key=?',(b'p'*32,))
            with self.assertRaisesRegex(RuntimeError,'one_committed'):
                crash.pending_snapshot(receiver)
            with sqlite3.connect(path) as db:
                db.execute('UPDATE channel_htlcs SET payment_key=NULL')
                db.execute('INSERT INTO channel_htlcs VALUES (2,14,NULL,2000000)')
            with self.assertRaisesRegex(RuntimeError,'one_committed'):
                crash.pending_snapshot(receiver)
            with sqlite3.connect(path) as db:
                db.execute('DELETE FROM channel_htlcs')
            with self.assertRaisesRegex(RuntimeError,'one_committed'):
                crash.pending_snapshot(receiver)

    def test_stale_pid_marker_cannot_kill_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            receiver = dict(root=Path(tmp),proc=Mock(pid=os.getpid()))
            receiver['proc'].poll.return_value = None
            directory = crash.gate.private_dir(receiver)
            directory.mkdir(parents=True)
            marker = directory/'fixture-process.json'
            for value in (dict(pid=True,parent=os.getpid()),dict(pid=os.getpid(),parent=-1),
                          dict(pid=os.getpid(),parent=os.getpid())):
                marker.write_text(json.dumps(value))
                with patch.object(crash.os,'kill') as kill:
                    with self.assertRaises((RuntimeError,OSError)):
                        crash.kill_gate(receiver)
                    kill.assert_not_called()

    def test_changed_core_row_refuses_before_starting_replacement_nodes(self):
        lab = Mock()
        payer = dict(name='payer',identity={},profile=crash.ci.BTC,cli=[])
        receiver = dict(name='receiver',identity={},profile=crash.ci.BTC,proc=Mock(returncode=1))
        snapshot = dict(payment_hash=b'p'*32,hstate=14)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            receiver['root'] = directory
            (directory/'console.log').write_text('')
            (directory/'state.json').write_bytes(b'persisted journal')
            with patch.object(crash.gate,'channel',return_value={}), \
                 patch.object(crash.hg,'channel_pin',return_value={}), \
                 patch.object(crash,'pending_snapshot',side_effect=[snapshot,dict(snapshot,hstate=15)]), \
                 patch.object(crash,'core_unpaid'),patch.object(crash,'kill_gate'), \
                 patch.object(crash,'isolate_invoice_hook'),patch.object(crash,'record_crash'), \
                 patch.object(crash,'check_crash_log'), \
                 patch.object(crash.gate,'private_dir',return_value=directory), \
                 patch.object(crash.gate,'attempt',return_value={}), \
                 patch.object(crash.gate,'wait_for_gate_writer_exit'):
                with self.assertRaisesRegex(RuntimeError,'core_advanced_held_HTLC'):
                    crash.crash_restart(lab,payer,receiver)
            lab.lightning.assert_not_called()
            lab.stop.assert_not_called()

    def test_control_replaces_important_option_without_duplicates(self):
        lab = crash.CrashLab.__new__(crash.CrashLab)
        lab.gates,lab.commands,lab.important = {'receiver':crash.ci.BTC},{},False
        original = ['lightningd','--important-plugin='+str(crash.gate.PLUGIN),
                    '--swap-gate-profile='+crash.ci.BTC]
        with patch.object(crash.Lab,'start',return_value=None) as start:
            lab.start(original,Path('/results/receiver/console.log'))
        command = start.call_args.args[0]
        self.assertEqual(command.count('--plugin='+str(crash.gate.PLUGIN)),1)
        self.assertNotIn('--important-plugin='+str(crash.gate.PLUGIN),command)


    def test_invoice_hook_isolation_requires_exact_active_plugins_and_confirmed_stop(self):
        rows = [dict(name='/plugins/'+name,active=True,dynamic=name=='offers')
                for name in ('bcli','topology','offers','txprepare','spenderp',crash.gate.PLUGIN.name)]
        stopped = [p for p in rows if Path(p['name']).name != 'offers']
        node = dict(cli=['cli'])
        lab = Mock()
        lab.rpc.side_effect = [dict(plugins=rows),{},dict(plugins=stopped)]
        crash.isolate_invoice_hook(lab,node)
        self.assertEqual(lab.rpc.call_args_list[1].args,(['cli'],'plugin','stop','/plugins/offers'))
        for before,after in ((rows,rows),(rows+[dict(name='/plugins/extra',active=True)],stopped),
                             ([dict(p,active=False) if p['name']=='/plugins/offers' else p for p in rows],stopped)):
            lab = Mock()
            lab.rpc.side_effect = [dict(plugins=before),{},dict(plugins=after)]
            with self.assertRaises(RuntimeError):
                crash.isolate_invoice_hook(lab,node)

    def test_uploaded_fallthrough_trace_fails_patched_log_check(self):
        # The supplied 0070 trace reached this point without persisting a
        # fulfillment. Unchanged DB rows alone cannot establish suppression.
        died = 'Plugin died from htlc_accepted hook call\n'
        assembled = 'HTLC set contains 1 HTLCs, for a total of 2000000msat out of 2000000msat (payment_secret)\n'
        shutdown = 'Plugin marked as important, shutting down lightningd!\n'
        trace = died+assembled+shutdown
        crash.check_crash_log(trace,fallthrough=True,important=True)
        with self.assertRaisesRegex(RuntimeError,'core_continued'):
            crash.check_crash_log(trace,fallthrough=False,important=True)
        crash.check_crash_log(shutdown,fallthrough=False,important=True)
        crash.check_crash_log(died+assembled,fallthrough=True,important=False)
        for invalid in ('',shutdown,assembled+died+shutdown,shutdown+died+assembled,trace+trace):
            with self.assertRaises(RuntimeError):
                crash.check_crash_log(invalid,fallthrough=True,important=True)


if __name__ == '__main__':
    unittest.main()
