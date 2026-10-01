"""Durable single-part XBT quote gate. Regtest default; dormant explicit live mode.

Quotes and terminal intent live beside this plugin. The harness copies
quote_plugin.py alongside it for shared CLTV/replay validation and atomic I/O.
"""
import hashlib
import json
from pathlib import Path
import sys
import time

from quote_plugin import replay_identity, save, validation_error


def admission(terms, htlc, onion, now):
    # The shared validator's historical BTC field names refer to its incoming
    # leg. Adapt only at this boundary; stored reverse terms name assets exactly.
    mapped = dict(terms, btc_amount_msat=terms['xbt_amount_msat'], btc_channel=terms['xbt_channel'])
    return validation_error(mapped, htlc, onion, now)


def validate_terms(terms, live=False, service_regtest=False):
    if live or service_regtest:
        from reverse_live import PROFILE, SERVICE_REGTEST, enabled, validate_terms as validate_live_terms
        expected = PROFILE if live else SERVICE_REGTEST
        enabled(expected)
        if terms.get('profile') != expected:
            raise ValueError('reverse gate profile mismatch')
        return validate_live_terms(terms)
    required = {'payment_hash', 'payment_secret', 'xbt_amount_msat', 'btc_amount_msat',
                'btc_invoice', 'xbt_channel', 'expires_at', 'min_cltv_delta', 'max_cltv_delta'}
    if set(terms) != required:
        raise ValueError('unexpected reverse quote fields')
    for key in ('payment_hash', 'payment_secret'):
        raw = bytes.fromhex(terms[key])
        if len(raw) != 32 or terms[key] != terms[key].lower():
            raise ValueError('invalid hash or secret')
    for key, value in (('xbt_amount_msat', 200000000), ('btc_amount_msat', 100000000),
                       ('min_cltv_delta', 100), ('max_cltv_delta', 2000)):
        if type(terms[key]) is not int or terms[key] != value:
            raise ValueError('unsupported reverse regtest limits')
    if (not isinstance(terms['btc_invoice'], str) or not terms['btc_invoice'].startswith('lnbcrt')
            or not isinstance(terms['xbt_channel'], str) or not terms['xbt_channel']
            or type(terms['expires_at']) is not int):
        raise ValueError('invalid reverse invoice, channel or expiry')


