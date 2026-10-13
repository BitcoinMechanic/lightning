#!/usr/bin/env python3
"""0092a: local administration feeds the six funded automatic HTTPS swaps.

No CLI call changes mainnet state. Restore checks fence stopped disposable
fixture journals; they do not claim funded wallet restore or on-chain recovery.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import bounded_authority as ba
import coordinator_admin as admin
import customer_invoice as invoice
import customer_remote as remote
import held_gate as hg
import restore_barrier as barrier
import service
import test_service_engine as base
from test_engine import require

ADMIN = Path(__file__).with_name('coordinator_admin_cli.py')


def call(node, **request):
    result = subprocess.run([sys.executable,str(ADMIN),'--disposable-regtest',
        '--network-directory',str(base.customer.state_dir(node)),'--profile',node['identity']['profile']],
        input=json.dumps(request),capture_output=True,text=True,timeout=120)
    require(result.returncode == 0 and not result.stderr,'admin_fixture_action_failed_'+request['action'])
    return json.loads(result.stdout)


def grant_setup(lab,node,kind,limits):
    root=base.customer.state_dir(node)
    if not (root/admin.DIRECTORY).exists(): call(node,action='initialize',confirmed=True)
    before=json.loads((root/'bounded-authority-v1/state.json').read_text())
    review=call(node,action='review',kind=kind,limits=limits,lifetime_seconds=86400)
    require(json.loads((root/'bounded-authority-v1/state.json').read_text()) == before,'admin_review_installed_authority')
    require(review['channel_count'] == 1 and review['limits'] == limits and review['phase'] == 'review',
            'admin_review_limits_changed')
    gid=review['confirmation']
    # Discard the first successful response, then recover using a fresh process.
    call(node,action='confirm',confirmation=gid,confirmed=True)
    snapshot=(root/'bounded-authority-v1/state.json').read_bytes()
    recovered=call(node,action='confirm',confirmation=gid,confirmed=True)
    require(recovered['confirmation'] == gid and recovered['current'] is True and recovered['phase'] == 'saved'
            and (root/'bounded-authority-v1/state.json').read_bytes() == snapshot,'admin_reply_recovery_changed_authority')
    after=json.loads(snapshot)
    require(len(after['grants']) == len(before['grants'])+1,'admin_duplicate_grant')
    lab.admin_receipts.append(dict(profile=node['identity']['profile'],kind=kind,channel_count=1,
        review_without_authority=True,fresh_process_confirmation=True,lost_reply_same_grant=True))
    return gid


def refuse(fn, *, reason=None):
    try: fn()
    except hg.Refused as error:
        require(reason is None or str(error) == reason, 'restored_fixture_wrong_refusal')
        return
    raise RuntimeError('restored_fixture_execution_not_refused')


def after_stop(lab,topologies,*,history_count=3):
    def no_rpc(*args,**kwargs): raise RuntimeError('restored_fixture_RPC_called')
    populated_publishers = credential_journals = 0
    for label,(_,nodes,_) in topologies.items():
        for node in (nodes[0],nodes[-1]):
            root=base.customer.state_dir(node)
            paths=list(root.glob('*/state.json'))
            before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
            journals=[]
            try:
                def opened(name):
                    j=hg.Journal(root/name);journals.append(j);return j
                a=ba.Authority(opened('bounded-authority-v1'),node['identity'],no_rpc)
                m=admin.Admin(a,opened(admin.DIRECTORY))
                publisher=invoice.Publisher(a,opened(base.customer.plugin.INVOICE_DIR))
                credential=remote.Remote(a,opened(base.customer.plugin.REMOTE_DIR),no_rpc,publisher)
                # Validate populated records before fencing. Publisher validation
                # calls Authority.entry(), which must refuse once restored.
                # Loading an empty publisher did not exercise this dependency.
                published=len(publisher.record['invoices'])
                populated_publishers += int(published > 0)
                credential_journals += int(bool(credential.record['credentials']))
                if history_count:
                    require(published == (history_count if node is nodes[-1] else 0),
                            'restore_fixture_publication_history_missing')
                    require(bool(credential.record['credentials']), 'restore_fixture_credentials_missing')
                barrier.fence(root)
                for obj in (a,m,credential,publisher): refuse(obj.guard)
                if node is nodes[-1]:
                    refuse(lambda:hg.Gate(opened('swap-gate-v1'),node['identity'],no_rpc))
                require(m.status()['blocked'] is True,'restored_admin_status_not_blocked')
                require({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before,
                        'restore_fence_modified_original_journals')
            finally:
                for j in reversed(journals):j.close()
            # Fresh facade startup is another valid refusal point. In particular,
            # a populated publisher refuses during construction, before RPC.
            configuration=dict(network='regtest',startup=True,**{'lightning-dir':str(root)})
            refuse(lambda:base.customer.plugin.Service(configuration,node['identity']['profile'],
                rpc_factory=lambda _:no_rpc), reason='restored_authority_blocked')
            require({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before,
                    'restored_startup_modified_original_journals')
    barrier.fence(lab.root)
    for direction in ('forward','reverse'):
        j=hg.Journal(lab.root/('service-worker-'+direction))
        try:
            s=service.Service(j,factory=no_rpc)
            require(s.status()['restored'] is True,'restored_service_status_not_blocked')
            refuse(lambda:s.tick('92'*32))
            require(len(s.status()['swaps'])==history_count,'restore_fence_lost_readonly_history')
        finally:j.close()
    lab.restore_receipt=dict(coordinator_count=4,controller_count=2,stopped_fixture_journals=True,
        execution_refused_before_RPC=True,history_preserved=True,funded_wallet_restore=False,
        loaded_objects_refused=True,fresh_facade_startup_refused=True,
        populated_publishers=populated_publishers,credential_journals=credential_journals)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--disposable-regtest',required=True,action='store_true')
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()));args=p.parse_args();os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):setattr(args,name,getattr(args,name).resolve())
    lab=base.previous.HttpsLab(args);lab.admin_receipts=[]
    try:
        base.run(lab,grant_setup=grant_setup,after_stop=after_stop)
        require(len(lab.admin_receipts)==8,'admin_setup_count_changed')
        result=dict(schema=1,fixture='privkeyio-coordinator-admin-0092a',regtest_only=True,installer=False,
            grants=lab.admin_receipts,restore=lab.restore_receipt,
            service=json.loads((lab.root/'service-result.json').read_text()))
        (lab.root/'coordinator-result.json').write_text(json.dumps(result,indent=2)+'\n')
        print('COORDINATOR ADMIN FIXTURE PASSED: 0092a; eight grant setups, six funded swaps, populated restore checks; no installer',flush=True)
    finally:lab.close()


if __name__=='__main__':main()
