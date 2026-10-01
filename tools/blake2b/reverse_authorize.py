"""Explicit per-quote operator authorization for bounded automatic processing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from reverse_activation import activation
from reverse_live import PROFILE, enabled, validate_terms
from service_manager import private_load
from swap_controller import save


def digest(quote):
    return hashlib.sha256(json.dumps(quote, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_binding(directory, settings, quote):
    from reverse_service import binding
    if (directory.resolve().parent != Path(settings['swap_root']).resolve()
            or quote['config'] != binding(settings)
            or quote['terms']['profile'] != settings.get('reverse_profile', PROFILE)):
        raise ValueError('authorization operator or directory binding mismatch')
    validate_terms(quote['terms'])
    if not quote.get('xbt_invoice'):
        raise ValueError('quote must be signed before authorization')


def valid_record(record, quote):
    return (isinstance(record, dict)
        and set(record) == {'format', 'quote_sha256', 'expires_at', 'authorized_at'}
        and record['format'] == 'reverse-authorization-v1'
        and record['quote_sha256'] == digest(quote)
        and record['expires_at'] == quote['terms']['expires_at']
        and type(record['authorized_at']) is int
        and 0 <= record['authorized_at'] < record['expires_at'])


def authorize(directory, settings, now=time.time):
    from reverse_service import locked
    with activation(settings), locked(directory):
        quote = private_load(directory/'reverse-quote.json')
        enabled(quote['terms']['profile'])
        validate_binding(directory, settings, quote)
        path = directory/'reverse-authorization.json'
        if path.exists():
            if not valid_record(private_load(path), quote):
                raise ValueError('existing authorization differs')
            return {'authorized': True, 'payment_started': False}
        state_path = directory/'reverse-state.json'
        if state_path.exists() and private_load(state_path)['phase'] != 'prepared':
            raise ValueError('payment already started; use recovery')
        timestamp = int(now())
        if timestamp >= quote['terms']['expires_at']:
            raise ValueError('quote expired before authorization')
        save(path, dict(format='reverse-authorization-v1', quote_sha256=digest(quote),
                        expires_at=quote['terms']['expires_at'], authorized_at=timestamp))
    return {'authorized': True, 'payment_started': False}


def process_record(directory, settings, rpc=None, controller=None):
    from reverse_service import recover_record, step, reconcile
    from swap_rpc import RPC
    kwargs = {}
    if rpc is not None:
        kwargs['rpc'] = rpc
    if controller is not None:
        kwargs['controller'] = controller
    rpc = rpc or RPC.call
    controller = controller or reconcile
    state_path = directory/'reverse-state.json'
    # Existing obligations must recover even if authorization expired or was removed.
    started = state_path.exists() and private_load(state_path)['phase'] != 'prepared'
    permit_path = directory/'reverse-authorization.json'
    if started or not permit_path.exists():
        return recover_record(directory, settings, **kwargs)
    quote = private_load(directory/'reverse-quote.json')
    validate_binding(directory, settings, quote)
    permit = private_load(permit_path)
    if not valid_record(permit, quote):
        raise ValueError('automatic-processing authorization differs')
    if permit['expires_at'] <= int(time.time()):
        return {'outcome': 'authorization_expired'}
    with activation(settings):
        return step(directory, rpc=rpc, controller=controller, authorized_digest=permit['quote_sha256'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(authorize(args.directory.expanduser().resolve(), private_load(args.settings.expanduser()))))
        return 0
    except Exception:
        print(json.dumps(dict(event='reverse_authorization_error', details='withheld', payment_started=False)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
