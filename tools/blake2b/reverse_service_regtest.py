#!/usr/bin/env python3
"""Exercise real reverse-service quote, submission and recovery on isolated regtest.

No live activation override, network rewriting or external oracle request.
A deterministic order book is injected only at the price-fetch boundary.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading

from smoke_regtest import Lab, wait_until
from reverse_check import check
from reverse_service import create
import reverse_customer as customer
from reverse_quote_api import Quotes, Server
from reverse_request import request_quote
from reverse_live import SERVICE_REGTEST, LIVE_EXECUTION_ENABLED
from swap_controller import save


class ServiceLab(Lab):
    def start(self, args, logfile, new_session=False):
        if Path(args[0]).name == 'lightningd':
            if not any(a in ('--network=regtest', '--network=xbt-regtest') for a in args):
                raise RuntimeError('service lab refuses non-regtest daemon')
            args = [*args, '--force-feerates=253']
            if any(a.endswith('/btc-relay') and a.startswith('--lightning-dir=') for a in args):
                args.append('--cltv-delta=304')
            if any(a.endswith('/btc-receiver') and a.startswith('--lightning-dir=') for a in args):
                args.append('--cltv-final=144')
        return super().start(args, logfile, new_session)


def run(lab, fail_outgoing=False, abort_unspent=False, auto_process=False, any_xbt=False, second_payer=False, routed_xbt=False, customer_routed_xbt=False):
    if customer_routed_xbt:
        routed_xbt = True
    if routed_xbt:
        any_xbt = True
    if LIVE_EXECUTION_ENABLED:
        raise RuntimeError('expected live reverse activation to remain disabled')
    private_hint = True
    btc, xbt = lab.node('knots-btc', False), lab.node('knots-xbt', True)
    gate = lab.root/'reverse-service-gate.py'
    hold = lab.root/'hold_htlc.py'
    gate.write_text(f'#!{sys.executable}\n' +
        'import sys\nfrom pathlib import Path\n' +
        f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n' +
        'from reverse_gate import main\n' +
        f'main(Path({str(lab.root/"service-gate.json")!r}), service_regtest=True)\n')
    gate.chmod(0o700)
    hold.write_text(f'#!{sys.executable}\n'+Path(__file__).with_name('hold_htlc.py').read_text())
    hold.chmod(0o700)
    payer = lab.lightning('xbt-payer', 'xbt-regtest', xbt)
    payers = [payer, lab.lightning('xbt-payer-2', 'xbt-regtest', xbt)] if any_xbt else [payer]
    if second_payer:
        payer = payers[1]
    incoming = lab.lightning('xbt-operator', 'xbt-regtest', xbt, plugins=(gate,))
    xbt_relay = lab.lightning('xbt-relay', 'xbt-regtest', xbt) if routed_xbt else None
    incoming_peer = xbt_relay if routed_xbt else payer
    payer_peer = xbt_relay if routed_xbt else incoming
    outgoing = lab.lightning('btc-operator', 'regtest', btc)
    relay = lab.lightning('btc-relay', 'regtest', btc)
    receiver = lab.lightning('btc-receiver', 'regtest', btc, plugins=(hold,))
    xbt_nodes = (*payers, incoming, *([xbt_relay] if routed_xbt else []))
    btc_nodes = (outgoing, relay, receiver)
    nodes = (*xbt_nodes, *btc_nodes)

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node, peer):
        matches = [c for c in rpc(node, 'listpeerchannels')['channels'] if c['peer_id'] == peer['id']]
        if len(matches) != 1:
            raise AssertionError('expected one channel to fixture peer')
        return matches[0]

    def mine(backend, count):
        peers = btc_nodes if backend is btc else xbt_nodes
        rpc(backend, 'generatetoaddress', count, rpc(backend, 'getnewaddress'))
        height = rpc(backend, 'getblockcount')
        for node in peers:
            wait_until(lambda: rpc(node, 'getinfo')['blockheight'] >= height, node['proc'], timeout=90)

    def fund(backend, sender, recipient, private=False):
        mine(backend, 1)
        txid = rpc(backend, 'sendtoaddress', rpc(sender, 'newaddr', 'bech32')['bech32'], '0.02')
        mine(backend, 1)
        wait_until(lambda: any(o['txid'] == txid and o['status'] == 'confirmed'
                              for o in rpc(sender, 'listfunds')['outputs']), sender['proc'])
        rpc(sender, 'connect', recipient['id'], '127.0.0.1', recipient['port'])
        funding = lab.rpc([*sender['cli'], '-k'], 'fundchannel', 'id='+recipient['id'],
                          'amount=1000000sat', 'announce='+('false' if private else 'true'))
        wait_until(lambda: funding['txid'] in rpc(backend, 'getrawmempool'))
        mine(backend, 6)
        for left, right in ((sender, recipient), (recipient, sender)):
            wait_until(lambda: channel(left, right)['state'] == 'CHANNELD_NORMAL', left['proc'])

    for p in payers:
        fund(xbt, p, xbt_relay if routed_xbt and p is payer else incoming)
    if routed_xbt:
        fund(xbt, xbt_relay, incoming)
        rpc(xbt_relay, 'setchannel', channel(xbt_relay, incoming)['short_channel_id'], '5000msat', 0)
        wait_until(lambda: channel(incoming, xbt_relay).get('updates', {}).get('remote', {}).get('fee_base_msat') == 5000
                   and channel(incoming, xbt_relay)['updates']['remote']['fee_proportional_millionths'] == 0, incoming['proc'])
        assert not any(c['peer_id'] == incoming['id'] for c in rpc(payer,'listpeerchannels')['channels'])
        assert not any(c['peer_id'] == payer['id'] for c in rpc(incoming,'listpeerchannels')['channels'])
        print('PASS: XBT payer has no direct coordinator channel; intermediate XBT relay charges 5 sats',flush=True)
    fund(btc, outgoing, relay)
    fund(btc, relay, receiver, private=private_hint)
    rpc(relay, 'setchannel', channel(relay, receiver)['short_channel_id'], '5000msat', 0)
    if any(c['peer_id'] == receiver['id'] for c in rpc(outgoing, 'listpeerchannels')['channels']):
        raise AssertionError('unexpected direct operator-to-receiver channel')
    # Incoming operator needs its own confirmed on-chain recovery reserve.
    reserve_tx = rpc(xbt, 'sendtoaddress', rpc(incoming, 'newaddr', 'bech32')['bech32'], '0.001')
    mine(xbt, 1)
    wait_until(lambda: any(o['txid'] == reserve_tx and o['status'] == 'confirmed'
                          for o in rpc(incoming, 'listfunds')['outputs']), incoming['proc'])
    initial = {(n['id'], c['short_channel_id']): c['to_us_msat'] for n in nodes
               for c in rpc(n, 'listpeerchannels')['channels']}
    print('PASS: funded XBT channel and two-hop BTC path; no direct BTC receiver channel', flush=True)
    if private_hint:
        wait_until(lambda: channel(receiver, relay).get('updates', {}).get('remote', {}).get('fee_base_msat') == 5000
                   and channel(receiver, relay)['updates']['remote']['fee_proportional_millionths'] == 0,
                   receiver['proc'])
        if not channel(receiver, relay)['private']:
            raise AssertionError('final BTC channel must be unannounced')
        private_scid = channel(relay, receiver)['short_channel_id']
        if rpc(outgoing, 'listchannels', private_scid)['channels']:
            raise AssertionError('operator unexpectedly knows private channel in gossip')
    invoice = lab.rpc([*receiver['cli'], '-k'], 'invoice', 'amount_msat=1500000msat',
                      'label=service-reverse', 'description=Service reverse regtest',
                      'exposeprivatechannels=true', 'cltv=144')
    settings = dict(reverse_profile=SERVICE_REGTEST, deployment='operator-pair-v1', btc_cli=outgoing['cli'],
                    xbt_cli=incoming['cli'],
                    node_ids=[outgoing['id'], incoming['id']], receiver_id=payer['id'],
                    swap_root=str(lab.root))
    if any_xbt:
        settings.pop('receiver_id')
        settings['reverse_incoming_policy'] = 'any-normal-v1'
    settings_path = lab.root/'service-settings.json'
    save(settings_path, settings)
    directory = lab.root/'swap'

    def market(kind):
        if kind == 'ticker':
            return dict(success=True, pair='BTCB2_BTC', ticker=dict(
                computedAt=int(time.time()*1000), bestBid='0.0043066', bestAsk='0.00431'))
        return dict(success=True, pair='BTCB2_BTC', bids=[dict(price='0.0043066', quantity='1')])

    def operator_rpc(cli, *args):
        if any(list(cli[:len(p['cli'])]) == p['cli'] for p in payers):
            raise AssertionError('quote creation contacted customer wallet RPC')
        return lab.rpc(cli, *args)

    def inspect(*args, **kwargs):
        return check(*args, **kwargs, market_fetch=market)

    # Waiting for gossip is read-only. Once create begins its directory must
    # never be retried blindly if registration or signing has a lost reply.
    def ready():
        result = inspect(invoice['bolt11'], dict(btc=outgoing['cli'],
                         operator=incoming['cli']), rpc=operator_rpc,
                         **({'incoming_policy':'any-normal-v1'} if any_xbt else {'payer_id':payer['id']}),
                         max_routing_fee_sats=30, max_delay=576, _service_regtest=True)
        return result if result.get('route_found') else None
    summary = wait_until(ready, outgoing['proc'], timeout=90)
    if summary['btc_outgoing_cltv'] != 448:
        raise AssertionError('expected 448-block route from private final hint')
    def creator(settings, value, destination, inspector):
        return create(settings, value, destination, rpc=operator_rpc, inspector=inspector)
    api = Quotes(settings, creator=creator, inspector=inspect, auto_process=auto_process)
    request_dir = lab.root/'customer-request'
    credential = dict(token='ab'*32, **({'scope':'reverse'} if any_xbt else {'payer_id':payer['id']}))
    with Server(lab.port(), api, credential) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(2):
                request_quote(invoice['bolt11'], credential, f'http://127.0.0.1:{server.server_port}',
                              request_dir, 500000)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    request_id = json.loads((request_dir/'request.json').read_text())['request_id']
    directory = lab.root/('api-'+request_id)
    quote = json.loads((directory/'reverse-quote.json').read_text())
    print('PASS: repeated customer API request returned the original offer; no payment or duplicate quote', flush=True)
    if quote['inspection']['payer_rpc_checked'] or quote['inspection']['xbt_payer_spendable_sats'] is not None:
        raise AssertionError('operator-only quote claimed customer wallet inspection')
    print('PASS: service quote uses only operator RPCs; customer wallet RPC never accessed', flush=True)
    terms = quote['terms']
    if any_xbt:
        assert 'payer_id' not in terms and 'xbt_channel' not in terms
        assert 'payer_id' not in quote['config']
        print('PASS: quote contains no payer identity or incoming XBT channel; both channels eligible', flush=True)
    if (terms['timing']['minimum_xbt_remaining_blocks'] != 598
            or terms['timing']['proposed_xbt_invoice_cltv'] != 622
            or terms['route'][0]['amount_msat'] != 1505000
            or terms['route'][-1]['id'] != receiver['id']):
        raise AssertionError('service quote changed route, fee, recipient or timing')
    if any(p['payment_hash'] == terms['payment_hash']
           for p in rpc(outgoing, 'listsendpays')['payments']):
        raise AssertionError('quote creation attempted a BTC payment')
    print('PASS: real service registered and signed XBT quote; 448-block BTC route, 622-block XBT invoice; no BTC spend', flush=True)
    pay_log = lab.root/'service-xbt-pay.log'
    offer_path = request_dir/'offer.json'
    customer_dir = lab.root/'customer-payment'
    if routed_xbt:
        from routed_xbt_regtest import wait_for_route, receipt as routed_receipt
        wait_for_route(lab, payer, xbt_relay, incoming, terms)
    if routed_xbt and not customer_routed_xbt:
        paying = lab.start([*payer['cli'], '-k', 'pay', 'bolt11='+quote['xbt_invoice'],
                            'maxfee=10000msat', 'maxdelay=2016', 'retry_for=0'], pay_log)
        print('PASS: ordinary XBT pay uses a two-hop route; customer helper direct-channel restriction is not relaxed',flush=True)
    else:
        customer.review(json.loads(offer_path.read_text()), invoice['bolt11'], payer['cli'],
                        customer_dir, 500000, rpc=lab.rpc, network='xbt-regtest',
                        **({'max_xbt_routing_fee_sats': 10} if customer_routed_xbt else {}))
        client = lab.root/'customer-pay.py'
        client.write_text('import sys,json\nfrom pathlib import Path\n'+
            f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'+
            'from reverse_customer import pay\n'+
            f'print(json.dumps(pay(Path({str(customer_dir)!r}))))\n')
        paying = lab.start([sys.executable, str(client)], pay_log)
        print('PASS: customer reviewed minimal offer and submitted using its own wallet only', flush=True)
        if customer_routed_xbt:
            assert json.loads((customer_dir/'customer.json').read_text())['max_xbt_routing_fee_sats'] == 10
            print('PASS: customer helper pinned 10-sat XBT routing cap; no direct coordinator channel required',flush=True)
    payment_hash = terms['payment_hash']

    def committed(node, peer, direction):
        wanted = 'RCVD_ADD_ACK_REVOCATION' if direction == 'in' else 'SENT_ADD_ACK_REVOCATION'
        return any(h['payment_hash'] == payment_hash and h['direction'] == direction
                   and h['state'] == wanted for h in channel(node, peer).get('htlcs', []))
    wait_until(lambda: rpc(incoming, 'reverse-status', payment_hash)['hook_ready'], incoming['proc'])
    wait_until(lambda: committed(incoming, incoming_peer, 'in'), incoming['proc'])
    wait_until(lambda: committed(payer, payer_peer, 'out'), payer['proc'])
    state_path = directory/'reverse-state.json'
    command = [sys.executable, str(Path(__file__).with_name('reverse_service.py'))]

    def service(mode):
        proc = subprocess.run([*command, mode, '--directory', str(directory)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode:
            raise AssertionError('service command failed; inspect retained regtest logs: '+proc.stdout)
        return json.loads(proc.stdout)

    def background():
        worker = lab.root/'recovery-tick.py'
        worker.write_text('import sys,json\nfrom pathlib import Path\n'+
            f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'+
            'from service_runtime import tick\n'+
            f'print(json.dumps(tick(json.loads(Path({str(settings_path)!r}).read_text()))))\n')
        result = subprocess.run([sys.executable, str(worker)], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError('background worker failed')
        health = json.loads(result.stdout)
        if (not health.get('operators_ready') or not health.get('nodes_ready')
                or any(s.get('outcome') == 'needs_inspection' for s in health['swaps'])):
            raise AssertionError('background worker could not process service payment')
        return health

    if service('recover') != {'outcome': 'needs_manual_start'} or state_path.exists():
        raise AssertionError('recovery started an unsubmitted payment')
    print('PASS: incoming XBT committed; recovery-only process did not originate BTC or create state', flush=True)

    if abort_unspent:
        # Force the service preflight to refuse an already-admitted quote.
        # Advance only XBT until the original HTLC margin is insufficient.
        status = rpc(incoming, 'reverse-status', payment_hash)
        height = rpc(incoming, 'getinfo')['blockheight']
        mine(xbt, status['cltv_expiry']-height-terms['min_cltv_delta']+1)
        refused = subprocess.run([*command, 'tick', '--directory', str(directory)],
                                 capture_output=True, text=True, timeout=60)
        if refused.returncode != 1 or json.loads(state_path.read_text())['phase'] != 'prepared':
            raise AssertionError('stale pre-spend timing was not refused')
        if rpc(outgoing, 'listsendpays')['payments']:
            raise AssertionError('stale margin spent BTC')
        if service('abort-unspent').get('phase') != 'xbt_failed':
            raise AssertionError('unspent cancellation did not finish')
        if service('recover').get('phase') != 'xbt_failed':
            raise AssertionError('unspent cancellation not repeatable')
        print('PASS: stale margin blocked service submission; abort-unspent returned original XBT; no BTC attempt', flush=True)
    else:
        if auto_process:
            background()
            if not state_path.exists() or json.loads(state_path.read_text())['phase'] != 'outgoing_started':
                raise AssertionError('authorized background worker did not start original BTC attempt')
            print('PASS: authorized quote processed by background worker; no manual service start', flush=True)
        elif service('tick').get('outcome') != 'pending':
            raise AssertionError('service did not submit original pending attempt')
        wait_until(lambda: committed(outgoing, relay, 'out'), outgoing['proc'])
        wait_until(lambda: committed(receiver, relay, 'in'), receiver['proc'])
        checkpoint = json.loads(state_path.read_text())
        if checkpoint['phase'] != 'outgoing_started' or 'preimage' in checkpoint:
            raise AssertionError('unexpected pending checkpoint')
        attempts = rpc(outgoing, 'listsendpays')['payments']
        if len(attempts) != 1 or attempts[0]['status'] != 'pending':
            raise AssertionError('expected exactly one pending BTC attempt')
        attempt_id = tuple(attempts[0].get(k) for k in ('id', 'groupid', 'partid'))
        if any_xbt:
            pin = checkpoint['incoming_channel']
            assert pin['peer_id'] == incoming_peer['id']
            assert pin['funding_txid'] == channel(incoming,incoming_peer)['funding_txid']
            assert checkpoint['xbt_binding'][0] == channel(incoming,incoming_peer)['short_channel_id']
        gate_before = (lab.root/'service-gate.json').read_bytes()
        for node in (incoming, outgoing):
            lab.stop(node['proc'])
            node['log'].rename(node['log'].with_name('before-service-restart.log'))
        for node, name, network, backend, plugins in (
                (incoming, 'xbt-operator', 'xbt-regtest', xbt, (gate,)),
                (outgoing, 'btc-operator', 'regtest', btc, ())):
            restarted = lab.lightning(name, network, backend, plugins=plugins)
            if restarted['id'] != node['id']:
                raise AssertionError('operator identity changed on restart')
            node.update(restarted)
        for p in payers:
            if not routed_xbt or p is not payer:
                rpc(p, 'connect', incoming['id'], '127.0.0.1', incoming['port'])
        if routed_xbt:
            rpc(xbt_relay, 'connect', incoming['id'], '127.0.0.1', incoming['port'])
        rpc(outgoing, 'connect', relay['id'], '127.0.0.1', relay['port'])
        wait_until(lambda: rpc(incoming, 'reverse-status', payment_hash)['hook_ready'], incoming['proc'])
        wait_until(lambda: committed(incoming, incoming_peer, 'in'), incoming['proc'])
        wait_until(lambda: committed(outgoing, relay, 'out'), outgoing['proc'])
        if (lab.root/'service-gate.json').read_bytes() != gate_before:
            raise AssertionError('gate replay changed immutable quote or binding')
        for _ in range(2):
            if service('recover').get('outcome') != 'pending':
                raise AssertionError('fresh recovery did not preserve pending attempt')
        print('PASS: operators restarted with service payment pending; fresh recovery processes preserved original attempt and gate binding', flush=True)
        method, field = ('xbt-fail', 'failed') if fail_outgoing else ('xbt-continue', 'continued')
        if rpc(receiver, method, payment_hash)[field] != 1:
            raise AssertionError('receiver hook outcome not triggered')
        terminal = 'failed' if fail_outgoing else 'complete'
        wait_until(lambda: rpc(outgoing, 'listsendpays')['payments'][0]['status'] == terminal, outgoing['proc'])
        for _ in range(2):
            background()
        expected = 'xbt_failed' if fail_outgoing else 'xbt_released'
        if json.loads(state_path.read_text())['phase'] != expected or service('recover').get('phase') != expected:
            raise AssertionError('background recovery did not finish original payment')
        attempts = rpc(outgoing, 'listsendpays')['payments']
        if len(attempts) != 1 or tuple(attempts[0].get(k) for k in ('id', 'groupid', 'partid')) != attempt_id:
            raise AssertionError('service recovery changed or repeated BTC attempt')
        print('PASS: background service reconciled terminal BTC outcome; repeated recovery safe; original BTC attempt only', flush=True)

    failed = fail_outgoing or abort_unspent
    paying.wait(timeout=40)
    if routed_xbt and not customer_routed_xbt:
        assert (paying.returncode != 0) == failed
        rows = rpc(payer, 'listpays', quote['xbt_invoice'])['pays']
        receipt = routed_receipt(rows, quote, incoming['id'], failed=failed)
        assert routed_receipt(rpc(payer, 'listpays', quote['xbt_invoice'])['pays'],
                              quote, incoming['id'], failed=failed) == receipt
    else:
        if paying.returncode:
            raise AssertionError('customer command failed')
        customer_state = json.loads((customer_dir/'customer.json').read_text())
        wait_until(lambda: customer.result(customer_state, rpc=lab.rpc)['outcome'] == ('failed' if failed else 'complete'), payer['proc'])
        receipt = customer.result(customer_state, rpc=lab.rpc)
        if receipt['outcome'] != ('failed' if failed else 'complete'):
            raise AssertionError('wrong customer outcome')
        if customer.pay(customer_dir, rpc=lab.rpc) != receipt:
            raise AssertionError('repeated customer pay did not reconcile the original outcome')
    deltas = {key: 0 for key in initial}
    if not failed:
        incoming_legs = ([(payer, xbt_relay, terms['xbt_amount_msat']+5000),
                          (xbt_relay, incoming, terms['xbt_amount_msat'])] if routed_xbt else
                         [(payer, incoming, terms['xbt_amount_msat'])])
        for left, right, amount in [*incoming_legs, (outgoing, relay, 1505000), (relay, receiver, 1500000)]:
            scid = channel(left, right)['short_channel_id']
            deltas[(left['id'], scid)] -= amount
            deltas[(right['id'], scid)] += amount
        if receipt['xbt_sent_sats']*1000 != terms['xbt_amount_msat']+(5000 if routed_xbt else 0) or not receipt['matching_preimage_verified']:
            raise AssertionError('payer receipt differs from service quote')
    for node in nodes:
        def settled():
            channels = rpc(node, 'listpeerchannels')['channels']
            return len(channels) == sum(k[0] == node['id'] for k in initial) and all(c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                       and c['to_us_msat'] == initial[(node['id'], c['short_channel_id'])]
                       + deltas[(node['id'], c['short_channel_id'])] for c in channels)
        wait_until(settled, node['proc'])
    received = rpc(receiver, 'listinvoices', 'service-reverse')['invoices'][0]
    if received['status'] != ('unpaid' if failed else 'paid'):
        raise AssertionError('receiver invoice state differs')
    if not failed and received['amount_received_msat'] != 1500000:
        raise AssertionError('receiver amount differs')
    print('PASS: all channel-side balances and invoice outcomes verified; no pending HTLCs', flush=True)
    if any_xbt:
        final_state = json.loads(state_path.read_text())
        assert final_state['incoming_channel']['peer_id'] == incoming_peer['id']
        if routed_xbt:
            assert final_state['incoming_channel']['peer_id'] != payer['id']
            gain = sum(c['to_us_msat']-initial[(xbt_relay['id'],c['short_channel_id'])]
                       for c in rpc(xbt_relay,'listpeerchannels')['channels'])
            assert gain == (0 if failed else 5000)
            print('PASS: coordinator pinned relay-facing HTLC; XBT relay earned '+str(gain//1000)+' sats; payer remained unbound',flush=True)
        print('PASS: unbound reverse quote used payer '+('2' if second_payer else '1')+
              '; original incoming pin retained; unused XBT channel balance unchanged',flush=True)
    mode = 'unspent cancellation' if abort_unspent else 'BTC rejection' if failed else 'success'
    print(f'Reverse service workflow OK ({mode}; real regtest nodes; deterministic market fixture; live activation disabled)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--fail-outgoing', action='store_true')
    modes.add_argument('--abort-unspent', action='store_true')
    parser.add_argument('--auto-process', action='store_true')
    parser.add_argument('--any-xbt', action='store_true')
    parser.add_argument('--second-payer', action='store_true')
    parser.add_argument('--customer-routed-xbt', action='store_true',
                        help='Use customer review/pay/recovery over the routed XBT fixture.')
    parser.add_argument('--routed-xbt', action='store_true', help='Ordinary XBT pay through an intermediate relay; implies --any-xbt.')
    parser.add_argument('--work-dir', type=Path)
    args = parser.parse_args()
    if args.auto_process and args.abort_unspent:
        parser.error('auto-process cancellation is covered by authorization unit tests')
    if args.customer_routed_xbt:
        args.routed_xbt = True
    if args.routed_xbt:
        args.any_xbt = True
    if args.second_payer and not args.any_xbt:
        parser.error('--second-payer requires --any-xbt')
    temp = None
    if args.work_dir:
        root = args.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temp = tempfile.TemporaryDirectory(prefix='cln-reverse-service-')
        root = Path(temp.name)
    lab = ServiceLab(root, str(args.bitcoind.resolve()), str(args.bitcoin_cli.resolve()))
    print(f'Test directory: {root}', flush=True)
    try:
        run(lab, args.fail_outgoing, args.abort_unspent, args.auto_process, args.any_xbt, args.second_payer, args.routed_xbt, args.customer_routed_xbt)
    finally:
        lab.close()
        if temp:
            temp.cleanup()


if __name__ == '__main__':
    main()
