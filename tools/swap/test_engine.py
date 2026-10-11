#!/usr/bin/env python3
"""Real released binaries, fresh regtest only; no supplied datadir/endpoints."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import chain_identity as ci
from bolt11 import unsigned


# decode belongs to offers, even when only BOLT11 invoices are being tested.
# topology supplies listincoming for core invoice creation.
# This direct-channel engine fixture uses core sendpay/waitsendpay. Legacy
# pay refuses inside its handler with deprecated APIs disabled, even though
# help and the plugin manifest still list it without deprecation metadata.
# spenderp supplies fundchannel/multifundchannel and uses core PSBT RPCs.
FIXTURE_PLUGINS = frozenset(('bcli', 'offers', 'topology', 'txprepare', 'spenderp'))
REQUIRED_RPCS = frozenset((
    'getinfo', 'invoice', 'decode', 'signinvoice', 'newaddr', 'listfunds',
    'connect', 'fundchannel', 'multifundchannel', 'listpeerchannels',
    'sendpay', 'waitsendpay', 'listincoming', 'listinvoices', 'help', 'plugin', 'listconfigs',
))


class FixtureRPCError(subprocess.CalledProcessError):
    """Keep raw synthetic RPC replies in logs, not invoice arguments in tracebacks."""
    def __init__(self, result, method, log):
        super().__init__(result.returncode, [method], result.stdout, result.stderr)
        self.method = method
        self.log = log
        self.code = None
        for stream in (result.stdout, result.stderr):
            try:
                reply = json.loads(stream)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(reply, dict) and type(reply.get('code')) is int:
                self.code = reply['code']
                break

    def __str__(self):
        code = '' if self.code is None else f'; RPC code {self.code}'
        return f'fixture RPC {self.method} failed (exit {self.returncode}{code}); details: {self.log}'


def check_capabilities(reply):
    rows = reply.get('help', []) if isinstance(reply, dict) else []
    available = {row['command'].split()[0] for row in rows
                 if isinstance(row, dict) and isinstance(row.get('command'), str) and row['command'].strip()}
    missing = REQUIRED_RPCS - available
    require(not missing, 'fixture_required_RPC_missing: ' + ', '.join(sorted(missing)))


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def check_received(reply, payment_hash, amount_msat):
    rows = reply.get('invoices', []) if isinstance(reply, dict) else []
    require(len(rows) == 1 and rows[0].get('payment_hash') == payment_hash
            and rows[0].get('status') == 'paid'
            and rows[0].get('amount_received_msat') == amount_msat,
            'fixture_recipient_payment_not_confirmed')


def plugins_ready(reply):
    active = {Path(p['name']).name for p in reply.get('plugins', []) if p.get('active') is True}
    return FIXTURE_PLUGINS <= active


def funding_address(lab, node):
    # With deprecated APIs disabled, no-argument newaddr returns only p2tr.
    # Keep this fixture's original P2WPKH funding input explicit.
    reply = lab.rpc(node['cli'],'newaddr','bech32')
    address = reply.get('bech32') if isinstance(reply,dict) else None
    require(isinstance(address,str) and bool(address), 'fixture_bech32_address_missing')
    return address


def direct_payment(lab, node, peer, channel, created, amount_msat):
    """One full payment on the fixture's known channel; no route search/retry."""
    decoded = ci.invoice(lab.rpc(node['cli'],'decode',created['bolt11']),
                         ci.PRIVKEYIO_XBT,'regtest')
    recipient = peer['identity']['node_id']
    require(decoded['payee'] == recipient and decoded['amount_msat'] == amount_msat
            and decoded['payment_hash'] == created['payment_hash'],
            'fixture_payment_invoice_mismatch')
    ci.channel(channel,ci.PRIVKEYIO_XBT)
    require(channel.get('state') == 'CHANNELD_NORMAL' and channel.get('peer_connected') is True
            and channel.get('reestablished') is True
            and channel.get('peer_id') == recipient
            and isinstance(channel.get('short_channel_id'),str) and bool(channel['short_channel_id']),
            'fixture_direct_channel_not_ready')
    delay = decoded.get('min_final_cltv_expiry')
    require(type(delay) is int and 0 < delay <= 0xffffffff, 'fixture_final_cltv_missing')
    # These aliases are accepted unconditionally by param_route_hops, and are
    # also used by the pinned pyln-testing LightningNode.pay direct helper.
    route = [dict(id=recipient,channel=channel['short_channel_id'],
                  amount_msat=amount_msat,delay=delay)]
    attempt = dict(payment_hash=decoded['payment_hash'],partid=0,groupid=1)

    def check(reply, statuses):
        require(isinstance(reply,dict) and reply.get('status') in statuses
                and reply.get('payment_hash') == attempt['payment_hash']
                and reply.get('groupid') == 1 and reply.get('partid',0) == 0
                and reply.get('destination') == recipient
                and reply.get('amount_msat') == amount_msat
                and reply.get('amount_sent_msat') == amount_msat,
                'fixture_payment_attempt_mismatch')

    # Never retry a mutating RPC after an uncertain reply. A new fixture run
    # creates new regtest datadirs; this is not a production recovery adapter.
    sent = lab.rpc_named(node['cli'],'sendpay',dict(attempt,route=route,
                         bolt11=created['bolt11'],payment_secret=decoded['payment_secret']),timeout=90)
    check(sent,('pending','complete'))
    completed = lab.rpc_named(node['cli'],'waitsendpay',dict(attempt,timeout=60),timeout=90)
    check(completed,('complete',))


