"""Drive reverse controller crashes and optional durable-gate operator restarts."""
import json
from pathlib import Path
import subprocess
import sys

from smoke_regtest import wait_until
from swap_controller import save


def exercise(lab, payer, incoming, outgoing, receiver, invoice, decoded, route, mode, gate=None):
    payment_hash = invoice['payment_hash']
    failed = mode in ('pending-failure', 'gate-restart-failure', 'gate-failure')

    def rpc(node, *args):
        return lab.rpc(node['cli'], *args)

    def channel(node):
        channels = rpc(node, 'listpeerchannels')['channels']
        if len(channels) != 1:
            raise AssertionError('expected one fixture channel')
        return channels[0]

    def payment(node):
        records = [p for p in rpc(node, 'listsendpays')['payments']
                   if p['payment_hash'] == payment_hash]
        if len(records) != 1:
            raise AssertionError('expected exactly one original attempt')
        return records[0]

    def committed(node, direction):
        state = 'SENT_ADD_ACK_REVOCATION' if direction == 'out' else 'RCVD_ADD_ACK_REVOCATION'
        return any(h['payment_hash'] == payment_hash and h['state'] == state
                   for h in channel(node).get('htlcs', []))

    c = channel(incoming)
    hooks = [h for h in c.get('htlcs', []) if h['payment_hash'] == payment_hash and h['direction'] == 'in']
    if len(hooks) != 1:
        raise AssertionError('expected original committed XBT HTLC')
    h = hooks[0]
    path = lab.root/'reverse-state.json'
    state = dict(profile='reverse-regtest-v1', phase='prepared',
                    xbt_cli=incoming['cli'], btc_cli=outgoing['cli'],
                    node_ids=[incoming['id'], outgoing['id']], payment_hash=payment_hash,
                    xbt_binding=[c['short_channel_id'], h['id']], xbt_expiry=h['expiry'],
                    xbt_amount_msat=200000000, btc_amount_msat=100000000,
                    btc_invoice=invoice['bolt11'], btc_secret=decoded['payment_secret'], route=route)
    if gate:
        state.update(durable_gate=True, reverse_quote=gate['quote'])
        if 'routing' in gate:
            state['routing'] = gate['routing']
    save(path, state)
    command = [sys.executable, str(Path(__file__).with_name('reverse_controller.py')), '--state', str(path)]
    after_btc = mode in ('after-btc', 'gate-release')
    crashed = subprocess.run([*command, '--crash-after-btc' if after_btc else '--crash-after-sendpay'],
                             capture_output=True, text=True, timeout=60)
    if crashed.returncode != (86 if after_btc else 88):
        raise AssertionError('reverse controller did not reach the requested crash point: '+crashed.stdout)
    before = path.read_bytes()
    state = json.loads(before)
    if state['phase'] != 'outgoing_started' or 'preimage' in state:
        raise AssertionError('unexpected submission checkpoint')
    original = payment(outgoing)
    attempt_id = tuple(original.get(k) for k in ('id', 'groupid', 'partid'))
    if mode in ('gate-restart', 'gate-restart-failure'):
        for node, direction in ((payer, 'out'), (incoming, 'in'), (outgoing, 'out'), (receiver, 'in')):
            wait_until(lambda: committed(node, direction), node['proc'])
        gate_before = gate['plugin'].with_suffix('.quotes.json').read_bytes()
        for node in (incoming, outgoing):
            lab.stop(node['proc'])
            node['log'].rename(node['log'].with_name('before-restart.log'))
        for node, name, network, backend, plugins in (
                (incoming, 'xbt-operator', 'xbt-regtest', gate['xbt'], (gate['plugin'],)),
                (outgoing, 'btc-operator', 'regtest', gate['btc'], ())):
            restarted = lab.lightning(name, network, backend, plugins=plugins)
            if restarted['id'] != node['id']:
                raise AssertionError('operator identity changed')
            node.update(restarted)
        rpc(payer, 'connect', incoming['id'], '127.0.0.1', incoming['port'])
        rpc(outgoing, 'connect', receiver['id'], '127.0.0.1', receiver['port'])
        wait_until(lambda: rpc(incoming, 'reverse-status', payment_hash)['hook_ready'], incoming['proc'])
        wait_until(lambda: committed(incoming, 'in'), incoming['proc'])
        wait_until(lambda: committed(outgoing, 'out'), outgoing['proc'])
        wait_until(lambda: channel(incoming)['peer_connected'], incoming['proc'])
        if gate['plugin'].with_suffix('.quotes.json').read_bytes() != gate_before:
            raise AssertionError('quote or accepted HTLC snapshot changed on replay')
        print('PASS: both operators restarted while pending; durable XBT quote and hook replay preserved', flush=True)

    def resume():
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise AssertionError('reverse recovery failed: '+result.stdout)
        return json.loads(result.stdout)

    def xbt_pending():
        p = payment(payer)
        if p['status'] != 'pending' or p.get('payment_preimage') or not committed(incoming, 'in'):
            raise AssertionError('XBT no longer committed and pending')

    xbt_pending()
    if after_btc:
        if original['status'] != 'complete':
            raise AssertionError('BTC did not complete before controller crash')
        print('PASS: controller crashed after BTC settlement; XBT held; disk lacks preimage', flush=True)
    else:
        wait_until(lambda: committed(outgoing, 'out'), outgoing['proc'])
        wait_until(lambda: committed(receiver, 'in'), receiver['proc'])
        print('PASS: controller crashed after submission; original BTC and XBT HTLCs pending', flush=True)
        for _ in range(2):
            if resume() != {'phase': 'outgoing_started', 'outcome': 'pending'}:
                raise AssertionError('pending recovery did not remain pending')
            xbt_pending()
            if path.read_bytes() != before or payment(outgoing)['status'] != 'pending':
                raise AssertionError('pending checkpoint or outgoing attempt changed')
        print('PASS: two fresh controllers preserve pending payments without BTC resend', flush=True)
        method, field = ('xbt-fail', 'failed') if failed else ('xbt-continue', 'continued')
        if rpc(receiver, method, payment_hash)[field] != 1:
            raise AssertionError('expected one receiver hook transition')
        terminal = 'failed' if failed else 'complete'
        wait_until(lambda: payment(outgoing)['status'] == terminal, outgoing['proc'])
        xbt_pending()
    phase = 'xbt_failed' if failed else 'xbt_released'
    if mode in ('gate-release', 'gate-failure'):
        crashed = subprocess.run([*command, '--crash-after-xbt-resolution'],
                                 capture_output=True, text=True, timeout=60)
        checkpoint = json.loads(path.read_text())
        status = rpc(incoming, 'reverse-status', payment_hash)
        if (crashed.returncode != 89 or checkpoint['phase'] != ('btc_failed' if failed else 'btc_paid')
                or status['phase'] != ('failed' if failed else 'resolved') or status['hook_ready']):
            raise AssertionError('terminal gate intent crash point not reached')
        print('PASS: controller crashed after durable XBT resolution; controller checkpoint still incomplete', flush=True)
    if resume() != {'phase': phase}:
        raise AssertionError('recovered phase mismatch')
    completed = path.read_bytes()
    if resume() != {'phase': phase} or path.read_bytes() != completed:
        raise AssertionError('terminal recovery changed completed state')
    if tuple(payment(outgoing).get(k) for k in ('id', 'groupid', 'partid')) != attempt_id:
        raise AssertionError('outgoing attempt identity changed')
    if gate:
        status = rpc(incoming, 'reverse-status', payment_hash)
        if status['terms'] != gate['quote'] or status['binding'] != state['xbt_binding']:
            raise AssertionError('terminal reverse quote or binding changed')
    print('PASS: fresh controller recovered terminal BTC outcome; repeated recovery safe; one BTC attempt', flush=True)
