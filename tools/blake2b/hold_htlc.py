"""Disposable regtest fixture: hold incoming HTLC hooks without resolving them.

The harness supplies a venv shebang when copying this into its temporary dir.
Do not load this plugin into a real node: held payments deliberately stall.
"""
import json
import hashlib
import sys


def main():
    held = []
    pending = []
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        method = request['method']
        if method == 'getmanifest':
            result = {'options': [], 'rpcmethods': [
                {'name': 'xbt-held', 'usage': '', 'description': 'List held test HTLCs'},
                {'name': 'xbt-release', 'usage': 'preimage',
                 'description': 'Resolve held test HTLCs with a matching preimage'},
                {'name': 'xbt-fail', 'usage': 'payment_hash',
                 'description': 'Fail matching held test HTLCs'},
                {'name': 'xbt-continue', 'usage': 'payment_hash',
                 'description': 'Resume normal invoice handling for held test HTLCs'}],
                'subscriptions': [], 'hooks': [{'name': 'htlc_accepted'}],
                'dynamic': True, 'nonnumericids': True}
        elif method == 'init':
            if request['params']['configuration']['network'] not in ('xbt-regtest', 'regtest'):
                result = {'disable': 'This fixture requires BTC or XBT regtest'}
            else:
                result = {}
        elif method == 'htlc_accepted':
            held.append(request['params']['htlc'])
            pending.append(request)
            continue  # Deliberately leave this hook request unanswered.
        elif method == 'xbt-held':
            result = {'held': held}
        elif method == 'xbt-continue':
            params = request['params']
            payment_hash = params[0] if isinstance(params, list) else params['payment_hash']
            matches = [p for p in pending if
                       p['params']['htlc']['payment_hash'] == payment_hash]
            for p in matches:
                print(json.dumps({'jsonrpc': '2.0', 'id': p['id'],
                                  'result': {'result': 'continue'}}),
                      end='\n\n', flush=True)
                pending.remove(p)
            result = {'continued': len(matches)}
        elif method == 'xbt-fail':
            params = request['params']
            payment_hash = params[0] if isinstance(params, list) else params['payment_hash']
            matches = [p for p in pending if
                       p['params']['htlc']['payment_hash'] == payment_hash]
            for p in matches:
                print(json.dumps({'jsonrpc': '2.0', 'id': p['id'],
                                  'result': {'result': 'fail', 'failure_message': '2002'}}),
                      end='\n\n', flush=True)
                pending.remove(p)
            result = {'failed': len(matches)}
        elif method == 'xbt-release':
            params = request['params']
            preimage = params[0] if isinstance(params, list) else params['preimage']
            raw = bytes.fromhex(preimage)
            if len(raw) != 32:
                raise ValueError('preimage must be 32 bytes')
            payment_hash = hashlib.sha256(raw).hexdigest()
            matches = [p for p in pending if
                       p['params']['htlc']['payment_hash'] == payment_hash]
            for p in matches:
                print(json.dumps({'jsonrpc': '2.0', 'id': p['id'],
                                  'result': {'result': 'resolve', 'payment_key': preimage}}),
                      end='\n\n', flush=True)
                pending.remove(p)
            result = {'released': len(matches)}
        else:
            if 'id' not in request:
                continue
            raise RuntimeError(f'unexpected test plugin request: {method}')
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}),
              end='\n\n', flush=True)


if __name__ == '__main__':
    main()
