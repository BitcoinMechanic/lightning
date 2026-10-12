#!/usr/bin/env python3
"""Explicit disposable-regtest process driver for saved HTTPS pairing.

Only pair receives credentials on private stdin. All later processes load the
original private pairing journal. No CLI command, RPC method or proxy input.
Fault injection exists only in this fixture, after an HTTPS operation returns.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys

import held_gate as hg
import worker_pairing as pairing


def execute(request):
    directory = Path(request['directory'])
    hg.need(directory.is_absolute() and directory.name.startswith('cross-worker-'), 'fixture_worker_path_refused')
    pair_dir = directory/'https-pairing'
    pj = hg.Journal(pair_dir); wj = None
    try:
        p = pairing.Pairing(pj)
        if request['action'] == 'pair':return p.save(request['binding'],confirmed=request['confirmed'])
        hg.need('binding' not in request and 'clients' not in request, 'fixture_configuration_on_recovery_refused')
        wj = hg.Journal(directory); worker = p.worker(wj)
        original = worker.call; fault = request.get('fault')
        def call(kind,gid,op,body):
            value = original(kind,gid,op,body)
            if fault == dict(kind=kind,operation=op):
                (directory/'fault-consumed').write_text(kind+' '+op+'\n')
                os.kill(os.getpid(),signal.SIGKILL)
            return value
        worker.call = call; action = request['action']
        if action == 'prepare':return dict(contract_id=worker.prepare(request['contract']))
        cid = request['contract_id']
        if action == 'confirm':return worker.confirm(cid,request['confirmation'])
        if action == 'step':return worker.step(cid)
        if action == 'invoice':return worker.payment_invoice(cid)
        if action == 'status':return worker.status(cid)
        raise ValueError('fixture_worker_action_refused')
    finally:
        if wj is not None:wj.close()
        pj.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True);parser.parse_args()
    os.umask(0o077)
    try:
        raw=sys.stdin.read(hg.MAX_BYTES+1);hg.need(len(raw)<=hg.MAX_BYTES,'fixture_request_too_large')
        request=json.loads(raw,object_pairs_hook=hg.unique_object)
        print(json.dumps(dict(result=execute(request))))
    except Exception as error:
        reason=str(error) if isinstance(error,ValueError) and re.fullmatch('[a-z_]{1,80}',str(error)) else None
        print(json.dumps(dict(error_type=type(error).__name__,reason=reason,rpc_code=None)))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
