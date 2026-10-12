#!/usr/bin/env python3
"""0082: native CLNrest HTTPS, saved pairing and bidirectional worker recovery."""
import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import tempfile
import urllib.parse

import bounded_authority as ba
import chain_identity as ci
import customer_https as https
import customer_remote as remote
import held_gate as hg
import test_cross_engine as cross
import test_customer_engine as customer
import worker_pairing as pairing
from test_engine import require


def raw(config, method, params, *, rune=None):
    p=urllib.parse.urlsplit(config['url'])
    conn=http.client.HTTPSConnection(p.hostname,p.port,timeout=15,context=https.context(config['ca_pem']))
    try:
        conn.request('POST','/v1/'+method,body=json.dumps(params).encode(),headers={
            'Content-Type':'application/json','Accept':'application/json','Rune':config['rune'] if rune is None else rune})
        r=conn.getresponse();data=r.read(https.LIMIT+1)
        require(len(data)<=https.LIMIT,'https_fixture_reply_too_large')
        return r.status,json.loads(data)
    finally:conn.close()


class HttpsLab(customer.CustomerLab):
    def __init__(self,args):
        super().__init__(args);self.rest={}

    def start(self,command,log):
        if Path(command[0]).name=='lightningd' and log.parent.name.endswith(('-source','-recipient')):
            name=log.parent.name
            if name not in self.rest:self.rest[name]=self.port()
            plugin=Path(command[0]).parent.parent/'libexec/c-lightning/plugins/clnrest'
            # --disable-rust excludes CLNrest from the compiled built-in list.
            # Merely copying the executable into that directory does not load it.
            command=[arg for arg in command if str(arg) not in ('--disable-plugin=clnrest',
                '--plugin='+str(plugin),'--important-plugin='+str(plugin)) and not str(arg).startswith(
                ('--clnrest-host=','--clnrest-protocol=','--clnrest-port=','--clnrest-certs='))]
            command += ['--important-plugin='+str(plugin),'--clnrest-host=127.0.0.1','--clnrest-protocol=https',
                '--clnrest-port='+str(self.rest[name]),'--clnrest-certs='+str(log.parent/'rest-certs')]
        return super().start(command,log)

    def lightning(self,*args,**kwargs):
        node=super().lightning(*args,**kwargs)
        if node['root'].name in self.rest:
            self.wait(lambda:any(Path(p['name']).name=='clnrest' and p.get('active') is True
                for p in self.rpc(node['cli'],'plugin','list')['plugins']),node['proc'])
            def ready():
                config=self.config(node,'invalid_'+'a'*32,{})
                return raw(config,'getinfo',{})[0] in (401,403)
            self.wait(ready,node['proc'])
        return node

    def config(self,node,rune,grant):
        return dict(url='https://127.0.0.1:'+str(self.rest[node['root'].name]),
            ca_pem=(node['root']/'rest-certs/ca.pem').read_text(),rune=rune,grant=grant)


class Driver(cross.Driver):
    script=Path(__file__).with_name('fixture_https_worker.py')

    def __init__(self,lab,directory,clients):
        super().__init__(lab,directory,clients)
        self.pair_dir=directory/'https-pairing'
        if not self.pair_dir.exists():
            journal=hg.Journal(directory)
            try:require(not cross.cw.validate(journal.read())['swaps'],'https_pairing_missing_during_recovery')
            finally:journal.close()
            pairing.initialize_fixture(self.pair_dir)
            grants={k:c.call('info')['grant'] for k,c in clients.items()}
            direction='forward' if grants['incoming']['chain']['profile']==ci.BTC else 'reverse'
            config={k:lab.config(c.node,c.credential['rune'],grants[k]) for k,c in clients.items()}
            self.run('pair',binding=dict(direction=direction,nodes=config),confirmed=True)
        present=(self.pair_dir/'state.json').read_bytes()
        if not hasattr(lab,'pairing_bytes'):lab.pairing_bytes={}
        self.saved_pairing=lab.pairing_bytes.setdefault(str(directory),present)
        require(present==self.saved_pairing,'https_pairing_changed_across_restart')

    def request(self,action,**kwargs):
        return dict(directory=str(self.directory),action=action,contract_id=self.cid,**kwargs)

    def run(self,action,**kwargs):
        result=super().run(action,**kwargs)
        if hasattr(self,'saved_pairing'):
            require((self.pair_dir/'state.json').read_bytes()==self.saved_pairing,'https_saved_pairing_changed')
        return result


def scope_response(lab,profile,kind,case,result,expected_status,expected_code):
    status,reply=result
    code=reply.get('code') if type(reply) is dict else None
    error_object=type(reply) is dict and type(reply.get('message')) is str and 'result' not in reply
    report=dict(profile=profile,kind=kind,case=case,
        http_status=status if type(status) is int else None,
        rpc_code=code if type(code) is int else None,error_object=error_object,
        expected_http_status=expected_status,expected_rpc_code=expected_code)
    # Never log params, credentials, error messages or arbitrary remote strings.
    with (lab.root/'https-scope.jsonl').open('a') as stream:stream.write(json.dumps(report)+'\n')
    require(error_object and type(status) is int and status==expected_status
        and type(code) is type(expected_code) and code==expected_code,
        'https_scope_response_changed:'+json.dumps(report,sort_keys=True))


