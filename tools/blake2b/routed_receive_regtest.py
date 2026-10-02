"""BTC -> routed XBT controller fixture; disposable regtest, fixed price only."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from smoke_regtest import Lab, wait_until
from swap_controller import save
from swap_invoice import unsigned_invoice
from outgoing_xbt import MODE, AMOUNT, plan


def wait_for_ready(lab, node, backend, network, expected_id):
    """RPC/listener readiness and HTLC replay can precede chain sync."""
    height = lab.rpc(backend['cli'], 'getblockcount')
    def ready():
        info = lab.rpc(node['cli'], 'getinfo')
        if info['network'] != network or info['id'] != expected_id:
            raise AssertionError('restarted fixture identity or network changed')
        return (info['blockheight'] >= height
                and not any(k.startswith('warning_') for k in info))
    wait_until(ready, node['proc'], timeout=90)


def run(lab, fail=False, fee_limit=False, api=False, private_hint=False, onchain=None, bounded=False, stale_margin=False):
    btc, xbt = lab.node('knots-btc', False), lab.node('knots-xbt', True)
    gate, hold = lab.root/'quote_plugin.py', lab.root/'hold_htlc.py'
    for path in (gate, hold):
        path.write_text('#!'+sys.executable+'\n'+Path(__file__).with_name(path.name).read_text())
        path.chmod(0o700)
    payer = lab.lightning('payer', 'regtest', btc)
    incoming = lab.lightning('btc-operator', 'regtest', btc, plugins=(gate,))
    outgoing = lab.lightning('xbt-operator', 'xbt-regtest', xbt)
    if onchain:
        relay_root = lab.root/'xbt-relay'
        relay_root.mkdir(mode=0o700)
        (relay_root/'config').write_text('cltv-delta=30\n')
    relay = lab.lightning('xbt-relay', 'xbt-regtest', xbt)
    receiver = lab.lightning('receiver', 'xbt-regtest', xbt, plugins=(hold,))
    nodes = (payer, incoming, outgoing, relay, receiver)
    rpc = lambda node,*a: lab.rpc(node['cli'],*a)
    def channel(node, peer):
        rows=[c for c in rpc(node,'listpeerchannels')['channels'] if c['peer_id']==peer['id']]
        assert len(rows)==1
        return rows[0]
    def mine(backend,count):
        rpc(backend,'generatetoaddress',count,rpc(backend,'getnewaddress'))
        height=rpc(backend,'getblockcount')
        for n in ((payer,incoming) if backend is btc else (outgoing,relay,receiver)):
            wait_until(lambda:rpc(n,'getinfo')['blockheight']>=height,n['proc'],timeout=90)
    def fund(backend,sender,recipient,private=False):
        mine(backend,1)
        tx=rpc(backend,'sendtoaddress',rpc(sender,'newaddr','bech32')['bech32'],'0.02')
        mine(backend,1)
        wait_until(lambda:any(o['txid']==tx and o['status']=='confirmed' for o in rpc(sender,'listfunds')['outputs']),sender['proc'])
        rpc(sender,'connect',recipient['id'],'127.0.0.1',recipient['port'])
        funding=lab.rpc([*sender['cli'],'-k'],'fundchannel','id='+recipient['id'],
                        'amount=1000000sat','announce='+('false' if private else 'true'))
        wait_until(lambda:funding['txid'] in rpc(backend,'getrawmempool'))
        mine(backend,6)
        for a,b in ((sender,recipient),(recipient,sender)):
            wait_until(lambda:channel(a,b)['state']=='CHANNELD_NORMAL',a['proc'])
    fund(btc,payer,incoming)
    if bounded:
        # Incoming operator has no funding change; provide its on-chain reserve.
        reserve_tx=rpc(btc,'sendtoaddress',rpc(incoming,'newaddr','bech32')['bech32'],'0.001')
        mine(btc,1)
        wait_until(lambda:any(o['txid']==reserve_tx and o['status']=='confirmed'
                             for o in rpc(incoming,'listfunds')['outputs']),incoming['proc'])
    fund(xbt,outgoing,relay)
    fund(xbt,relay,receiver,private=private_hint)
    rpc(relay,'setchannel',channel(relay,receiver)['short_channel_id'],'5000msat',0)
    assert not any(c['peer_id']==receiver['id'] for c in rpc(outgoing,'listpeerchannels')['channels'])
    initial={(n['id'],c['short_channel_id']):c['to_us_msat'] for n in nodes for c in rpc(n,'listpeerchannels')['channels']}
    if private_hint:
        wait_until(lambda: channel(receiver,relay).get('updates',{}).get('remote',{}).get('fee_base_msat') == 5000
                   and channel(receiver,relay)['updates']['remote']['fee_proportional_millionths'] == 0,
                   receiver['proc'])
        assert channel(receiver,relay)['private']
        assert not rpc(outgoing,'listchannels',channel(relay,receiver)['short_channel_id'])['channels']
    inv=lab.rpc([*receiver['cli'],'-k'],'invoice','amount_msat='+str(AMOUNT)+'msat',
                'label=routed-receive','description=Routed XBT delivery',
                'exposeprivatechannels='+('true' if private_hint else 'false'))
    decoded=rpc(outgoing,'decode',inv['bolt11'])
    def route_ready():
        route,policy=plan(outgoing['cli'],decoded,outgoing['id'],lab.rpc)
        return (route,policy) if route[0]['amount_msat']==AMOUNT+5000 else None
    route,policy=wait_until(route_ready,outgoing['proc'],timeout=90)
    assert len(route)==2 and [h['id'] for h in route]==[relay['id'],receiver['id']]
    if onchain: assert route[0]['delay'] == 70 and route[-1]['delay'] == 40
    if private_hint:
        from reverse_route import no_route
        assert any(len(h)==1 and h[0]['pubkey']==relay['id']
                   and h[0]['short_channel_id']==route[-1]['channel'] for h in decoded.get('routes',[]))
        try:
            plan(outgoing['cli'],dict(decoded,routes=[]),outgoing['id'],lab.rpc)
        except subprocess.CalledProcessError as error:
            assert no_route(error)
        else:
            raise AssertionError('private receiver reachable without signed hint')
        print('PASS: signed XBT hint supplies private final hop absent from operator gossip; total route fee 5 sats',flush=True)
    else:
        print('PASS: no direct receiver channel; public two-hop XBT route charges 5 sats within 10-sat cap',flush=True)
    workflow = None
    if api:
        from routed_receive_api_regtest import Workflow
        workflow = Workflow(lab, incoming, outgoing, inv['bolt11'], bounded=bounded)
        btc_invoice = workflow.offer['btc_invoice']
        btc_amount = workflow.offer['btc_sats'] * 1000
        ph = inv['payment_hash']
    else:
        ph=inv['payment_hash']; secret=secrets.token_hex(32)
        terms=dict(payment_hash=ph,payment_secret=secret,btc_amount_msat=100000000,xbt_amount_msat=AMOUNT,
                   xbt_invoice=inv['bolt11'],expires_at=int(time.time())+3600,min_cltv_delta=100,max_cltv_delta=2000)
        assert rpc(incoming,'xbt-register',json.dumps(terms))['registered']
        btc_invoice=rpc(incoming,'signinvoice',unsigned_invoice(ph,secret))['bolt11']
        btc_amount = 100000000
    paying=lab.start([*payer['cli'],'-k','pay','bolt11='+btc_invoice,'retry_for=0'],lab.root/'payer-pay.log')
    def committed(node,peer,direction):
        expected='RCVD_ADD_ACK_REVOCATION' if direction=='in' else 'SENT_ADD_ACK_REVOCATION'
        return any(h['payment_hash']==ph and h['state']==expected and h['direction']==direction
                   for h in channel(node,peer).get('htlcs',[]))
    wait_until(lambda:committed(incoming,payer,'in'),incoming['proc'])
    wait_until(lambda:committed(payer,incoming,'out'),payer['proc'])
    status=rpc(incoming,'xbt-quote-status',ph)
    assert status['phase']=='held'
    if api:
        path = workflow.directory/'state.json'
        state = workflow.quote['controller']
        controller = workflow.controller
    else:
        path=lab.root/'state.json'
        state=dict(phase='prepared',quote_gate=True,payment_hash=ph,payment_secret=decoded['payment_secret'],
                   xbt_invoice=inv['bolt11'],xbt_amount_msat=AMOUNT,btc_binding=status['binding'],
                   btc_cli=incoming['cli'],xbt_cli=outgoing['cli'],btc_node_id=incoming['id'],
                   xbt_routing=MODE,xbt_route_policy=policy,route=route)
        if fee_limit: state['xbt_route_policy']['max_fee_msat']=4999
        save(path,state)
        cmd=[sys.executable,str(Path(__file__).with_name('swap_controller.py')),'--state',str(path)]
        def controller(*args):
            return subprocess.run([*cmd,*args],capture_output=True,text=True,timeout=45)
    if stale_margin:
        terms=workflow.quote['terms']
        spend=rpc(incoming,'xbt-spend-info',ph)
        current=rpc(btc,'getblockcount')
        mine(btc,spend['cltv_expiry']-current-terms['min_cltv_delta']+1)
        before=(workflow.directory/'quote.json').read_bytes()
        for _ in range(2):
            result=workflow.step_result()
            assert result.get('outcome') == 'refused' and result.get('phase') == 'prepared', result
            assert (workflow.directory/'quote.json').read_bytes()==before
            assert json.loads(path.read_text())['phase']=='prepared'
            assert rpc(outgoing,'listsendpays')['payments']==[]
            assert rpc(incoming,'xbt-quote-status',ph)['phase']=='held'
        assert rpc(incoming,'xbt-fail',ph,json.dumps(status['binding']))['failed']==1
        print('PASS: fresh BTC height one block below required margin refused twice; no XBT attempt; harness returned BTC',flush=True)
    elif fee_limit:
        before=path.read_bytes()
        for _ in range(2):
            assert controller().returncode!=0
            assert path.read_bytes()==before
            assert rpc(outgoing,'listsendpays')['payments']==[]
        assert rpc(incoming,'xbt-quote-status',ph)['phase']=='held'
        assert rpc(incoming,'xbt-fail',ph,json.dumps(state['btc_binding']))['failed']==1
        print('PASS: over-budget route refused twice before XBT send; harness returned unspent BTC',flush=True)
    else:
        if api:
            # waitsendpay times out on the deliberately held receiver. The
            # next worker step must reconcile the durable submission.
            controller()
            assert path.exists() and json.loads(path.read_text())['phase'] == 'outgoing_started'
        else:
            crashed=controller('--crash-after-sendpay')
            assert crashed.returncode==88, 'controller did not reach submission checkpoint: '+crashed.stderr
        wait_until(lambda:committed(outgoing,relay,'out'),outgoing['proc'])
        wait_until(lambda:committed(receiver,relay,'in'),receiver['proc'])
        checkpoint=json.loads(path.read_text())
        assert checkpoint['phase']=='outgoing_started' and 'preimage' not in checkpoint
        assert checkpoint['xbt_first_hop']['funding_txid']==channel(outgoing,relay)['funding_txid']
        attempt=rpc(outgoing,'listsendpays')['payments'][0]
        attempt_id=tuple(attempt.get(k) for k in ('id','groupid','partid'))
        before=gate.with_suffix('.quotes.json').read_bytes()
        for n in (incoming,outgoing): lab.stop(n['proc'])
        incoming.update(lab.lightning('btc-operator','regtest',btc,plugins=(gate,)))
        outgoing.update(lab.lightning('xbt-operator','xbt-regtest',xbt))
        wait_for_ready(lab,incoming,btc,'regtest',state['btc_node_id'])
        wait_for_ready(lab,outgoing,xbt,'xbt-regtest',state['xbt_route_policy']['source'])
        rpc(payer,'connect',incoming['id'],'127.0.0.1',incoming['port'])
        rpc(outgoing,'connect',relay['id'],'127.0.0.1',relay['port'])
        wait_until(lambda:any(h['payment_hash']==ph for h in rpc(incoming,'xbt-held')['held']),incoming['proc'])
        wait_until(lambda:committed(outgoing,relay,'out'),outgoing['proc'])
        assert gate.with_suffix('.quotes.json').read_bytes()==before
        for _ in range(2):
            result=controller(); assert result.returncode==0,result.stderr
            assert json.loads(result.stdout)['outcome']=='pending'
            assert json.loads(path.read_text())==checkpoint
        print('PASS: coordinators restarted pending; original route, funding pin and BTC binding preserved; no resend',flush=True)
        if onchain:
            from routed_receive_onchain import finish
            finish(lab, workflow, payer, incoming, outgoing, relay, receiver,
                   btc, xbt, inv, initial, paying, gate, onchain)
            return
        method,field=('xbt-fail','failed') if fail else ('xbt-continue','continued')
        assert rpc(receiver,method,ph)[field]==1
        terminal='failed' if fail else 'complete'
        wait_until(lambda:rpc(outgoing,'listsendpays')['payments'][0]['status']==terminal,outgoing['proc'])
        for _ in range(2):
            result=controller();assert result.returncode==0,result.stderr
            assert json.loads(result.stdout)['phase']==('btc_failed' if fail else 'btc_released')
        attempts=rpc(outgoing,'listsendpays')['payments']
        assert len(attempts)==1 and tuple(attempts[0].get(k) for k in ('id','groupid','partid'))==attempt_id
    failed=fail or fee_limit or stale_margin
    paying.wait(timeout=40)
    assert (paying.returncode!=0)==failed
    deltas={k:0 for k in initial}
    if not failed:
        for a,b,amount in ((payer,incoming,btc_amount),(outgoing,relay,AMOUNT+5000),(relay,receiver,AMOUNT)):
            scid=channel(a,b)['short_channel_id']
            deltas[(a['id'],scid)]-=amount;deltas[(b['id'],scid)]+=amount
    for n in nodes:
        def settled():
            channels=rpc(n,'listpeerchannels')['channels']
            return len(channels)==sum(k[0]==n['id'] for k in initial) and all(
                not c.get('htlcs') and c['state']=='CHANNELD_NORMAL'
                and c['to_us_msat']==initial[(n['id'],c['short_channel_id'])]+deltas[(n['id'],c['short_channel_id'])]
                for c in channels)
        wait_until(settled,n['proc'])
    paid=rpc(receiver,'listinvoices','routed-receive')['invoices'][0]
    assert paid['status']==('unpaid' if failed else 'paid')
    if not failed:
        assert paid['amount_received_msat']==AMOUNT
        assert rpc(payer,'listpays',btc_invoice)['pays'][0]['preimage']==paid['payment_preimage']
    print('PASS: all six channel-side balances verified; no pending HTLCs; relay earned '+str(0 if failed else 5)+' sats',flush=True)
    if api and not stale_margin:
        print('PASS: API authorization and fresh background workers delivered the original quote without reprice or resend', flush=True)
    print(('Routed receive API OK (' if api else 'Routed BTC -> XBT delivery OK (')+('stale margin refusal' if stale_margin else 'fee refusal' if fee_limit else 'XBT rejection' if fail else 'success')+('; private invoice hint' if private_hint else '; public route')+'; regtest only)',flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bitcoind',type=Path,required=True)
    p.add_argument('--bitcoin-cli',type=Path,required=True)
    modes=p.add_mutually_exclusive_group()
    modes.add_argument('--fail-outgoing',action='store_true')
    modes.add_argument('--fee-limit',action='store_true')
    modes.add_argument('--onchain-preimage',action='store_true')
    modes.add_argument('--onchain-timeout',action='store_true')
    modes.add_argument('--stale-margin',action='store_true')
    p.add_argument('--bounded-policy',action='store_true',help='candidate forward checks; regtest only')
    p.add_argument('--private-hint',action='store_true',help='unannounced final XBT channel via signed invoice hint')
    p.add_argument('--api',action='store_true',help='exercise authenticated quote API and background worker')
    p.add_argument('--work-dir',type=Path)
    a=p.parse_args();temp=None
    onchain = 'preimage' if a.onchain_preimage else 'timeout' if a.onchain_timeout else None
    if a.bounded_policy and (not a.api or a.private_hint or onchain or a.fee_limit):
        p.error('--bounded-policy requires public-route --api without on-chain or fee-limit modes')
    if a.stale_margin and not a.bounded_policy: p.error('--stale-margin requires --bounded-policy')
    if onchain and not a.api: p.error('on-chain cases require --api')
    if a.api and a.fee_limit: p.error('--api supports success and --fail-outgoing')
    if a.work_dir:
        root=a.work_dir.resolve();root.mkdir(mode=0o700,parents=True,exist_ok=False)
    else:
        temp=tempfile.TemporaryDirectory(prefix='cln-routed-receive-');root=Path(temp.name)
    lab=Lab(root,str(a.bitcoind.resolve()),str(a.bitcoin_cli.resolve()))
    print('Test directory: '+str(root),flush=True)
    try: run(lab,a.fail_outgoing,a.fee_limit,a.api,a.private_hint,onchain,a.bounded_policy,a.stale_margin)
    finally:
        lab.close()
        if temp:temp.cleanup()


if __name__=='__main__': main()
