#!/usr/bin/env python3
"""Disposable fixture process driver. Never install as a service or RPC plugin.

Private stdin supplies the fixture's local CLI transports and scoped runes.
Faults kill only this process after a selected authenticated operation returns.
Normal cross_worker contains neither transport configuration nor fault hooks.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

import cross_worker as cw
import customer_remote as remote
import held_gate as hg


class RPCError(Exception):
    def __init__(self, code): self.code = code


def execute(request):
    directory = Path(request['directory'])
    hg.need(directory.is_absolute() and directory.name.startswith('cross-worker-'), 'fixture_worker_path_refused')
    clients = request['clients']; hg.need(set(clients) == set(cw.KINDS), 'fixture_clients_invalid')
    fault = request.get('fault')
    def call(kind, gid, op, body):
        saved = clients[kind]
        hg.need(gid == saved['grant_id'], 'fixture_grant_changed')
        params = dict(peer_id=saved['peer_id'],method=remote.CALL,
            params=dict(grant_id=gid,operation=op,body=body),rune=saved['rune'])
        command = [*saved['cli'],'-k','commando',*(k+'='+json.dumps(v,separators=(',',':')) for k,v in params.items())]
        result = subprocess.run(command,capture_output=True,text=True,timeout=90)
        if result.returncode:
            try: code = json.loads(result.stdout).get('code')
            except (ValueError,AttributeError): code = None
            raise RPCError(code if type(code) is int else None)
        value = json.loads(result.stdout)
        if fault == dict(kind=kind,operation=op):
            (directory/'fault-consumed').write_text(kind+' '+op+'\n')
            os.kill(os.getpid(),signal.SIGKILL)
        return value
    journal = hg.Journal(directory)
    try:
        worker = cw.Worker(journal,call); action = request['action']
        if action == 'prepare': return dict(contract_id=worker.prepare(request['contract']))
        cid = request['contract_id']
        if action == 'confirm': return worker.confirm(cid,request['confirmation'])
        if action == 'step': return worker.step(cid)
        if action == 'invoice': return worker.payment_invoice(cid)
        if action == 'status': return worker.status(cid)
        raise ValueError('fixture_worker_action_refused')
    finally: journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True); parser.parse_args()
    os.umask(0o077)
    try:
        raw = sys.stdin.read(hg.MAX_BYTES+1); hg.need(len(raw) <= hg.MAX_BYTES,'fixture_request_too_large')
        request = json.loads(raw,object_pairs_hook=hg.unique_object)
        print(json.dumps(dict(result=execute(request))))
    except Exception as error:
        reason = str(error) if isinstance(error,ValueError) and re.fullmatch('[a-z_]{1,80}',str(error)) else None
        print(json.dumps(dict(error_type=type(error).__name__,reason=reason,
                             rpc_code=error.code if isinstance(error,RPCError) else None)))
        return 1
    return 0


if __name__ == '__main__': raise SystemExit(main())