def scope_checks(lab,topologies):
    configs=[]
    for _,nodes,clients in topologies.values():
        for c in clients:
            config=lab.config(c.node,c.credential['rune'],c.call('info')['grant']);configs.append(config)
            require(https.Client(config).call('info')['grant']==config['grant'],'https_identity_not_verified')
            base=dict(grant_id=c.gid,operation='info',body={})
            tests=[('method_'+m,m,{}) for m in ('getinfo','sendpay','close','withdraw','createrune',remote.CREDENTIAL,'swap-gate-release')]
            tests += [(case,remote.CALL,p) for case,p in (
                ('wrong_grant',dict(base,grant_id='0'*64)),('wrong_operation',dict(base,operation='close')),
                ('extra_parameter',dict(base,extra=True)))]
            before=c.mutations()
            profile=config['grant']['chain']['profile'];kind=config['grant']['kind']
            for case,method,params in tests:
                # CLNrest forwards core RUNE_NOT_PERMITTED (1502). Commando
                # wraps this as COMMANDO_ERROR_REMOTE_AUTH (19537) instead.
                scope_response(lab,profile,kind,case,raw(config,method,params),401,1502)
            # CLNrest accepts JSON objects only. Positional arrays fail parsing
            # before the rune check; they are not a rune-rejection control.
            scope_response(lab,profile,kind,'positional_body',raw(config,remote.CALL,list(base.values())),500,None)
            rune=config['rune'];forged=('A' if rune[0]!='A' else 'B')+rune[1:]
            scope_response(lab,profile,kind,'forged_rune',raw(config,remote.CALL,base,rune=forged),401,1501)
            require(c.mutations()==before,'https_denied_request_mutated')
    wrong=dict(configs[0],ca_pem=configs[-1]['ca_pem'])
    try:https.Client(wrong).call('info')
    except https.HttpsError:pass
    else:raise RuntimeError('https_wrong_CA_accepted')
    # A valid certificate and rune still cannot change the expected grant/node.
    wrong=copy.deepcopy(configs[0]);wrong['grant']=copy.deepcopy(configs[-1]['grant'])
    try:https.Client(wrong).call('info')
    except ValueError:pass
    else:raise RuntimeError('https_wrong_identity_accepted')
    print('PASS: native CLNrest TLS, exact grant identity and real rune scope refusals',flush=True)


def run(lab):
    topologies={label:customer.topology(lab,label,profile,grant_amount_msat=10000000)
        for label,profile in [('btc',ci.BTC),('xbt',ci.PRIVKEYIO_XBT)]}
    scope_checks(lab,topologies)
    backend,nodes,_=topologies['xbt'];lab.mine(backend,count=41);lab.synced(backend,*nodes)
    heights=[lab.rpc(t[1][0]['cli'],'getinfo')['blockheight'] for t in topologies.values()]
    require(heights[0]!=heights[1],'https_chain_heights_not_independent')
    receipts=[]
    def contract(incoming,outgoing,bolt,direction):
        clients=[https.Client(lab.config(c.node,c.credential['rune'],c.call('info')['grant']))
                 for c in (incoming,outgoing)]
        return cross.contract(*clients,bolt,direction)
    for direction in ('forward','reverse'):
        for name in ('release','fail','restart-held'):
            case=cross.scenario(lab,topologies,direction,name,driver_class=Driver,contract_factory=contract)
            case.pop('authenticated_peer_rpc')
            case.update(authenticated_https_rpc=True,saved_pairing_unchanged=True)
            receipts.append(case)
    for backend,nodes,_ in topologies.values():
        for node in reversed(nodes):lab.stop(node['proc'])
        lab.stop(backend['proc'])
    result=dict(schema=1,fixture='privkeyio-https-0082',scenarios=receipts,regtest_only=True,
        cross_chain_worker=True,independent_chain_heights=True,synthetic_price_snapshots=True,
        https_transport=True,native_clnrest=True,real_rune_enforcement=True,wrong_CA_refused=True,
        saved_pairing=True,scoped_https_planning=True,live_market_feed=False,onchain_recovery=False,
        automatic_payment_retries=False,production_activation_supported=False)
    (lab.root/'https-result.json').write_text(json.dumps(result,indent=2)+'\n');lab.checkpoint('complete')
    print('HTTPS FIXTURE PASSED: 0082; native CLNrest, saved pairing, both directions and recovery; no installer',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for name in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args=parser.parse_args();os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):setattr(args,name,getattr(args,name).resolve())
    for path in (Driver.script,customer.PLUGIN,cross.routes.gate.PLUGIN,
                 args.btc_prefix/'libexec/c-lightning/plugins/clnrest',args.xbt_prefix/'libexec/c-lightning/plugins/clnrest'):
        require(path.is_file() and os.access(path,os.X_OK),'https_fixture_executable_missing')
    lab=HttpsLab(args)
    try:run(lab)
    finally:lab.close()


if __name__=='__main__':main()