class Lab:
    # Subclasses that test source builds must declare their exact versions.
    engine_versions = {ci.BTC: ('v26.06.9', '26.06.9'),
                       ci.PRIVKEYIO_XBT: ('v26.06.9-blake2b.7', '26.06.9-blake2b.7')}

    def __init__(self, args):
        self.args = args
        self.root = Path(tempfile.mkdtemp(prefix='privkeyio-0066-', dir=args.results_parent))
        self.procs = []; self.logs = []; self.ports = set()
        self.current_stage = 'starting'
        print('Disposable logs: ' + str(self.root), flush=True)

    def checkpoint(self, stage):
        self.current_stage = stage
        (self.root/'progress.json').write_text(json.dumps(dict(stage=stage),indent=2)+'\n')
        print('STAGE: ' + stage, flush=True)

    def port(self):
        while True:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
            if port not in self.ports:
                self.ports.add(port); return port

    def start(self, command, log):
        output = log.open('a'); self.logs.append(output)
        proc = subprocess.Popen(list(map(str,command)),stdout=output,stderr=subprocess.STDOUT,
                                start_new_session=True)
        self.procs.append(proc); return proc

    def rpc(self, cli, *args, timeout=30):
        try:
            result = subprocess.run([*map(str,cli),*map(str,args)],capture_output=True,text=True,timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'fixture RPC {args[0]} timed out after {timeout}s; inspect disposable logs') from None
        if result.returncode:
            log = self.root/'rpc-errors.jsonl'
            with log.open('a') as output:
                output.write(json.dumps(dict(stage=getattr(self,'current_stage','unknown'),
                                             method=str(args[0]),exit_code=result.returncode,
                                             stdout=result.stdout,stderr=result.stderr))+'\n')
            raise FixtureRPCError(result,str(args[0]),log)
        try: return json.loads(result.stdout)
        except json.JSONDecodeError: return result.stdout.strip() or None

    def rpc_named(self, cli, method, params, *, timeout=30):
        # lightning-cli -k preserves JSON literals, including quoted strings.
        # JSON-encode every value so a numeric-looking hash stays a string.
        args = [key+'='+json.dumps(value,separators=(',',':')) for key,value in params.items()]
        return self.rpc([*cli,'-k'],method,*args,timeout=timeout)

    def wait(self, fn, proc=None, timeout=90):
        end = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < end:
            require(proc is None or proc.poll() is None, 'fixture_process_exited; inspect disposable logs')
            try:
                value = fn()
                if value: return value
            except (subprocess.CalledProcessError,ci.IdentityError,ConnectionError,OSError) as error:
                last_error = str(error)
            time.sleep(.1)
        raise RuntimeError('fixture_timeout; inspect disposable logs' +
                           (f'; last error: {last_error}' if last_error else ''))

    def backend(self, name, fork):
        root = self.root/name;root.mkdir()
        port = self.port()
        command = [self.args.bitcoind,f'-datadir={root}','-conf=/dev/null','-regtest',
                   '-server=1','-listen=0','-dnsseed=0','-discover=0','-connect=0',
                   '-rpcbind=127.0.0.1','-rpcallowip=127.0.0.1',f'-rpcport={port}',
                   '-fallbackfee=0.00001','-rejectparasites=0']
        if fork:
            command += ['-testactivationheight=blake2b@1','-rdtsexpiry=2147483647',
                        '-blake2b_headline=Disposable CLN migration fixture']
        proc = self.start(command,root/'console.log')
        cli = [self.args.bitcoin_cli,f'-datadir={root}','-conf=/dev/null','-regtest',f'-rpcport={port}']
        self.wait(lambda:self.rpc(cli,'getblockchaininfo'),proc)
        self.rpc(cli,'createwallet','fixture')
        address = self.rpc(cli,'getnewaddress')
        self.rpc(cli,'generatetoaddress',101,address)
        require(self.rpc(cli,'getblockchaininfo')['chain']=='regtest','regtest_required')
        tip = self.rpc(cli,'getbestblockhash');header = self.rpc(cli,'getblockheader',tip,'false')
        require(len(bytes.fromhex(header)) == (164 if fork else 80), 'unexpected_backend_header')
        return dict(root=root,port=port,cli=cli,proc=proc,address=address)

    def lightning(self, name, profile, backend, *, expect_refusal=False):
        require(profile in (ci.BTC,ci.PRIVKEYIO_XBT),'fixture_profile_refused')
        prefix = self.args.xbt_prefix if profile==ci.PRIVKEYIO_XBT else self.args.btc_prefix
        root = self.root/name;root.mkdir(exist_ok=True)
        port = self.port(); log = root/('refusal.log' if expect_refusal else 'console.log')
        command = [prefix/'bin/lightningd',f'--lightning-dir={root}','--conf=/dev/null','--network=regtest',
                   f'--bitcoin-cli={self.args.bitcoin_cli}',f'--bitcoin-datadir={backend["root"]}',
                   f'--bitcoin-rpcport={backend["port"]}','--developer','--allow-deprecated-apis=false',
                   '--dev-bitcoind-poll=1',
                   '--disable-dns','--autoconnect-seeker-peers=0','--autolisten=false',
                   f'--bind-addr=127.0.0.1:{port}','--log-level=debug']
        # This fixture checks core identity, encoding and signing. Sidecars,
        # web APIs, auto-connect helpers and unrelated plugins are not needed.
        for plugin in (prefix/'libexec/c-lightning/plugins').iterdir():
            if plugin.name not in FIXTURE_PLUGINS:
                command += ['--disable-plugin='+plugin.name]
        if profile == ci.PRIVKEYIO_XBT:
            command += ['--dev-blake2b-activation-height=1']
        proc = self.start(command,log)
        cli = [prefix/'bin/lightning-cli',f'--lightning-dir={root}','--network=regtest',
               '--json','--notifications=none']
        if expect_refusal:
            proc.wait(timeout=45)
            require(proc.returncode != 0 and '80-byte header' in log.read_text(),
                    'wrong_backend_not_rejected_by_header_guard')
            return
        identity = self.wait(lambda:ci.node(self.rpc(cli,'getinfo'),profile,'regtest'),proc)
        # The RPC socket is created before plugins_config completes. A working
        # getinfo or a registered method does not prove plugin init finished.
        def initialized_plugins():
            reply = self.rpc(cli,'plugin','list')
            (root/'plugin-readiness.json').write_text(json.dumps(reply,indent=2)+'\n')
            return reply if plugins_ready(reply) else None
        plugins = self.wait(initialized_plugins,proc)
        info = self.rpc(cli,'getinfo')
        help_reply = self.rpc(cli,'help')
        config = self.rpc(cli,'listconfigs','allow-deprecated-apis')
        report = dict(node=name,profile=profile,stage=self.current_stage,
                      version=info['version'],help=help_reply,plugins=plugins,config=config)
        with (self.root/'startup-reports.jsonl').open('a') as output:
            output.write(json.dumps(report)+'\n')
        require(config['configs']['allow-deprecated-apis']['value_bool'] is False,
                'fixture_deprecated_APIs_unexpectedly_enabled')
        check_capabilities(help_reply)
        version = info['version']
        require(version in self.engine_versions[profile], 'unexpected_engine_version')
        def listening():
            with socket.create_connection(('127.0.0.1',port),timeout=1):return True
        self.wait(listening,proc)
        return dict(cli=cli,proc=proc,port=port,root=root,identity=identity)

    def mine(self, backend, count=6):
        return self.rpc(backend['cli'],'generatetoaddress',count,backend['address'])

    def synced(self, backend, *nodes):
        height = self.rpc(backend['cli'],'getblockcount')
        for n in nodes:
            self.wait(lambda:self.rpc(n['cli'],'getinfo')['blockheight']==height,n['proc'])

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=15)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)

    def close(self):
        for proc in reversed(self.procs): self.stop(proc)
        for log in self.logs:log.close()