class Gate:
    def __init__(self, path, live=False, service_regtest=False):
        if live and service_regtest:
            raise ValueError('choose exactly one gate profile')
        self.service_regtest = service_regtest
        self.live = live
        self.path = path
        self.quotes = json.loads(path.read_text()) if path.exists() else {}
        self.pending = {}
        self.active = False

    def handle(self, request):
        replies = []

        def reply(req, result):
            replies.append(dict(jsonrpc='2.0', id=req['id'], result=result))

        method = request['method']
        params = request.get('params', {})
        if method == 'getmanifest':
            methods = [('reverse-register', 'quote'), ('reverse-status', 'payment_hash'),
                       ('reverse-release', 'payment_hash binding preimage'),
                       ('reverse-fail', 'payment_hash binding'), ('xbt-held', '')]
            if self.live:
                methods.append(('reverse-pilot-info', ''))
            reply(request, dict(options=[], rpcmethods=[dict(name=n, usage=u, description='Reverse regtest gate')
                                                        for n, u in methods],
                                subscriptions=[], hooks=[{'name': 'htlc_accepted'}],
                                dynamic=True, nonnumericids=True))
            return replies
        if method == 'init':
            self.active = params['configuration']['network'] == ('xbt' if self.live else 'xbt-regtest')
            if self.live:
                from reverse_live import enabled
                try:
                    enabled()
                except RuntimeError:
                    self.active = False
            reply(request, {} if self.active else {'disable': 'reverse gate requires XBT regtest'})
            return replies
        if not self.active:
            raise ValueError('reverse gate inactive')

        def argument(index, key):
            return params[index] if isinstance(params, list) else params[key]

        if method == 'reverse-pilot-info' and self.live:
            reply(request, dict(profile='reverse-live-v1', gate_active=self.active))
        elif method == 'reverse-register':
            terms = argument(0, 'quote')
            validate_terms(terms, live=self.live, service_regtest=self.service_regtest)
            payment_hash = terms['payment_hash']
            old = self.quotes.get(payment_hash)
            if old:
                if old['terms'] != terms:
                    raise ValueError('reverse quote is immutable')
            else:
                now = int(time.time())
                if not now < terms['expires_at'] <= now + 3600:
                    raise ValueError('quote expiry outside fixture limits')
                if any(e['phase'] == 'held' or (e['phase'] == 'quoted' and e['terms']['expires_at'] > now)
                       for e in self.quotes.values()):
                    raise ValueError('another reverse quote is active')
                self.quotes[payment_hash] = dict(terms=terms, phase='quoted')
                save(self.path, self.quotes)
            reply(request, {'registered': True})
        elif method == 'htlc_accepted':
            htlc, onion = params['htlc'], params['onion']
            payment_hash = htlc['payment_hash']
            entry = self.quotes.get(payment_hash)
            if entry is None and (self.live or self.service_regtest):
                reply(request, {'result': 'continue'})
                return replies
            binding = [htlc['short_channel_id'], htlc['id']]
            snapshot = replay_identity(htlc, onion)
            if entry and entry.get('binding') == binding and entry['phase'] in ('held', 'resolved', 'failed'):
                if entry.get('accepted') != snapshot:
                    # Never fail an accepted HTLC on inconsistent replay: BTC
                    # may already have been spent. Leave it unresolved.
                    return replies
                if entry['phase'] == 'held':
                    self.pending[payment_hash] = request
                elif entry['phase'] == 'resolved':
                    reply(request, {'result': 'resolve', 'payment_key': entry['preimage']})
                else:
                    reply(request, {'result': 'fail', 'failure_message': '2002'})
            elif (entry and entry['phase'] == 'quoted'
                  and admission(entry['terms'], htlc, onion, int(time.time())) is None):
                entry.update(phase='held', binding=binding, accepted=snapshot)
                save(self.path, self.quotes)
                self.pending[payment_hash] = request
            else:
                reply(request, {'result': 'fail', 'failure_message': '2002'})
        elif method == 'xbt-held':
            reply(request, {'held': [r['params']['htlc'] for r in self.pending.values()]})
        elif method == 'reverse-status':
            payment_hash = argument(0, 'payment_hash')
            entry = self.quotes[payment_hash]
            reply(request, dict(payment_hash=payment_hash, phase=entry['phase'],
                                binding=entry.get('binding'), terms=entry['terms'],
                                cltv_expiry=entry.get('accepted', {}).get('htlc', {}).get('cltv_expiry'),
                                hook_ready=payment_hash in self.pending))
        elif method in ('reverse-release', 'reverse-fail'):
            payment_hash, binding = argument(0, 'payment_hash'), argument(1, 'binding')
            entry = self.quotes[payment_hash]
            if entry.get('binding') != binding or not entry.get('accepted'):
                raise ValueError('original HTLC binding required')
            release = method == 'reverse-release'
            phase, field = ('resolved', 'released') if release else ('failed', 'failed')
            if release:
                preimage = argument(2, 'preimage')
                raw = bytes.fromhex(preimage)
                if len(raw) != 32 or hashlib.sha256(raw).hexdigest() != payment_hash:
                    raise ValueError('preimage mismatch')
            if entry['phase'] == phase:
                reply(request, {field: 1})  # Exact terminal intent already persisted.
                return replies
            if entry['phase'] != 'held' or payment_hash not in self.pending:
                raise ValueError('original held hook unavailable')
            entry['phase'] = phase
            if release:
                entry['preimage'] = preimage
            save(self.path, self.quotes)  # Always before returning the hook response.
            hook = self.pending.pop(payment_hash)
            reply(hook, {'result': 'resolve', 'payment_key': preimage} if release
                  else {'result': 'fail', 'failure_message': '2002'})
            reply(request, {field: 1})
        elif 'id' in request:
            raise ValueError('unknown reverse gate method')
        return replies


def main(path=None, live=False, service_regtest=False):
    gate = Gate(path or Path(__file__).with_suffix('.quotes.json'), live=live, service_regtest=service_regtest)
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        try:
            replies = gate.handle(request)
        except (ValueError, KeyError, TypeError, IndexError):
            # Fixed error text; never dump secrets or onion contents. For a
            # malformed accepted replay, preserve it unresolved rather than
            # accidentally returning a failure after outgoing BTC spending.
            if request.get('method') == 'htlc_accepted':
                continue
            replies = [dict(jsonrpc='2.0', id=request['id'], error=dict(
                code=-32602, message='reverse gate request refused'))]
        for response in replies:
            print(json.dumps(response), end='\n\n', flush=True)


if __name__ == '__main__':
    main()
