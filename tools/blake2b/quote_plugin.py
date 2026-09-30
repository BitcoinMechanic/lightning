"""Experimental single-part quote gate. No automatic XBT spending.

The controller registers trusted quotes and resolves them after XBT settlement.
State lives beside this copied plugin. Regtest is the default; live is opt-in.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import time


def save(path, quotes):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(quotes, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validation_error(quote, htlc, onion, now):
    # Return fixed labels only: never log secrets, preimages or onion payloads.
    checks = (
        (quote['expires_at'] > now, 'quote expired'),
        ('btc_channel' not in quote or htlc.get('short_channel_id') == quote['btc_channel'],
         'incoming channel differs from quote'),
        (htlc['payment_hash'] == quote['payment_hash'], 'payment hash mismatch'),
        (hmac.compare_digest(onion.get('payment_secret', ''), quote['payment_secret']),
         'payment secret mismatch'),
        (htlc['amount_msat'] == quote['btc_amount_msat'], 'incoming amount mismatch'),
        (onion.get('forward_msat') == quote['btc_amount_msat'], 'forward amount mismatch'),
        (onion.get('total_msat') == quote['btc_amount_msat'], 'total amount mismatch'),
        (onion.get('type') == 'tlv', 'expected TLV onion'),
        ('short_channel_id' not in onion and 'next_node_id' not in onion, 'not final hop'),
    )
    for valid, reason in checks:
        if not valid:
            return reason
    expiry = htlc['cltv_expiry']
    remaining = htlc['cltv_expiry_relative']
    outgoing = onion.get('outgoing_cltv_value')
    if any(type(value) is not int for value in (expiry, remaining, outgoing)):
        return 'invalid CLTV fields'
    # BOLT 4 / lightningd check_cltv permits incoming >= outgoing. xpay's
    # shadow route can pad incoming without changing the final onion expiry.
    if outgoing > expiry:
        return 'onion expiry exceeds incoming expiry'
    if not quote['min_cltv_delta'] <= remaining <= quote['max_cltv_delta']:
        return 'incoming CLTV outside quote bounds'
    blockheight = expiry - remaining
    if not quote['min_cltv_delta'] <= outgoing - blockheight <= quote['max_cltv_delta']:
        return 'onion CLTV outside quote bounds'
    return None


def validate(quote, htlc, onion, now):
    return validation_error(quote, htlc, onion, now) is None


def replay_identity(htlc, onion):
    """Immutable accepted fields; relative CLTV changes as blocks arrive."""
    if not isinstance(htlc, dict) or not isinstance(onion, dict):
        return None
    return {
        'htlc': {key: htlc.get(key) for key in
                 ('short_channel_id', 'id', 'payment_hash', 'amount_msat', 'cltv_expiry')},
        'onion': {key: onion.get(key) for key in
                  ('payment_secret', 'forward_msat', 'total_msat', 'type',
                   'outgoing_cltv_value', 'short_channel_id', 'next_node_id')},
    }


def log_rejection(reason):
    print(json.dumps({'jsonrpc': '2.0', 'method': 'log',
                      'params': {'level': 'warn', 'message': 'Quote gate rejected HTLC: ' + reason}}),
          end='\n\n', flush=True)


def reply(request, result):
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}),
          end='\n\n', flush=True)


def main():
    path = Path(__file__).with_suffix('.quotes.json')
    quotes = json.loads(path.read_text()) if path.exists() else {}
    pending = {}
    active = False
    live = False
    live_profile = None
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        method, params = request['method'], request.get('params', {})
        if method == 'getmanifest':
            reply(request, {'options': [{'name': 'xbt-live-pilot', 'type': 'string',
                'default': 'disabled', 'description': 'Explicit bounded live pilot opt-in'}], 'rpcmethods': [
                {'name': 'xbt-register', 'usage': 'quote', 'description': 'Register immutable test swap terms'},
                {'name': 'xbt-renew', 'usage': 'quote expiry', 'description': 'Renew an unused expired v2 quote once'},
                {'name': 'xbt-pilot-info', 'usage': '', 'description': 'Read active profile and saved quote count'},
                {'name': 'xbt-held', 'usage': '', 'description': 'List validated held HTLCs'},
                {'name': 'xbt-quote-status', 'usage': 'payment_hash', 'description': 'Read durable quote phase and binding'},
                {'name': 'xbt-spend-info', 'usage': 'payment_hash', 'description': 'Read bound HTLC expiry and quote limits before spending'},
                {'name': 'xbt-fail', 'usage': 'payment_hash binding', 'description': 'Persist failure of the bound test HTLC'},
                {'name': 'xbt-release', 'usage': 'preimage', 'description': 'Settle validated test swap'}],
                'subscriptions': [], 'hooks': [{'name': 'htlc_accepted'}],
                'dynamic': True, 'nonnumericids': True})
            continue
        if method == 'init':
            network = params['configuration']['network']
            live_profile = params.get('options', {}).get('xbt-live-pilot')
            live = network == 'bitcoin' and live_profile in ('live-pilot-v1', 'live-pilot-v2', 'live-market-v1')
            active = network == 'regtest' or live
            reply(request, {} if active else {'disable': 'requires regtest or explicit live pilot'})
            continue
        if not active:
            raise RuntimeError('quote gate not initialized for the selected network')
        try:
            if method == 'xbt-register':
                quote = params[0] if isinstance(params, list) else params['quote']
                required = {'payment_hash', 'payment_secret', 'btc_amount_msat',
                            'xbt_amount_msat', 'xbt_invoice', 'expires_at',
                            'min_cltv_delta', 'max_cltv_delta'}
                if live:
                    required.add('pilot')
                    if quote.get('pilot') == 'live-pilot-v2':
                        required.update(('replaces', 'btc_channel'))
                    if quote.get('pilot') == 'live-market-v1':
                        required.update(('btc_channel', 'oracle_digest', 'controller_id'))
                if set(quote) != required:
                    raise ValueError('unexpected quote fields')
                if live:
                    limits = {'live-pilot-v1': (1000000, 2000000),
                              'live-pilot-v2': (2000000, 4000000)}
                    if quote['pilot'] == 'live-market-v1':
                        for key, cap in (('btc_amount_msat', 10000000), ('xbt_amount_msat', 500000000)):
                            if type(quote[key]) is not int or quote[key] % 1000 or not 0 < quote[key] <= cap:
                                raise ValueError('market amount outside hard limits')
                        for key in ('oracle_digest', 'controller_id'):
                            if len(bytes.fromhex(quote[key])) != 32 or quote[key] != quote[key].lower():
                                raise ValueError('invalid market audit or controller identity')
                        if not isinstance(quote['btc_channel'], str) or not quote['btc_channel']:
                            raise ValueError('missing market channel binding')
                    elif (quote['pilot'] not in limits
                          or (quote['btc_amount_msat'], quote['xbt_amount_msat']) != limits[quote['pilot']]):
                        raise ValueError('live pilot limits mismatch')
                    if quote['min_cltv_delta'] != 288 or quote['max_cltv_delta'] != 2016:
                        raise ValueError('live pilot limits mismatch')
                    if quote['payment_hash'] not in quotes:
                        if quote['pilot'] != live_profile:
                            raise ValueError('new quote profile differs from enabled pilot')
                        if quote['pilot'] == 'live-pilot-v2':
                            previous = quotes.get(quote['replaces'])
                            if (len(quotes) != 1 or previous is None
                                    or previous['phase'] != 'failed' or 'preimage' in previous
                                    or previous['terms'].get('pilot') != 'live-pilot-v1'
                                    or not previous.get('binding')
                                    or quote['btc_channel'] != previous['binding'][0]):
                                raise ValueError('replacement requires the sole failed v1 quote on its original channel')
                        elif quote['pilot'] == 'live-market-v1':
                            now = int(time.time())
                            if not now < quote['expires_at'] <= now + 120:
                                raise ValueError('market quote expiry exceeds two minutes')
                            for previous in quotes.values():
                                phase = previous['phase']
                                if phase in ('resolved', 'failed'):
                                    continue
                                if (phase == 'quoted' and previous['terms']['expires_at'] <= now
                                        and 'binding' not in previous and 'accepted' not in previous):
                                    continue
                                raise ValueError('another quote is active; finish it first')
                        elif quotes:
                            raise ValueError('live pilot permits one quote only; retain original state')
                for key in ('payment_hash', 'payment_secret'):
                    if len(bytes.fromhex(quote[key])) != 32 or quote[key] != quote[key].lower():
                        raise ValueError('invalid hash or secret')
                for key in ('btc_amount_msat', 'xbt_amount_msat', 'expires_at',
                            'min_cltv_delta', 'max_cltv_delta'):
                    if type(quote[key]) is not int or quote[key] <= 0:
                        raise ValueError('invalid quote integer')
                if (quote['expires_at'] <= int(time.time())
                        or quote['min_cltv_delta'] > quote['max_cltv_delta']
                        or not quote['xbt_invoice'].startswith('lnxbt' if live else 'lnxbtrt')
                        or (live and quote['xbt_invoice'].startswith('lnxbtrt'))):
                    raise ValueError('invalid expiry or XBT invoice')
                if quote['payment_hash'] in quotes:
                    if quotes[quote['payment_hash']]['terms'] != quote:
                        raise ValueError('quote already registered with different terms')
                    reply(request, {'registered': True})
                    continue  # Idempotent setup retry: never reset phase or binding.
                quotes[quote['payment_hash']] = {'terms': quote, 'phase': 'quoted'}
                save(path, quotes)
                reply(request, {'registered': True})
            elif method == 'xbt-renew':
                old = params[0] if isinstance(params, list) else params['quote']
                expiry = params[1] if isinstance(params, list) else params['expiry']
                entry = quotes.get(old['payment_hash'])
                if (not live or live_profile != 'live-pilot-v2' or entry is None
                        or old.get('pilot') != 'live-pilot-v2'
                        or entry['phase'] != 'quoted' or 'binding' in entry
                        or 'accepted' in entry or 'preimage' in entry
                        or old['payment_hash'] in pending):
                    raise ValueError('renewal requires a never-accepted live v2 quote')
                renewed = dict(old, expires_at=expiry)
                history = {'old_terms': old, 'new_expiry': expiry}
                if entry.get('renewal') == history and entry['terms'] == renewed:
                    reply(request, {'renewed': True})
                    continue  # Lost RPC reply: exact retry, not another extension.
                if (entry.get('renewal') is not None or entry['terms'] != old
                        or type(expiry) is not int or old['expires_at'] > int(time.time())
                        or not int(time.time()) < expiry <= int(time.time()) + 600):
                    raise ValueError('invalid or already-used quote renewal')
                entry.update(terms=renewed, renewal=history)
                save(path, quotes)
                reply(request, {'renewed': True})
            elif method == 'htlc_accepted':
                htlc, onion = params['htlc'], params['onion']
                entry = quotes.get(htlc['payment_hash'])
                if live and entry is None:
                    reply(request, {'result': 'continue'})
                    continue
                binding = [htlc['short_channel_id'], htlc['id']]
                if entry and entry.get('binding') == binding and entry['phase'] == 'resolved':
                    reply(request, {'result': 'resolve', 'payment_key': entry['preimage']})
                elif entry and entry.get('binding') == binding and entry['phase'] == 'failed':
                    reply(request, {'result': 'fail', 'failure_message': '2002'})
                elif entry and entry.get('binding') == binding and entry['phase'] == 'held':
                    # Admission expiry cannot cancel an accepted swap: XBT may
                    # already be pending or settled. Restore only the exact
                    # persisted hook; never run fresh-admission CLTV checks.
                    if (entry.get('accepted') is not None
                            and entry['accepted'] == replay_identity(htlc, onion)):
                        pending[htlc['payment_hash']] = request
                    else:
                        # Old checkpoints lack this snapshot. Do not fail BTC
                        # or expose a releasable hook on uncertain identity.
                        log_rejection('held replay needs inspection; BTC remains unresolved')
                elif (entry and entry['phase'] == 'quoted'
                      and entry.get('binding', binding) == binding
                      and validate(entry['terms'], htlc, onion, int(time.time()))):
                    entry.update(phase='held', binding=binding,
                                 accepted=replay_identity(htlc, onion))
                    save(path, quotes)
                    pending[htlc['payment_hash']] = request
                    # Only validated and durably bound HTLCs become visible.
                else:
                    reason = (validation_error(entry['terms'], htlc, onion, int(time.time()))
                              if entry else 'unknown quote')
                    log_rejection(reason or 'quote phase or HTLC binding mismatch')
                    reply(request, {'result': 'fail', 'failure_message': '2002'})
            elif method == 'xbt-pilot-info':
                reply(request, {'profile': live_profile if live else 'regtest',
                                'registered_quotes': len(quotes)})
            elif method == 'xbt-held':
                reply(request, {'held': [r['params']['htlc'] for r in pending.values()]})
            elif method == 'xbt-quote-status':
                payment_hash = params[0] if isinstance(params, list) else params['payment_hash']
                entry = quotes.get(payment_hash)
                if entry is None:
                    raise ValueError('unknown quote')
                # Resolved means durable release intent, not proof that the
                # peer has committed settlement. Never expose the preimage.
                reply(request, {'payment_hash': payment_hash, 'phase': entry['phase'],
                                'binding': entry.get('binding')})
            elif method == 'xbt-spend-info':
                payment_hash = params[0] if isinstance(params, list) else params['payment_hash']
                entry = quotes.get(payment_hash)
                held = pending.get(payment_hash)
                if entry is None or entry['phase'] != 'held' or held is None:
                    raise ValueError('quote has no active held HTLC')
                terms = entry['terms']
                reply(request, {'payment_hash': payment_hash, 'binding': entry['binding'],
                                'cltv_expiry': held['params']['htlc']['cltv_expiry'],
                                'min_cltv_delta': terms['min_cltv_delta'],
                                'max_cltv_delta': terms['max_cltv_delta'],
                                'expires_at': terms['expires_at'],
                                'xbt_invoice': terms['xbt_invoice'],
                                'xbt_amount_msat': terms['xbt_amount_msat'],
                                'btc_amount_msat': terms['btc_amount_msat'],
                                'pilot': terms.get('pilot'),
                                'btc_channel': terms.get('btc_channel'),
                                'oracle_digest': terms.get('oracle_digest'),
                                'controller_id': terms.get('controller_id')})
            elif method == 'xbt-fail':
                payment_hash = params[0] if isinstance(params, list) else params['payment_hash']
                binding = params[1] if isinstance(params, list) else params['binding']
                entry = quotes.get(payment_hash)
                held = pending.get(payment_hash)
                if (entry is None or entry['phase'] != 'held' or held is None
                        or entry.get('binding') != binding or 'preimage' in entry):
                    raise ValueError('no matching bound HTLC eligible for failure')
                entry['phase'] = 'failed'
                save(path, quotes)  # Durable failure intent before hook response.
                reply(held, {'result': 'fail', 'failure_message': '2002'})
                del pending[payment_hash]
                reply(request, {'failed': 1})
            elif method == 'xbt-release':
                preimage = params[0] if isinstance(params, list) else params['preimage']
                raw = bytes.fromhex(preimage)
                if len(raw) != 32:
                    raise ValueError('invalid preimage')
                payment_hash = hashlib.sha256(raw).hexdigest()
                held = pending.get(payment_hash)
                if held is None:
                    raise ValueError('no validated HTLC held for this preimage')
                entry = quotes[payment_hash]
                entry.update(phase='resolved', preimage=preimage)
                save(path, quotes)  # Persist before revealing the secret.
                reply(held, {'result': 'resolve', 'payment_key': preimage})
                del pending[payment_hash]
                reply(request, {'released': 1})
            elif 'id' in request:
                raise ValueError('unknown method')
        except (ValueError, KeyError, TypeError) as exc:
            if method == 'htlc_accepted':
                log_rejection('malformed hook fields')
                reply(request, {'result': 'fail', 'failure_message': '2002'})
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                                  'error': {'code': -32602, 'message': str(exc)}}),
                      end='\n\n', flush=True)


if __name__ == '__main__':
    main()
