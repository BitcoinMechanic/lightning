"""Single-swap regtest controller crash fixture, not a production service.

Unknown outgoing outcomes fail closed. Quote-backed swaps can reconcile a
durable BTC release after losing its controller checkpoint. btc_released means
release recorded, not independently verified end-to-end BTC settlement.
Likewise btc_failed records failure intent; the harness verifies payer failure
and restored balances separately. Only definitive single-attempt failure is
handled; pending, missing and ambiguous outcomes never authorize BTC failure.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from swap_rpc import RPC, wait_until
from deadline_guard import protect
import live_pilot as pilot


def save(path, state):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def check_spend(state):
    info = RPC.call(state['btc_cli'], 'xbt-spend-info', state['payment_hash'])
    if (info['payment_hash'] != state['payment_hash']
            or info['binding'] != state['btc_binding']
            or info['xbt_amount_msat'] != state['xbt_amount_msat']):
        raise RuntimeError('spend quote identity or amount mismatch')
    if pilot.is_live(state):
        if (info.get('pilot') != state['profile'] or info.get('btc_amount_msat') != pilot.amounts(state)[0]
                or info['min_cltv_delta'] != pilot.MIN_CLTV
                or info['max_cltv_delta'] != pilot.MAX_CLTV):
            raise RuntimeError('live quote policy mismatch')
        if (state['profile'] in (pilot.PROFILE_V2, pilot.PROFILE_MARKET)
                and info.get('btc_channel') != state.get('btc_channel')):
            raise RuntimeError('live quote incoming channel mismatch')
        if state['profile'] in pilot.MARKET_PROFILES:
            if (info.get('oracle_digest') != state['oracle_digest']
                    or info.get('controller_id') != state['controller_id']):
                raise RuntimeError('market quote audit or controller mismatch')
        pilot.require_reserves(state, RPC.call)
        pilot.check_channels(state, RPC.call)
    from incoming_btc import enabled, check_spend as check_incoming
    if enabled(state):
        check_incoming(state, info, RPC.call)
    height = RPC.call(state['btc_cli'], 'getinfo')['blockheight']
    if info['expires_at'] <= int(time.time()):
        return 'quote_expired'
    remaining = info['cltv_expiry'] - height
    # Experimental BTC-regtest policy: this is a fresh BTC-chain check, not
    # a claim that block counts on independent chains represent equal time.
    if not info['min_cltv_delta'] <= remaining <= info['max_cltv_delta']:
        return 'insufficient_btc_cltv' if remaining < info['min_cltv_delta'] else 'excessive_btc_cltv'
    if state.get('xbt_invoice') != info['xbt_invoice']:
        return 'quoted_invoice_mismatch'
    decoded = RPC.call(state['xbt_cli'], 'decode', info['xbt_invoice'])
    if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
            or decoded.get('currency') != ('xbt' if pilot.is_live(state) else 'xbtrt')
            or decoded.get('payment_hash') != state['payment_hash']
            or decoded.get('payment_secret') != state['payment_secret']
            or decoded.get('amount_msat') != state['xbt_amount_msat']):
        return 'xbt_invoice_fields_mismatch'
    if decoded['created_at'] + decoded['expiry'] <= int(time.time()):
        return 'xbt_invoice_expired'
    # This fixture only supports one direct hop. Bind that hop to the signed
    # invoice, rather than trusting a separately supplied destination/amount.
    route = state.get('route', [])
    if (len(route) != 1 or route[0].get('id') != decoded['payee']
            or route[0].get('amount_msat') != decoded['amount_msat']
            or type(route[0].get('delay')) is not int
            or route[0]['delay'] < decoded['min_final_cltv_expiry']):
        return 'xbt_route_mismatch'
    return None


def run(path, crash_after_xbt=False, crash_after_btc=False, crash_after_sendpay=False,
        wait_pending=False, recover_only=False):
    # Lock a separate stable inode: save() atomically replaces the state file.
    # Never unlink the lock file, including on normal exit or recovery.
    # All controllers for this swap must use this same canonical state path;
    # copied state files and multiple hosts are outside this fixture's scope.
    path = path.resolve()
    fd = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'outcome': 'busy'}
        return run_locked(path, crash_after_xbt, crash_after_btc,
                          crash_after_sendpay, wait_pending, recover_only)
    finally:
        os.close(fd)  # The kernel also releases the lock after process death.


def run_locked(path, crash_after_xbt=False, crash_after_btc=False, crash_after_sendpay=False,
               wait_pending=False, recover_only=False):
    state = json.loads(path.read_text())
    if recover_only and state['phase'] == 'prepared':
        return {'phase': 'prepared', 'outcome': 'needs_manual_start'}
    pilot.verify_state(state, RPC.call)
    from incoming_btc import enabled, validate_state
    if enabled(state):
        validate_state(state)
    payment_hash = state['payment_hash']
    if state['phase'] == 'prepared':
        if state.get('quote_gate'):
            reason = check_spend(state)
            if reason:
                return {'phase': 'prepared', 'outcome': 'refused', 'reason': reason}
        # Record intent before sending. A restart must reconcile, never blindly
        # repeat sendpay: an interrupted RPC does not tell us whether it sent.
        state['phase'] = 'outgoing_started'
        save(path, state)
        RPC.call([*state['xbt_cli'], '-k'], 'sendpay',
                'route=' + json.dumps(state['route']),
                'payment_hash=' + payment_hash,
                'payment_secret=' + state['payment_secret'])
        if crash_after_sendpay:
            os._exit(88)  # Submission acknowledged; no waitsendpay or preimage.
        if wait_pending:
            # Bounded fixture wait used to exercise an overlapping controller.
            # Keep individual RPC calls short; a timeout never authorizes refund.
            def terminal():
                payments = [p for p in RPC.call(state['xbt_cli'], 'listsendpays')['payments']
                            if p['payment_hash'] == payment_hash]
                if len(payments) != 1:
                    raise RuntimeError('unexpected outgoing attempt count while waiting')
                return payments[0]['status'] != 'pending'
            wait_until(terminal, timeout=60)
        payment = RPC.call(state['xbt_cli'], 'waitsendpay', payment_hash, 10)
        if payment['status'] != 'complete':
            raise RuntimeError('outgoing payment did not complete')
        if crash_after_xbt:
            os._exit(86)  # No cleanup or completion checkpoint: simulated crash.

    if state['phase'] == 'outgoing_started':
        payments = [p for p in RPC.call(state['xbt_cli'], 'listsendpays')['payments']
                    if p['payment_hash'] == payment_hash]
        if len(payments) != 1:
            raise RuntimeError('outgoing outcome unresolved; refusing resend or BTC release')
        payment = payments[0]
        if payment['amount_msat'] != state['xbt_amount_msat']:
            raise RuntimeError('outgoing amount mismatch')
        if payment['status'] == 'pending' and not payment.get('payment_preimage'):
            # A later invocation reconciles again. Neither resend nor release
            # (nor fail) BTC on a timeout or a still-pending outgoing payment.
            protect(path, state, RPC.call, save)
            return {'phase': 'outgoing_started', 'outcome': 'pending'}
        if (payment['status'] == 'failed' and not payment.get('payment_preimage')
                and state.get('quote_gate')):
            state['phase'] = 'xbt_failed'
            save(path, state)
        elif payment['status'] != 'complete':
            raise RuntimeError('outgoing outcome unresolved; refusing resend or BTC release')
        else:
            preimage = payment['payment_preimage']
            if hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != payment_hash:
                raise RuntimeError('outgoing preimage mismatch')
            state['preimage'] = preimage
            state['phase'] = 'xbt_paid'
            save(path, state)

    if state['phase'] == 'xbt_failed':
        if not state.get('quote_gate') or 'preimage' in state:
            raise RuntimeError('invalid failure checkpoint')
        status = RPC.call(state['btc_cli'], 'xbt-quote-status', payment_hash)
        if (status['payment_hash'] != payment_hash
                or status['binding'] != state['btc_binding']
                or status['phase'] not in ('held', 'failed')):
            raise RuntimeError('BTC quote not eligible for failure; inspect node state')
        if status['phase'] == 'held':
            result = RPC.call(state['btc_cli'], 'xbt-fail', payment_hash,
                             json.dumps(state['btc_binding']))
            if result['failed'] != 1:
                raise RuntimeError('bound BTC HTLC not failed')
        state['phase'] = 'btc_failed'
        save(path, state)
    if state['phase'] == 'btc_failed':
        return {'phase': 'btc_failed', 'outcome': 'failed'}

    if state['phase'] == 'xbt_paid':
        released = False
        if state.get('quote_gate'):
            status = RPC.call(state['btc_cli'], 'xbt-quote-status', payment_hash)
            if (status['payment_hash'] != payment_hash
                    or status['binding'] != state['btc_binding']
                    or status['phase'] not in ('held', 'resolved')):
                raise RuntimeError('BTC quote phase or binding mismatch; inspect node state')
            released = status['phase'] == 'resolved'
        if not released and RPC.call(state['btc_cli'], 'xbt-release', state['preimage'])['released'] != 1:
            raise RuntimeError('held BTC HTLC not released; inspect node state')
        if crash_after_btc:
            os._exit(87)  # Release acknowledged, controller checkpoint absent.
        state['phase'] = 'btc_released'
        save(path, state)
    if state['phase'] != 'btc_released':
        raise RuntimeError('unexpected controller phase')
    return {'payment_preimage': state['preimage'], 'phase': state['phase']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--crash-after-xbt', action='store_true')
    parser.add_argument('--crash-after-btc', action='store_true')
    parser.add_argument('--crash-after-sendpay', action='store_true')
    parser.add_argument('--wait-pending', action='store_true',
                        help='Fixture: wait up to 60 seconds for outgoing completion while holding the lock.')
    args = parser.parse_args()
    print(json.dumps(run(args.state, args.crash_after_xbt, args.crash_after_btc,
                         args.crash_after_sendpay, args.wait_pending)), flush=True)


if __name__ == '__main__':
    main()
