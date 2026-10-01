#!/usr/bin/env python3
"""XBT -> BTC through one forwarding node, with bounded fees. Regtest only."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from smoke_regtest import Lab, wait_until
from reverse_recovery import exercise
from reverse_route import no_route, plan
from swap_controller import save
from swap_invoice import unsigned_invoice


def run(lab, fail_outgoing=False, fee_limit=False, private_hint=False):
    btc, xbt = lab.node('knots-btc', False), lab.node('knots-xbt', True)
    gate = lab.root/'reverse_gate.py'
    hold = lab.root/'hold_htlc.py'
    for name in ('reverse_gate.py', 'hold_htlc.py', 'quote_plugin.py'):
        path = lab.root/name
        path.write_text(f'#!{sys.executable}\n'+Path(__file__).with_name(name).read_text())
        path.chmod(0o700)
    payer = lab.lightning('xbt-payer', 'xbt-regtest', xbt)
    incoming = lab.lightning('xbt-operator', 'xbt-regtest', xbt, plugins=(gate,))
    outgoing = lab.lightning('btc-operator', 'regtest', btc)
    relay = lab.lightning('btc-relay', 'regtest', btc)
    receiver = lab.lightning('btc-receiver', 'regtest', btc, plugins=(hold,))
    nodes = (payer, incoming, outgoing, relay, receiver)
    btc_nodes, xbt_nodes = (outgoing, relay, receiver), (payer, incoming)

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

    fund(xbt, payer, incoming)
    fund(btc, outgoing, relay)
    fund(btc, relay, receiver, private=private_hint)
    rpc(relay, 'setchannel', channel(relay, receiver)['short_channel_id'], '5000msat', 0)
    if any(c['peer_id'] == receiver['id'] for c in rpc(outgoing, 'listpeerchannels')['channels']):
        raise AssertionError('unexpected direct operator-to-receiver channel')
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
    invoice = lab.rpc([*receiver['cli'], '-k'], 'invoice', 'amount_msat=100000000msat',
                      'label=routed-reverse', 'description=Routed reverse regtest',
                      'exposeprivatechannels='+('true' if private_hint else 'false'))
    decoded = rpc(outgoing, 'decode', invoice['bolt11'])

    def route_ready():
        route, policy = plan(outgoing['cli'], decoded, outgoing['id'], lab.rpc)
        return (route, policy) if route[0]['amount_msat']-100000000 == 5000 else None

    route, policy = wait_until(route_ready, outgoing['proc'], timeout=90)
    if len(route) != 2 or [h['id'] for h in route] != [relay['id'], receiver['id']]:
        raise AssertionError('planner did not select the two-hop fixture path')
    if private_hint:
        hints = decoded.get('routes', [])
        if not any(len(h) == 1 and h[0]['pubkey'] == relay['id']
                   and h[0]['short_channel_id'] == route[-1]['channel'] for h in hints):
            raise AssertionError('route did not use the invoice private hint')
        # Removing the signed hint must make this isolated receiver unreachable.
        without_hints = dict(decoded, routes=[])
        try:
            plan(outgoing['cli'], without_hints, outgoing['id'], lab.rpc)
        except subprocess.CalledProcessError as error:
            if not no_route(error):
                raise
        else:
            raise AssertionError('private receiver reachable without invoice hint')
        print('PASS: invoice hint supplies unannounced final hop; public prefix plus private tail costs 5 sats', flush=True)
    else:
        print('PASS: getroutes selected one two-hop route; 5-sat fee within 10-sat cap', flush=True)
    payment_hash, secret = decoded['payment_hash'], secrets.token_hex(32)
    quote = dict(payment_hash=payment_hash, payment_secret=secret,
                 xbt_amount_msat=200000000, btc_amount_msat=100000000,
                 btc_invoice=invoice['bolt11'], xbt_channel=channel(incoming, payer)['short_channel_id'],
                 expires_at=int(time.time())+3600, min_cltv_delta=100, max_cltv_delta=2000)
    if rpc(incoming, 'reverse-register', json.dumps(quote)) != {'registered': True}:
        raise AssertionError('reverse quote registration failed')
    unsigned = unsigned_invoice(payment_hash, secret, amount_msat=200000000, currency='xbtrt', final_cltv=120)
    xbt_invoice = rpc(incoming, 'signinvoice', unsigned)['bolt11']
    check = rpc(payer, 'decode', xbt_invoice)
    expected = dict(valid=True, currency='xbtrt', payee=incoming['id'], payment_hash=payment_hash,
                    payment_secret=secret, amount_msat=200000000, min_final_cltv_expiry=120)
    if any(check.get(k) != v for k, v in expected.items()):
        raise AssertionError('signed XBT invoice differs from quote')
    pay_log = lab.root/'xbt-pay.log'
    paying = lab.start([*payer['cli'], 'pay', xbt_invoice], pay_log)

    def held():
        if paying.poll() is not None:
            raise AssertionError('XBT payer exited before admission')
        return rpc(incoming, 'reverse-status', payment_hash)['hook_ready']

    wait_until(held, incoming['proc'])
    for node, peer, committed in ((payer, incoming, 'SENT_ADD_ACK_REVOCATION'),
                                   (incoming, payer, 'RCVD_ADD_ACK_REVOCATION')):
        wait_until(lambda: any(h['payment_hash'] == payment_hash and h['state'] == committed
                              for h in channel(node, peer).get('htlcs', [])), node['proc'])
    print('PASS: validated XBT quote held before any BTC payment attempt', flush=True)
    if fee_limit:
        # Deliberately supply the known 5-sat candidate against a 4.999-sat
        # controller budget: never trust the planner as the sole fee check.
        status = rpc(incoming, 'reverse-status', payment_hash)
        path = lab.root/'reverse-state.json'
        state = dict(profile='reverse-regtest-v1', phase='prepared', durable_gate=True,
                     reverse_quote=quote, xbt_cli=incoming['cli'], btc_cli=outgoing['cli'],
                     node_ids=[incoming['id'], outgoing['id']], payment_hash=payment_hash,
                     xbt_binding=status['binding'], xbt_expiry=status['cltv_expiry'],
                     xbt_amount_msat=200000000, btc_amount_msat=100000000,
                     btc_invoice=invoice['bolt11'], btc_secret=decoded['payment_secret'], route=route,
                     routing=dict(policy, max_fee_msat=4999))
        save(path, state)
        before = path.read_bytes()
        command = [sys.executable, str(Path(__file__).with_name('reverse_controller.py')), '--state', str(path)]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if result.returncode != 1 or path.read_bytes() != before:
                raise AssertionError('over-budget route was not refused without state change')
            attempts = [p for p in rpc(outgoing, 'listsendpays')['payments'] if p['payment_hash'] == payment_hash]
            if attempts or rpc(incoming, 'reverse-status', payment_hash)['phase'] != 'held':
                raise AssertionError('fee refusal spent BTC or resolved XBT')
        if rpc(incoming, 'reverse-fail', payment_hash, json.dumps(status['binding'])) != {'failed': 1}:
            raise AssertionError('harness cleanup failed')
        print('PASS: two controllers refused over-budget route; no BTC attempt; harness returned unspent XBT', flush=True)
    else:
        context = dict(quote=quote, plugin=gate, xbt=xbt, btc=btc, routing=policy)
        exercise(lab, payer, incoming, outgoing, receiver, invoice, decoded, route,
                 'gate-restart-failure' if fail_outgoing else 'gate-restart', context)

    failed = fee_limit or fail_outgoing
    paying.wait(timeout=30)
    if (paying.returncode == 0) == failed:
        raise AssertionError('unexpected XBT payer outcome')
    deltas = {key: 0 for key in initial}
    if not failed:
        for left, right, amount in ((payer, incoming, 200000000), (outgoing, relay, 100005000),
                                    (relay, receiver, 100000000)):
            scid = channel(left, right)['short_channel_id']
            deltas[(left['id'], scid)] -= amount
            deltas[(right['id'], scid)] += amount
        paid = json.loads(pay_log.read_text())
        if paid['status'] != 'complete' or paid['amount_sent_msat'] != 200000000:
            raise AssertionError('XBT payer receipt differs')
    for node in nodes:
        def settled():
            channels = rpc(node, 'listpeerchannels')['channels']
            return len(channels) == sum(key[0] == node['id'] for key in initial) and all(c['state'] == 'CHANNELD_NORMAL' and not c.get('htlcs')
                       and c['to_us_msat'] == initial[(node['id'], c['short_channel_id'])]
                       + deltas[(node['id'], c['short_channel_id'])] for c in channels)
        wait_until(settled, node['proc'])
    received = rpc(receiver, 'listinvoices', 'routed-reverse')['invoices'][0]
    if received['status'] != ('unpaid' if failed else 'paid'):
        raise AssertionError('BTC invoice outcome differs')
    if not failed and received['amount_received_msat'] != 100000000:
        raise AssertionError('BTC receiver amount differs')
    print('PASS: all six channel-side balances verified; no pending HTLCs'+
          ('; relay earned exactly 5 sats' if not failed else '; balances restored'), flush=True)
    mode = 'fee refusal' if fee_limit else 'BTC rejection' if fail_outgoing else 'success'
    path_kind = 'private invoice hint' if private_hint else 'public path'
    print(f'Routed XBT -> BTC swap test OK ({mode}; {path_kind}; single part; regtest only)', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bitcoind', required=True, type=Path)
    parser.add_argument('--bitcoin-cli', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fail-outgoing', action='store_true')
    mode.add_argument('--fee-limit', action='store_true')
    parser.add_argument('--private-hint', action='store_true', help='Unannounced final BTC channel via BOLT11 hint')
    parser.add_argument('--work-dir', type=Path)
    args = parser.parse_args()
    temporary = None
    if args.work_dir:
        root = args.work_dir.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    else:
        temporary = tempfile.TemporaryDirectory(prefix='cln-rr-')
        root = Path(temporary.name)
    lab = Lab(root, str(args.bitcoind.resolve()), str(args.bitcoin_cli.resolve()))
    print(f'Test directory: {root}', flush=True)
    try:
        run(lab, args.fail_outgoing, args.fee_limit, args.private_hint)
    finally:
        lab.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