def rejected(fn):
    try: fn()
    except ci.IdentityError: return
    raise RuntimeError('wrong_identity_was_accepted')


def run(lab):
    lab.checkpoint('fresh_regtest_backends')
    btc_backend = lab.backend('btc-chain',False)
    xbt_backend = lab.backend('xbt-chain',True)
    print('PASS: fresh regtest backends; SHA256 80-byte and BLAKE2b 164-byte headers',flush=True)
    lab.checkpoint('node_identity_and_runtime_capabilities')
    btc = lab.lightning('btc',ci.BTC,btc_backend)
    xbt = lab.lightning('xbt',ci.PRIVKEYIO_XBT,xbt_backend)
    original_identity = xbt['identity']
    ci.pair(btc['identity'],xbt['identity'])
    rejected(lambda:ci.node(lab.rpc(btc['cli'],'getinfo'),ci.PRIVKEYIO_XBT,'regtest'))
    rejected(lambda:ci.node(lab.rpc(xbt['cli'],'getinfo'),ci.BTC,'regtest'))
    print('PASS: both nodes report regtest; protocol checks separate their actual RPC identities',flush=True)
    lab.checkpoint('wrong_backend_refusal')
    lab.lightning('wrong-backend',ci.PRIVKEYIO_XBT,btc_backend,expect_refusal=True)
    print('PASS: pinned fork refuses the SHA256 backend after activation',flush=True)

    lab.checkpoint('invoice_signing_and_chain_boundaries')
    invoices = {}
    for label,node,profile in (('btc',btc,ci.BTC),('xbt',xbt,ci.PRIVKEYIO_XBT)):
        normal = lab.rpc(node['cli'],'invoice',1500000,'profile-'+label,'disposable fixture')['bolt11']
        invoices[label] = normal
        ci.invoice(lab.rpc(node['cli'],'decode',normal),profile,'regtest')
        text = unsigned('33'*32,'44'*32,1500000,profile=profile,environment='regtest',expiry=1800,final_cltv=120)
        signed = lab.rpc(node['cli'],'signinvoice',text)['bolt11']
        d = lab.rpc(node['cli'],'decode',signed)
        ci.invoice(d,profile,'regtest')
        require(d['payment_hash']=='33'*32 and d['payment_secret']=='44'*32 and d['amount_msat']==1500000
                and d['min_final_cltv_expiry']==120 and d['expiry']==1800
                and d['payee']==node['identity']['node_id'],'signed_invoice_fields_changed')
        require(not ci.feature_bits(d['features']) & ((1<<16)|(1<<17)), 'swap_invoice_advertises_mpp')
    # A privkeyio decode can accept a plain Bitcoin invoice. Our explicit
    # profile check must refuse it before any route search/payment authority.
    foreign = lab.rpc(xbt['cli'],'decode',invoices['btc'])
    require(foreign.get('valid') is True,'expected_generic_decoder_behavior_changed')
    rejected(lambda:ci.invoice(foreign,ci.PRIVKEYIO_XBT,'regtest'))
    try: lab.rpc(btc['cli'],'decode',invoices['xbt'])
    except subprocess.CalledProcessError as error:
        require('512' in error.stdout+error.stderr,'unexpected_foreign_invoice_error')
    else: raise RuntimeError('stock_BTC_decoder_accepted_compulsory_XBT_bit')
    print('PASS: real signinvoice/decode round trips; required XBT bit, one-part invoices, cross-chain refusals',flush=True)

    lab.checkpoint('fund_unified_channel')
    lab.stop(btc['proc'])
    peer = lab.lightning('xbt-peer',ci.PRIVKEYIO_XBT,xbt_backend)
    address = funding_address(lab,xbt)
    lab.rpc(xbt_backend['cli'],'sendtoaddress',address,'0.01')
    lab.mine(xbt_backend);lab.synced(xbt_backend,xbt,peer)
    lab.wait(lambda:any(o['status']=='confirmed' for o in lab.rpc(xbt['cli'],'listfunds')['outputs']),xbt['proc'])
    lab.rpc(xbt['cli'],'connect',peer['identity']['node_id'],'127.0.0.1',peer['port'])
    opened = lab.rpc(xbt['cli'],'fundchannel',peer['identity']['node_id'],300000,timeout=90)
    tx = lab.rpc(xbt_backend['cli'],'decoderawtransaction',opened['tx'])
    sigs = [bytes.fromhex(w) for vin in tx['vin'] for w in vin.get('txinwitness',[])
            if len(bytes.fromhex(w))==65 or (69<=len(bytes.fromhex(w))<=73 and w.startswith('30'))]
    require(sigs and all(s[-1] in (0x21,0xa3) for s in sigs),'funding_not_unified_signed')
    lab.mine(xbt_backend);lab.synced(xbt_backend,xbt,peer)
    def normal(n):
        rows=lab.rpc(n['cli'],'listpeerchannels')['channels']
        if (len(rows)==1 and rows[0]['state']=='CHANNELD_NORMAL'
                and rows[0]['peer_connected'] is True and rows[0].get('reestablished') is True):
            return ci.channel(rows[0],ci.PRIVKEYIO_XBT)
    original = lab.wait(lambda:normal(xbt),xbt['proc'])
    lab.wait(lambda:normal(peer),peer['proc'])
    def payment(label):
        lab.checkpoint(label)
        channel=lab.wait(lambda:normal(xbt),xbt['proc'])
        lab.wait(lambda:normal(peer),peer['proc'])
        created=lab.rpc(peer['cli'],'invoice',1000000,label,'disposable channel check')
        direct_payment(lab,xbt,peer,channel,created,1000000)
        check_received(lab.rpc(peer['cli'],'listinvoices',label),created['payment_hash'],1000000)
        # This fixture tests restart after settlement, not during a pending HTLC.
        for n in (xbt,peer):
            lab.wait(lambda n=n:(row:=normal(n)) is not None and row.get('htlcs') == [],n['proc'])
    payment('before-restart')
    lab.checkpoint('restart_original_channel')
    lab.stop(xbt['proc'])
    xbt = lab.lightning('xbt',ci.PRIVKEYIO_XBT,xbt_backend)
    try: lab.rpc(xbt['cli'],'connect',peer['identity']['node_id'],'127.0.0.1',peer['port'])
    except subprocess.CalledProcessError:pass
    restored = lab.wait(lambda:normal(xbt),xbt['proc'])
    require(restored['channel_id']==original['channel_id'] and restored['funding_txid']==original['funding_txid']
            and restored['short_channel_id']==original['short_channel_id']
            and restored['channel_type']==original['channel_type'],'channel_signing_type_changed_after_restart')
    payment('after-restart')
    print('PASS: funded unified-signature channel; payments before/after restart; original funding and signing type retained',flush=True)
    ci.same_binding(original_identity,ci.node(lab.rpc(xbt['cli'],'getinfo'),ci.PRIVKEYIO_XBT,'regtest'))
    rejected(lambda:ci.same_binding(None,xbt['identity']))
    rejected(lambda:ci.same_binding(ci.binding(ci.LEGACY_XBT,'regtest',xbt['identity']['node_id']),xbt['identity']))
    receipt=dict(schema=1,fixture='privkeyio-chain-boundary-0066e',regtest_only=True,
                 real_engine_and_invoice_checks=True,unified_channel_restart=True,
                 recipient_receipts_checked=True,deprecated_apis_enabled=False,
                 payment_rpc='sendpay/waitsendpay',payment_route='known_direct_channel',
                 automatic_payment_retries=False,
                 full_swap_controller_tested=False,legacy_funded_channel_migration_tested=False)
    (lab.root/'result.json').write_text(json.dumps(receipt,indent=2)+'\n')
    lab.checkpoint('complete')
    print('ENGINE FIXTURE PASSED: 0066e; isolated regtest only; no installer produced',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-regtest',action='store_true',required=True)
    for arg in ('btc-prefix','xbt-prefix','bitcoind','bitcoin-cli'):
        parser.add_argument('--'+arg,type=Path,required=True)
    parser.add_argument('--results-parent',type=Path,default=Path(tempfile.gettempdir()))
    args=parser.parse_args();os.umask(0o077)
    for name in ('btc_prefix','xbt_prefix','bitcoind','bitcoin_cli'):
        setattr(args,name,getattr(args,name).resolve())
    for p in (args.btc_prefix/'bin/lightningd',args.xbt_prefix/'bin/lightningd',args.bitcoind,args.bitcoin_cli):
        require(p.is_file() and os.access(p,os.X_OK),'fixture_executable_missing')
    lab=Lab(args)
    try:run(lab)
    finally:lab.close()


if __name__=='__main__':main()
