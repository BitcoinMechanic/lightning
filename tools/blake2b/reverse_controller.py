"""Controller-crash recovery fixture for a single direct XBT -> BTC regtest swap.

Legacy holding-plugin fixtures require CLN to stay running. Durable-gate
fixtures also reconcile operator restarts and terminal XBT resolution intent.
Opt-in on-chain claims release the same bound hook after BTC completion.
An opt-in regtest deadline guard can close the pinned incoming XBT channel.
Live-profile integration is explicitly activation-gated in reverse_live.py.
Unknown outcomes never authorize a resend or an incoming HTLC failure.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from reverse_metadata import invoice_metadata
import reverse_live as live
from swap_controller import save
from swap_rpc import RPC


PHASES = {'prepared', 'outgoing_started', 'btc_paid', 'btc_failed',
          'xbt_released', 'xbt_failed'}


def identity(state):
    if live.is_service(state):
        if state.get('phase') not in PHASES:
            raise RuntimeError('unsupported live reverse phase')
        live.verify_state(state, RPC.call)
        return
    if (state.get('profile') != 'reverse-regtest-v1' or state.get('phase') not in PHASES
            or state.get('xbt_amount_msat') != 200000000
            or state.get('btc_amount_msat') != 100000000):
        raise RuntimeError('unsupported reverse fixture state')
    ids = []
    for key, network in (('xbt_cli', 'xbt-regtest'), ('btc_cli', 'regtest')):
        info = RPC.call(state[key], 'getinfo')
        if info['network'] != network:
            raise RuntimeError('reverse controller requires disposable regtest nodes')
        ids.append(info['id'])
    if ids != state['node_ids'] or ids[0] == ids[1]:
        raise RuntimeError('operator identity mismatch')
    route = state['route']
    if 'routing' in state:
        from reverse_route import validate
        validate(route, state['btc_amount_msat'], state['routing'])
        if not state.get('durable_gate') or state['routing']['source'] != ids[1]:
            raise RuntimeError('routed reverse swap requires bound operator and durable gate')
    elif (len(route) != 1 or route[0]['amount_msat'] != state['btc_amount_msat']
            or route[0]['delay'] != 40):
        raise RuntimeError('unsupported reverse route')


def incoming(state, *, allow_onchain=False):
    """Require the exact original, committed incoming HTLC and holding hook."""
    scid, htlc_id = state['xbt_binding']
    channels = [c for c in RPC.call(state['xbt_cli'], 'listpeerchannels')['channels']
                if c.get('short_channel_id') == scid]
    if len(channels) != 1:
        raise RuntimeError('bound incoming channel unavailable')
    channel = channels[0]
    if channel['state'] == 'ONCHAIN' and allow_onchain:
        pin = state.get('incoming_channel')
        keys = ('channel_id', 'funding_txid', 'funding_outnum', 'peer_id', 'short_channel_id')
        if (state.get('xbt_onchain_claim') is not True or not state.get('durable_gate')
                or state['phase'] != 'btc_paid' or not isinstance(pin, dict)
                or set(pin) != set(keys) or any(channel.get(k) != pin[k] for k in keys)):
            raise RuntimeError('on-chain incoming channel identity mismatch')
    elif channel['state'] != 'CHANNELD_NORMAL' or not channel['peer_connected']:
        raise RuntimeError('bound incoming channel unavailable')
    htlcs = [h for h in channels[0].get('htlcs', [])
             if h['id'] == htlc_id and h['direction'] == 'in']
    expected = dict(payment_hash=state['payment_hash'], amount_msat=state['xbt_amount_msat'],
                    expiry=state['xbt_expiry'], state='RCVD_ADD_ACK_REVOCATION')
    if len(htlcs) != 1 or any(htlcs[0].get(k) != v for k, v in expected.items()):
        raise RuntimeError('original incoming HTLC unavailable or changed')
    hooks = [h for h in RPC.call(state['xbt_cli'], 'xbt-held')['held']
             if h['payment_hash'] == state['payment_hash']]
    expected = dict(short_channel_id=scid, id=htlc_id, amount_msat=state['xbt_amount_msat'],
                    cltv_expiry=state['xbt_expiry'])
    if len(hooks) != 1 or any(hooks[0].get(k) != v for k, v in expected.items()):
        raise RuntimeError('original holding hook unavailable or changed')
    return channel


def records(state):
    return [p for p in RPC.call(state['btc_cli'], 'listsendpays')['payments']
            if p['payment_hash'] == state['payment_hash']]


def gate_status(state):
    result = RPC.call(state['xbt_cli'], 'reverse-status', state['payment_hash'])
    quote = state['reverse_quote']
    expected = dict(payment_hash=state['payment_hash'], btc_invoice=state['btc_invoice'],
                    btc_amount_msat=state['btc_amount_msat'], xbt_amount_msat=state['xbt_amount_msat'],
                    xbt_channel=state['xbt_binding'][0],
                    min_cltv_delta=quote['timing']['minimum_xbt_remaining_blocks'] if live.is_service(state) else 100,
                    max_cltv_delta=2016 if live.is_service(state) else 2000)
    if (any(quote.get(k) != v for k, v in expected.items())
            or result['payment_hash'] != state['payment_hash']
            or result['terms'] != quote or result['binding'] != state['xbt_binding']
            or result['cltv_expiry'] != state['xbt_expiry']):
        raise RuntimeError('durable reverse gate binding or terms mismatch')
    return result


def preflight(state):
    if state.get('xbt_deadline_guard') and (state['xbt_deadline_guard'] is not True
            or state.get('xbt_onchain_claim') is not True or not state.get('durable_gate')):
        raise RuntimeError('reverse deadline requires opt-in on-chain recovery')
    channel = incoming(state)
    if state.get('durable_gate'):
        gate = gate_status(state)
        if (gate['phase'] != 'held' or not gate['hook_ready']
                or gate['terms']['expires_at'] <= int(time.time())):
            raise RuntimeError('durable reverse quote not eligible for spending')
    height = RPC.call(state['xbt_cli'], 'getinfo')['blockheight']
    # Remaining block counts are a controlled-regtest margin, not a live
    # guarantee about the relative progress of independent chains.
    minimum = (state['reverse_quote']['min_cltv_delta'] if live.is_service(state)
               else max(100, state['route'][0]['delay'] + 60))
    if not minimum <= state['xbt_expiry'] - height <= (2016 if live.is_service(state) else 2000):
        raise RuntimeError('incoming regtest margin outside fixture bounds')
    decoded = RPC.call(state['btc_cli'], 'decode', state['btc_invoice'])
    expected = dict(valid=True, type='bolt11 invoice', currency=live.networks(state['profile'])['btc_currency'] if live.is_service(state) else 'bcrt',
                    payment_hash=state['payment_hash'], payment_secret=state['btc_secret'],
                    payee=state['route'][-1]['id'], amount_msat=state['btc_amount_msat'])
    if (any(decoded.get(k) != v for k, v in expected.items())
            or decoded['created_at'] + decoded['expiry'] <= int(time.time())
            or not 0 < decoded['min_final_cltv_expiry'] <= state['route'][-1]['delay']):
        raise RuntimeError('BTC invoice binding or expiry mismatch')
    metadata = invoice_metadata(decoded)
    if ('btc_payment_metadata' in state
            and state['btc_payment_metadata'] != metadata):
        raise RuntimeError('BTC invoice metadata binding mismatch')
    # Persist with outgoing_started before sendpay. None and empty bytes are
    # distinct: empty metadata still requires the explicit sendpay argument.
    state['btc_payment_metadata'] = metadata
    route = state['route'][0]
    channels = [c for c in RPC.call(state['btc_cli'], 'listpeerchannels')['channels']
                if route['channel'] in (c.get('short_channel_id'), c.get('alias', {}).get('local'))
                and c['peer_id'] == route['id']]
    if (len(channels) != 1 or channels[0]['state'] != 'CHANNELD_NORMAL'
            or not channels[0]['peer_connected'] or channels[0].get('htlcs')
            or channels[0]['spendable_msat'] < route['amount_msat']):
        raise RuntimeError('BTC first-hop channel unavailable')
    if live.is_service(state):
        live.preflight(state, decoded, channel, channels[0], RPC.call)
    if records(state):
        raise RuntimeError('an outgoing attempt already exists')
    if state.get('xbt_onchain_claim'):
        if state['xbt_onchain_claim'] is not True or not state.get('durable_gate'):
            raise RuntimeError('on-chain claim requires a durable reverse gate')
        # Captured from the verified normal channel, before BTC submission.
        state['incoming_channel'] = {k: channel[k] for k in
            ('channel_id', 'funding_txid', 'funding_outnum', 'peer_id', 'short_channel_id')}


def outcome(state):
    payments = records(state)
    if len(payments) != 1:
        raise RuntimeError('outgoing outcome ambiguous; no resend or XBT resolution')
    p = payments[0]
    expected = dict(amount_msat=state['btc_amount_msat'],
                    amount_sent_msat=state['route'][0]['amount_msat'],
                    destination=state['route'][-1]['id'], bolt11=state['btc_invoice'])
    if any(p.get(k) != v for k, v in expected.items()):
        raise RuntimeError('outgoing attempt binding differs')
    status = p['status']
    if status in ('pending', 'failed') and not p.get('payment_preimage'):
        return status, None
    if status == 'complete':
        preimage = p.get('payment_preimage')
        try:
            raw = bytes.fromhex(preimage)
        except (ValueError, TypeError):
            raise RuntimeError('invalid outgoing preimage') from None
        if len(raw) == 32 and hashlib.sha256(raw).hexdigest() == state['payment_hash']:
            return status, preimage
    raise RuntimeError('outgoing outcome inconsistent; XBT remains unresolved')


def run(path, crash_after_sendpay=False, crash_after_btc=False, crash_after_xbt_resolution=False,
        recover_only=False):
    path = path.resolve()
    fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'outcome': 'busy'}
        state = json.loads(path.read_text())
        identity(state)
        if state['phase'] in ('xbt_released', 'xbt_failed'):
            return {'phase': state['phase']}
        if state['phase'] == 'prepared':
            if recover_only:
                return {'phase': 'prepared', 'outcome': 'needs_manual_start'}
            preflight(state)
            state['phase'] = 'outgoing_started'
            save(path, state)  # Before the mutation, including a lost RPC reply.
            metadata_args = ([] if state['btc_payment_metadata'] is None else
                             ['payment_metadata='+state['btc_payment_metadata']])
            RPC.call([*state['btc_cli'], '-k'], 'sendpay',
                     'route='+json.dumps(state['route']),
                     'payment_hash='+state['payment_hash'],
                     'payment_secret='+state['btc_secret'], 'bolt11='+state['btc_invoice'],
                     *metadata_args)
            if crash_after_sendpay:
                os._exit(88)
            if crash_after_btc:
                RPC.call(state['btc_cli'], 'waitsendpay', state['payment_hash'], 10)
                status, _ = outcome(state)
                if status != 'complete':
                    raise RuntimeError('BTC completion crash point not reached')
                os._exit(86)  # No preimage or completion checkpoint on disk.
        if state['phase'] == 'outgoing_started':
            status, preimage = outcome(state)
            if status == 'pending':
                if live.is_service(state):
                    live.monitor_pending(path, state, RPC.call, save)
                if state.get('xbt_deadline_guard'):
                    from reverse_deadline import protect
                    protect(path, state, RPC.call, save, gate_status(state))
                return {'phase': 'outgoing_started', 'outcome': 'pending'}
            state['phase'] = 'btc_paid' if status == 'complete' else 'btc_failed'
            if preimage is not None:
                state['preimage'] = preimage
            save(path, state)
        if state['phase'] == 'btc_paid':
            raw = bytes.fromhex(state['preimage'])
            if len(raw) != 32 or hashlib.sha256(raw).hexdigest() != state['payment_hash']:
                raise RuntimeError('completion checkpoint preimage mismatch')
            if state.get('durable_gate'):
                gate = gate_status(state)
                if gate['phase'] == 'held' and gate['hook_ready']:
                    incoming(state, allow_onchain=True)
                    if RPC.call(state['xbt_cli'], 'reverse-release', state['payment_hash'],
                                json.dumps(state['xbt_binding']), state['preimage']) != {'released': 1}:
                        raise RuntimeError('durable XBT release uncertain')
                elif gate['phase'] != 'resolved':
                    raise RuntimeError('reverse gate cannot reconcile XBT release')
            else:
                incoming(state)
                if RPC.call(state['xbt_cli'], 'xbt-release', state['preimage']) != {'released': 1}:
                    raise RuntimeError('XBT release uncertain; preserve checkpoint for inspection')
            if crash_after_xbt_resolution:
                os._exit(89)
            state['phase'] = 'xbt_released'
        elif state['phase'] == 'btc_failed':
            unspent_abort = live.is_service(state) and state.get('pre_spend_aborted') is True
            if ('preimage' in state or (bool(records(state)) if unspent_abort
                                        else outcome(state)[0] != 'failed')):
                raise RuntimeError('failure checkpoint inconsistent')
            if state.get('durable_gate'):
                gate = gate_status(state)
                if gate['phase'] == 'held' and gate['hook_ready']:
                    incoming(state)
                    if RPC.call(state['xbt_cli'], 'reverse-fail', state['payment_hash'],
                                json.dumps(state['xbt_binding'])) != {'failed': 1}:
                        raise RuntimeError('durable XBT failure uncertain')
                elif gate['phase'] != 'failed':
                    raise RuntimeError('reverse gate cannot reconcile XBT failure')
            else:
                incoming(state)
                if RPC.call(state['xbt_cli'], 'xbt-fail', state['payment_hash']) != {'failed': 1}:
                    raise RuntimeError('XBT failure uncertain; preserve checkpoint for inspection')
            if crash_after_xbt_resolution:
                os._exit(89)
            state['phase'] = 'xbt_failed'
        save(path, state)
        return {'phase': state['phase']}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--crash-after-sendpay', action='store_true')
    mode.add_argument('--crash-after-btc', action='store_true')
    mode.add_argument('--crash-after-xbt-resolution', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.state, args.crash_after_sendpay, args.crash_after_btc,
                             args.crash_after_xbt_resolution)))
        return 0
    except Exception as exc:
        print(json.dumps({'event': 'error', 'error': type(exc).__name__, 'details': 'withheld'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
