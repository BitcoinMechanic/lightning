"""Bounded reverse quote/run/recovery integration; activation is still disabled.

No payer RPCs or automatic payments. Operator command arrays come from private
service-manager settings. Signed invoices are published only after registration.
Never delete or reuse a swap directory after an uncertain RPC outcome.
"""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import json
import os
from pathlib import Path
import secrets
import time

from service_manager import private_load
from swap_rpc import RPC
from swap_controller import save
from reverse_controller import run as reconcile
from reverse_check import check, private_invoice
from reverse_route import plan
from reverse_policy import inspect_remote_policies
from reverse_metadata import invoice_metadata
from reverse_timing import proposal
from reverse_live import PROFILE, SERVICE_REGTEST, networks, enabled, validate_terms, private_final_allowed
from swap_invoice import unsigned_invoice


@contextmanager
def locked(directory):
    fd = os.open(directory/'reverse-service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def binding(settings):
    ids = settings['node_ids']
    if len(ids) != 2:
        raise ValueError('operator identities unavailable')
    return dict(btc_cli=settings['btc_cli'], xbt_cli=settings['xbt_cli'],
                node_ids=[ids[1], ids[0]], payer_id=settings['receiver_id'])


def create(settings, invoice, directory, rpc=RPC.call, inspector=check):
    from reverse_activation import activation
    with activation(settings):
        return _create(settings, invoice, directory, rpc, inspector)


def _create(settings, invoice, directory, rpc=RPC.call, inspector=check):
    profile = settings.get('reverse_profile', PROFILE)
    enabled(profile)
    net = networks(profile)
    if directory.resolve().parent != Path(settings['swap_root']).resolve():
        raise ValueError('reverse swap must be an immediate child of the monitored swap root')
    config = binding(settings)
    clis = dict(btc=config['btc_cli'], operator=config['xbt_cli'])
    summary = inspector(invoice, clis, rpc=rpc, max_routing_fee_sats=30,
                        margin_bps=100, max_xbt_sats=500000, max_delay=576,
                        _service_regtest=profile == SERVICE_REGTEST, payer_id=config['payer_id'])
    unknown_reason = 'remote HTLC limits unavailable for one or more planned hops'
    if (not summary.get('route_found') or summary['btc_sats'] != 1500
            or any(reason != unknown_reason for reason in summary.get('reasons', []))):
        raise ValueError('reverse pilot inspection did not pass')
    for role, node, network in (('btc', config['node_ids'][1], net['btc']),
                                ('operator', config['node_ids'][0], net['xbt'])):
        info = rpc(clis[role], 'getinfo')
        if info['id'] != node or info['network'] != network:
            raise ValueError('service node identity mismatch')
    decoded = rpc(config['btc_cli'], 'decode', invoice)
    metadata = invoice_metadata(decoded)
    route, routing = plan(config['btc_cli'], decoded, config['node_ids'][1], rpc,
                          max_fee_msat=30000, max_delay=576, max_hops=8, _inspection=True,
                          _service_regtest=profile == SERVICE_REGTEST)
    lookup = lambda scid: rpc(config['btc_cli'], 'listchannels', scid)
    audit = inspect_remote_policies(route, lookup)
    if (audit['remote_btc_policy_violations'] or
            (audit['remote_btc_policy_hops_unknown'] and not private_final_allowed(
                route, decoded, routing, audit, lookup))):
        raise ValueError('remote policy not eligible for bounded private-final exception')
    if (route[0]['delay'] != summary['btc_outgoing_cltv']
            or route[0]['amount_msat']-decoded['amount_msat'] != summary['routing_fee_msat']):
        raise ValueError('route changed since inspection; inspect anew')
    timing = proposal(route[0]['delay'])
    channels = [c for c in rpc(config['xbt_cli'], 'listpeerchannels')['channels']
                if c.get('peer_id') == config['payer_id'] and c['state'] == 'CHANNELD_NORMAL']
    if len(channels) != 1 or not channels[0]['peer_connected'] or channels[0].get('htlcs'):
        raise ValueError('incoming XBT channel not ready')
    now = int(time.time())
    if now*1000-summary['ticker_computed_at_ms'] > 30000:
        raise ValueError('market snapshot became stale before quote')
    expires = min(now+300, decoded['created_at']+decoded['expiry']-60)
    if expires <= now+60:
        raise ValueError('invoice expires too soon')
    terms = dict(profile=profile, payment_hash=decoded['payment_hash'],
        payment_secret=secrets.token_hex(32), btc_invoice=invoice,
        btc_amount_msat=decoded['amount_msat'], xbt_amount_msat=summary['estimated_xbt_sats']*1000,
        xbt_channel=channels[0]['short_channel_id'], expires_at=expires,
        min_cltv_delta=timing['minimum_xbt_remaining_blocks'], max_cltv_delta=2016,
        node_ids=config['node_ids'], payer_id=config['payer_id'], route=route,
        routing=routing, timing=timing, allow_signed_private_final=True)
    validate_terms(terms)
    # Refuse existing directories, including partial drafts from lost replies.
    directory.mkdir(mode=0o700)
    with locked(directory):
        quote = dict(config=config, terms=terms, btc_secret=decoded['payment_secret'],
                     btc_payment_metadata=metadata, inspection=summary)
        save(directory/'reverse-quote.json', quote)
        register_and_sign(directory, quote, rpc)
    return quote


def register_and_sign(directory, quote, rpc):
    terms, config = quote['terms'], quote['config']
    enabled(terms['profile'])
    net = networks(terms['profile'])
    validate_terms(terms)
    status = rpc(config['xbt_cli'], 'reverse-register', json.dumps(terms))
    if status != {'registered': True}:
        raise RuntimeError('reverse quote registration uncertain')
    unsigned = unsigned_invoice(terms['payment_hash'], terms['payment_secret'],
        amount_msat=terms['xbt_amount_msat'], expiry=max(1, terms['expires_at']-int(time.time())),
        currency=net['xbt_currency'], final_cltv=terms['timing']['proposed_xbt_invoice_cltv'],
        live_reverse=terms['profile'] == PROFILE)
    signed = rpc(config['xbt_cli'], 'signinvoice', unsigned)['bolt11']
    decoded = rpc(config['xbt_cli'], 'decode', signed)
    expected = dict(valid=True, currency=net['xbt_currency'], payment_hash=terms['payment_hash'],
                    payment_secret=terms['payment_secret'], amount_msat=terms['xbt_amount_msat'],
                    payee=terms['node_ids'][0], min_final_cltv_expiry=terms['timing']['proposed_xbt_invoice_cltv'])
    if any(decoded.get(k) != v for k, v in expected.items()):
        raise RuntimeError('signed reverse invoice mismatch')
    quote['xbt_invoice'] = signed
    save(directory/'reverse-quote.json', quote)


def step(directory, *, recover_only=False, rpc=RPC.call, controller=reconcile, authorized_digest=None):
    with locked(directory):
        quote = private_load(directory/'reverse-quote.json')
        if authorized_digest is not None:
            from reverse_authorize import digest
            if digest(quote) != authorized_digest:
                raise ValueError('authorized quote changed before processing')
        terms, config = quote['terms'], quote['config']
        enabled(terms['profile'])
        net = networks(terms['profile'])
        validate_terms(terms)
        for cli, network, node in ((config['xbt_cli'], net['xbt'], terms['node_ids'][0]),
                                   (config['btc_cli'], net['btc'], terms['node_ids'][1])):
            info = rpc(cli, 'getinfo')
            if info['network'] != network or info['id'] != node:
                raise ValueError('reverse service identity mismatch')
        path = directory/'reverse-state.json'
        if not path.exists():
            gate = rpc(config['xbt_cli'], 'reverse-status', terms['payment_hash'])
            if gate['terms'] != terms or gate['payment_hash'] != terms['payment_hash']:
                raise ValueError('registered reverse quote changed')
            if gate['phase'] == 'quoted':
                return {'outcome': 'quote_expired' if terms['expires_at'] <= int(time.time()) else 'waiting_for_xbt'}
            if gate['phase'] != 'held' or not gate['hook_ready']:
                return {'outcome': 'needs_inspection'}
            if recover_only:
                return {'outcome': 'needs_manual_start'}
            state = dict(profile=terms['profile'], phase='prepared', btc_cli=config['btc_cli'],
                xbt_cli=config['xbt_cli'], node_ids=terms['node_ids'], reverse_quote=terms,
                durable_gate=True, xbt_onchain_claim=True, xbt_deadline_guard=True,
                payment_hash=terms['payment_hash'], xbt_binding=gate['binding'],
                xbt_expiry=gate['cltv_expiry'], xbt_amount_msat=terms['xbt_amount_msat'],
                btc_amount_msat=terms['btc_amount_msat'], btc_invoice=terms['btc_invoice'],
                btc_secret=quote['btc_secret'], btc_payment_metadata=quote['btc_payment_metadata'],
                route=terms['route'], routing=terms['routing'])
            save(path, state)
        state = private_load(path)
        if (state['reverse_quote'] != terms or state['btc_cli'] != config['btc_cli']
                or state['xbt_cli'] != config['xbt_cli']):
            raise ValueError('reverse service state differs from quote')
        return controller(path, recover_only=recover_only)


def recover_record(directory, settings, rpc=RPC.call, controller=reconcile):
    quote = private_load(directory/'reverse-quote.json')
    if (quote['config'] != binding(settings)
            or quote['terms']['profile'] != settings.get('reverse_profile', PROFILE)):
        raise ValueError('recovery operator binding mismatch')
    from reverse_activation import activation
    with activation(settings):
        return step(directory, recover_only=True, rpc=rpc, controller=controller)


def abort_unspent(directory, rpc=RPC.call):
    """Only an already-prepared state can be cancelled; serialize with sender."""
    with locked(directory):
        path = directory/'reverse-state.json'
        quote = private_load(directory/'reverse-quote.json')
        enabled(quote['terms']['profile'])
        fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = private_load(path)
            from reverse_live import verify_state
            verify_state(state, rpc)
            if (state['reverse_quote'] != quote['terms']
                    or state['btc_cli'] != quote['config']['btc_cli']
                    or state['xbt_cli'] != quote['config']['xbt_cli']
                    or state['phase'] != 'prepared'):
                raise ValueError('only original unsubmitted prepared swap can be cancelled')
            attempts = rpc(state['btc_cli'], 'listsendpays')['payments']
            if any(p['payment_hash'] == state['payment_hash'] for p in attempts):
                raise ValueError('BTC attempt exists; unspent cancellation refused')
            state['pre_spend_aborted'] = True
            state['phase'] = 'btc_failed'
            save(path, state)
        finally:
            os.close(fd)
        # Reconciliation is performed after dropping controller lock. The
        # durable checkpoint prevents any sender from starting BTC meanwhile.
        return reconcile(path, recover_only=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create_parser = sub.add_parser('quote')
    create_parser.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    create_parser.add_argument('--invoice-file', type=Path, required=True)
    create_parser.add_argument('--directory', type=Path, required=True)
    for command in ('run', 'status', 'abort-unspent', 'tick', 'recover', 'offer'):
        child = sub.add_parser(command)
        child.add_argument('--directory', type=Path, required=True)
        if command == 'offer':
            child.add_argument('--output', type=Path, required=True)
        child.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = args.directory.expanduser().resolve()
        with ExitStack() as contexts:
            if args.command not in ('quote', 'status', 'offer'):
                stored = private_load(directory/'reverse-quote.json')
                if stored['terms']['profile'] == PROFILE:
                    from reverse_activation import activation, directory_settings
                    settings = directory_settings(directory, args.settings.expanduser())
                    contexts.enter_context(activation(settings))
            if args.command == 'offer':
                from reverse_customer import export
                print(json.dumps(export(directory, args.output.expanduser().resolve())))
            elif args.command == 'quote':
                quote = create(private_load(args.settings.expanduser()), private_invoice(args.invoice_file.expanduser()), directory)
                print(json.dumps({'xbt_invoice': quote['xbt_invoice'],
                    'btc_sats': quote['terms']['btc_amount_msat']//1000,
                    'xbt_sats': quote['terms']['xbt_amount_msat']//1000,
                    'expires_at': quote['terms']['expires_at']}))
            elif args.command in ('tick', 'recover'):
                print(json.dumps(step(directory, recover_only=args.command == 'recover')))
            elif args.command == 'abort-unspent':
                print(json.dumps(abort_unspent(directory)))
            elif args.command == 'status':
                path = directory/'reverse-state.json'
                state = private_load(path) if path.exists() else {}
                print(json.dumps({'phase': state.get('phase', 'not_started')}))
            else:
                while True:
                    result = step(directory)
                    print(json.dumps(result), flush=True)
                    if result.get('phase') in ('xbt_released', 'xbt_failed') or result.get('outcome') in ('quote_expired', 'needs_inspection'):
                        break
                    time.sleep(2)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(json.dumps({'event': 'reverse_service_error', 'details': 'withheld'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
