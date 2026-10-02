"""Single-swap BTC -> XBT service: regtest by default, explicit bounded live pilot."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time

from swap_rpc import RPC
from swap_controller import run as reconcile, save
from swap_invoice import unsigned_invoice
from swap_watch import watch
from customer_errors import channel_reason
from quote_refusal import QuoteRefused
import live_pilot as pilot


def emit(value):
    print(json.dumps(value), flush=True)


def invoice_from_file(path):
    # Bound input and avoid including invoice contents in decoding errors.
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('invoice file exceeds 64 KiB')
    try:
        invoice = raw.decode('ascii').strip()
    except UnicodeDecodeError:
        raise ValueError('invoice file must contain ASCII text') from None
    if not invoice or any(c.isspace() for c in invoice):
        raise ValueError('invoice file must contain one invoice')
    return invoice


def config_from(path):
    config = json.loads(path.read_text())
    if set(config) not in ({'btc_cli', 'xbt_cli'}, {'btc_cli', 'xbt_cli', 'profile'},
                            {'btc_cli', 'xbt_cli', 'profile', 'previous_state'},
                            {'btc_cli', 'xbt_cli', 'profile', 'market'}):
        raise ValueError('config needs btc_cli, xbt_cli and optionally profile')
    pilot.is_live(config)
    if (config.get('profile') == pilot.PROFILE_V2) != ('previous_state' in config):
        raise ValueError('v2 requires previous_state; other profiles must omit it')
    if (config.get('profile') in pilot.MARKET_PROFILES) != ('market' in config):
        raise ValueError('market profile requires market policy')
    if 'market' in config:
        from market_policy import policy
        policy(config)
    for cli in (config['btc_cli'], config['xbt_cli']):
        if (not isinstance(cli, list) or not cli or
                any(not isinstance(arg, str) or not arg for arg in cli)
                or not Path(cli[0]).is_absolute()):
            raise ValueError('CLI must be an argument array with an absolute executable path')
    return config


def identities(config):
    btc = RPC.call(config['btc_cli'], 'getinfo')
    xbt = RPC.call(config['xbt_cli'], 'getinfo')
    networks = ('bitcoin', 'xbt') if pilot.is_live(config) else ('regtest', 'xbt-regtest')
    if (btc['network'], xbt['network']) != networks:
        raise ValueError('operator networks do not match explicit profile')
    if btc['id'] == xbt['id']:
        raise ValueError('operator nodes must have distinct identities')
    return [btc['id'], xbt['id']]


def create(config, invoice, btc_sats, directory):
    market = config.get('profile') in pilot.MARKET_PROFILES
    if market and btc_sats is not None:
        raise ValueError('market quotes obtain BTC amount only from the oracle')
    if not market and (type(btc_sats) is not int or not 0 < btc_sats <= 2100000000000000):
        raise ValueError('BTC price must be a positive integer number of sats')
    live = pilot.is_live(config)
    btc_amount, xbt_amount = (None, None) if market else pilot.amounts(config)
    if live and not market and btc_sats * 1000 != btc_amount:
        raise ValueError('BTC amount differs from selected pilot profile')
    replacement = None
    if config.get('profile') == pilot.PROFILE_V2:
        replacement = pilot.replacement(config, RPC.call)
        pilot.incoming_preflight(config, replacement[1], RPC.call)
    ids = identities(config)
    if live:
        pilot.require_reserves(config, RPC.call)
    decoded = RPC.call(config['xbt_cli'], 'decode', invoice)
    amount = decoded.get('amount_msat')
    if (decoded.get('valid') is not True or decoded.get('type') != 'bolt11 invoice'
            or decoded.get('currency') != ('xbt' if live else 'xbtrt')
            or not invoice.startswith('lnxbt' if live else 'lnxbtrt')
            or type(amount) is not int or amount <= 0 or not decoded.get('payment_secret')):
        raise ValueError('requires a signed fixed-amount XBT invoice for the selected profile')
    if live and not market and amount != xbt_amount:
        raise ValueError('XBT amount differs from selected pilot profile')
    now = int(time.time())
    # Allow time for submission after the BTC quote expires.
    expires = min(now + 600, decoded['created_at'] + decoded['expiry'] - 60)
    if expires < now + 30:
        if market:
            raise QuoteRefused('invoice_expiring')
        raise ValueError('XBT invoice needs at least 90 seconds remaining')
    if decoded['min_final_cltv_expiry'] > 40:
        raise ValueError('receiver CLTV exceeds this experimental policy')
    all_channels = RPC.call(config['xbt_cli'], 'listpeerchannels')['channels']
    if market:
        bound = [c for c in all_channels if c.get('short_channel_id') == config['market']['xbt_channel']
                 and c['peer_id'] == decoded['payee']]
        reason = channel_reason(bound, 'xbt', amount)
        if reason:
            raise QuoteRefused(reason)
    channels = [c for c in all_channels
                if c['peer_id'] == decoded['payee'] and c['state'] == 'CHANNELD_NORMAL'
                and c.get('short_channel_id') and c.get('spendable_msat', 0) >= amount]
    if len(channels) != 1:
        raise ValueError('requires one usable direct XBT channel to invoice payee')
    if live:
        if not channels[0]['peer_connected']:
            raise ValueError('XBT peer disconnected')
        pilot.require_untrimmed(channels[0], amount)
    attempts = RPC.call(config['xbt_cli'], 'listsendpays')['payments']
    if any(p['payment_hash'] == decoded['payment_hash'] for p in attempts):
        raise ValueError('XBT invoice already has an outgoing attempt')
    audit = None
    if market:
        from market_policy import prepare
        audit = prepare(config, decoded, channels[0], RPC.call)
        btc_sats = audit['btc_sats']
        btc_amount = btc_sats * 1000
        expires = min(expires, int(time.time()) + 120)
    terms = {'payment_hash': decoded['payment_hash'], 'payment_secret': secrets.token_hex(32),
             'btc_amount_msat': btc_sats * 1000, 'xbt_amount_msat': amount,
             'xbt_invoice': invoice, 'expires_at': expires, 'min_cltv_delta': 100,
             'max_cltv_delta': 2000}
    if live:
        terms.update(pilot=config['profile'], min_cltv_delta=pilot.MIN_CLTV,
                     max_cltv_delta=pilot.MAX_CLTV)
    if replacement:
        if decoded['payment_hash'] == replacement[0]:
            raise ValueError('replacement needs a new invoice hash')
        terms.update(replaces=replacement[0], btc_channel=replacement[1])
    if market:
        from market_policy import digest
        terms.update(oracle_digest=digest(audit), controller_id=secrets.token_hex(32))
        if config['profile'] == pilot.PROFILE_MARKET_ANY:
            from incoming_btc import POLICY
            terms['btc_channel_policy'] = POLICY
        else:
            terms['btc_channel'] = config['market']['btc_channel']
    template = dict(config, phase='prepared', quote_gate=True, btc_deadline_guard=True,
                    payment_hash=decoded['payment_hash'], payment_secret=decoded['payment_secret'],
                    xbt_invoice=invoice, xbt_amount_msat=amount,
                    route=[{'id': decoded['payee'], 'channel': channels[0]['short_channel_id'],
                            'amount_msat': amount, 'delay': 40}])
    if live:
        template.update(node_ids=ids, btc_amount_msat=btc_amount)
        if replacement:
            template.update(btc_channel=replacement[1])
    if market:
        template.update(oracle=audit, oracle_digest=terms['oracle_digest'], controller_id=terms['controller_id'])
        key = 'btc_channel_policy' if config['profile'] == pilot.PROFILE_MARKET_ANY else 'btc_channel'
        template[key] = terms[key]
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Save everything needed to retry registration/signing before any mutation.
    save(directory / 'quote.json', {'config': config, 'node_ids': ids, 'terms': terms,
                                    'controller': template})


def publication_preflight(data):
    terms = data['terms']
    if data['config'].get('profile') in pilot.MARKET_PROFILES:
        from market_policy import publication
        publication(data, RPC.call)
    if data['config'].get('profile') == pilot.PROFILE_V2:
        previous, channel = pilot.replacement(data['config'], RPC.call)
        if terms.get('replaces') != previous or terms.get('btc_channel') != channel:
            raise RuntimeError('replacement quote binding mismatch')
        pilot.incoming_preflight(data['config'], channel, RPC.call)
        pilot.require_reserves(data['config'], RPC.call)
        route = data['controller']['route'][0]
        channels = [c for c in RPC.call(data['config']['xbt_cli'], 'listpeerchannels')['channels']
                    if c.get('short_channel_id') == route['channel'] and c['peer_id'] == route['id']]
        if (len(channels) != 1 or channels[0]['state'] != 'CHANNELD_NORMAL'
                or not channels[0]['peer_connected']
                or channels[0]['spendable_msat'] < terms['xbt_amount_msat']):
            raise RuntimeError('XBT channel not ready for publication')
        pilot.require_untrimmed(channels[0], terms['xbt_amount_msat'])


def publish(directory, renewing=False):
    path = directory / 'quote.json'
    data = json.loads(path.read_text())
    if identities(data['config']) != data['node_ids']:
        raise RuntimeError('configured operator identity changed')
    if data.get('renewal') and not data['renewal'].get('complete') and not renewing:
        raise RuntimeError('finish pending renewal with the renew command')
    terms = data['terms']
    live = pilot.is_live(data['config'])
    if terms['expires_at'] <= int(time.time()):
        raise RuntimeError('quote expired')
    if 'btc_invoice' not in data:
        publication_preflight(data)
        cli = data['config']['btc_cli']
        if RPC.call(cli, 'xbt-register', json.dumps(terms)) != {'registered': True}:
            raise RuntimeError('quote registration failed')
        unsigned = unsigned_invoice(terms['payment_hash'], terms['payment_secret'],
                                    terms['btc_amount_msat'], terms['expires_at'] - int(time.time()),
                                    currency='bc' if live else 'bcrt',
                                    final_cltv=pilot.INVOICE_CLTV if live else 120)
        signed = RPC.call(cli, 'signinvoice', unsigned)['bolt11']
        decoded = RPC.call(cli, 'decode', signed)
        expected = {'valid': True, 'currency': 'bc' if live else 'bcrt', 'payee': data['node_ids'][0],
                    'payment_hash': terms['payment_hash'], 'payment_secret': terms['payment_secret'],
                    'amount_msat': terms['btc_amount_msat'],
                    'min_final_cltv_expiry': pilot.INVOICE_CLTV if live else 120}
        if any(decoded.get(k) != v for k, v in expected.items()):
            raise RuntimeError('signed BTC invoice does not match quote')
        data['btc_invoice'] = signed
        save(path, data)
    return {'btc_invoice': data['btc_invoice'], 'payment_hash': terms['payment_hash'],
            'btc_sats': terms['btc_amount_msat'] // 1000,
            'xbt_msat': terms['xbt_amount_msat'], 'expires_at': terms['expires_at']}


def renew(directory):
    path = directory / 'quote.json'
    data = json.loads(path.read_text())
    if data['config'].get('profile') != pilot.PROFILE_V2:
        raise RuntimeError('renewal is only for the unused v2 quote')
    if (directory / 'state.json').exists():
        raise RuntimeError('controller state exists; renewal refused')
    if identities(data['config']) != data['node_ids']:
        raise RuntimeError('configured operator identity changed')
    if data.get('renewal', {}).get('complete'):
        return publish(directory)  # Reprint only; never extend twice.
    terms = data['terms']
    cli = data['config']['btc_cli']
    status = RPC.call(cli, 'xbt-quote-status', terms['payment_hash'])
    if (status['payment_hash'] != terms['payment_hash'] or status['phase'] != 'quoted'
            or status.get('binding') is not None):
        raise RuntimeError('quote was accepted or resolved; renewal refused')
    outgoing = RPC.call(data['config']['xbt_cli'], 'listsendpays')['payments']
    if any(p['payment_hash'] == terms['payment_hash'] for p in outgoing):
        raise RuntimeError('XBT attempt exists; renewal refused')
    publication_preflight(data)
    decoded = RPC.call(data['config']['xbt_cli'], 'decode', terms['xbt_invoice'])
    expected = {'valid': True, 'currency': 'xbt', 'payment_hash': terms['payment_hash'],
                'amount_msat': terms['xbt_amount_msat'],
                'payee': data['controller']['route'][0]['id'],
                'payment_secret': data['controller']['payment_secret']}
    if any(decoded.get(k) != v for k, v in expected.items()):
        raise RuntimeError('original XBT invoice mismatch')
    now = int(time.time())
    if 'renewal' not in data:
        if terms['expires_at'] > now or 'btc_invoice' not in data:
            raise RuntimeError('renewal requires an expired published quote')
        expires = min(now + 600, decoded['created_at'] + decoded['expiry'] - 60)
        if expires < now + 30:
            raise RuntimeError('original XBT invoice has insufficient time remaining')
        data['renewal'] = {'old_terms': dict(terms), 'old_btc_invoice': data['btc_invoice'],
                           'new_expiry': expires, 'complete': False}
        save(path, data)  # Journal before a potentially interrupted gate RPC.
    journal = data['renewal']
    if (journal['new_expiry'] <= now
            or journal['new_expiry'] > decoded['created_at'] + decoded['expiry'] - 60):
        raise RuntimeError('renewal window expired; preserve state for inspection')
    result = RPC.call(cli, 'xbt-renew', json.dumps(journal['old_terms']), journal['new_expiry'])
    if result != {'renewed': True}:
        raise RuntimeError('unexpected gate renewal response')
    data['terms'] = dict(journal['old_terms'], expires_at=journal['new_expiry'])
    data.pop('btc_invoice', None)
    save(path, data)
    report = publish(directory, renewing=True)
    data = json.loads(path.read_text())
    data['renewal']['complete'] = True
    save(path, data)
    return report


def serve(directory, stop, report=None):
    report = emit if report is None else report
    data = json.loads((directory / 'quote.json').read_text())
    if data.get('renewal') and not data['renewal'].get('complete'):
        raise RuntimeError('finish pending renewal before running the service')
    if identities(data['config']) != data['node_ids']:
        raise RuntimeError('configured operator identity changed')
    if 'btc_invoice' not in data:
        raise RuntimeError('finish quote publication first with the invoice command')
    path = directory / 'state.json'
    cli = data['config']['btc_cli']
    payment_hash = data['terms']['payment_hash']
    while not stop.is_set() and not path.exists():
        try:
            status = RPC.call(cli, 'xbt-quote-status', payment_hash)
            if status['payment_hash'] != payment_hash:
                raise RuntimeError('quote identity mismatch')
            if status['phase'] == 'held':
                binding = status['binding']
                channels = RPC.call(cli, 'listpeerchannels')['channels']
                committed = any(c.get('short_channel_id') == binding[0] and
                                any(h['id'] == binding[1] and h['direction'] == 'in'
                                    and h['payment_hash'] == payment_hash
                                    and h['state'] == 'RCVD_ADD_ACK_REVOCATION'
                                    for h in c.get('htlcs', [])) for c in channels)
                if committed:
                    outgoing = RPC.call(data['config']['xbt_cli'], 'listsendpays')['payments']
                    if any(p['payment_hash'] == payment_hash for p in outgoing):
                        raise RuntimeError('outgoing attempt exists without controller state; restore original state')
                    save(path, dict(data['controller'], btc_binding=binding))
                    break
            elif status['phase'] != 'quoted':
                raise RuntimeError('quote terminal without controller state; inspect nodes')
            elif data['terms']['expires_at'] <= int(time.time()):
                raise RuntimeError('unpaid quote expired')
            report({'event': 'waiting_for_btc', 'payment_hash': payment_hash})
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            report({'event': 'rpc_retry', 'error': type(exc).__name__})
        stop.wait(1)
    if stop.is_set():
        return 0
    # Only the original prepared checkpoint can submit. The controller saves
    # outgoing_started before sendpay; a lost reply is reconciled, never resent.
    if json.loads(path.read_text())['phase'] == 'prepared':
        try:
            result = reconcile(path)
            if result.get('outcome') == 'refused':
                report(result)
                return 1  # No spend. Keep binding for deliberate inspection/cleanup.
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if json.loads(path.read_text())['phase'] == 'prepared':
                raise  # Pre-spend RPC failure; safe to restart run.
    return watch(path, stop=stop, emit=report)


def status(directory):
    data = json.loads((directory / 'quote.json').read_text())
    path = directory / 'state.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    return {'payment_hash': data['terms']['payment_hash'],
            'phase': state.get('phase', 'waiting_for_btc' if 'btc_invoice' in data else 'unpublished'),
            'quote_expired': data['terms']['expires_at'] <= int(time.time()),
            'btc_close_requested': 'btc_close_intent' in state}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('quote', 'invoice', 'renew', 'run', 'status', 'market-check', 'quote-market'):
        command = commands.add_parser(name)
        command.add_argument('--directory', required=True, type=Path)
        if name == 'market-check':
            command.add_argument('--margin-bps', type=int, default=0)
        if name in ('quote', 'quote-market'):
            command.add_argument('--config', required=True, type=Path)
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument('--xbt-invoice')
            source.add_argument('--xbt-invoice-file', type=Path,
                                help='Read an invoice without placing it in shell history.')
            if name == 'quote':
                command.add_argument('--btc-sats', required=True, type=int)
    args = parser.parse_args()
    directory = args.directory.resolve()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        if args.command == 'market-check':
            from market_check import check
            emit(check(directory, args.margin_bps))
            return 0
        if args.command in ('quote', 'quote-market'):
            config = config_from(args.config)
            if (args.command == 'quote-market') != (config.get('profile') in pilot.MARKET_PROFILES):
                raise ValueError('quote command does not match profile')
            invoice = (invoice_from_file(args.xbt_invoice_file)
                       if args.xbt_invoice_file is not None else args.xbt_invoice)
            create(config, invoice, getattr(args, 'btc_sats', None), directory)
        if args.command == 'status':
            emit(status(directory))
            return 0
        # Stable per-directory lock serializes publication and service startup.
        fd = os.open(directory / 'service.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == 'renew':
                emit(renew(directory))
                return 0
            if args.command in ('quote', 'quote-market', 'invoice'):
                emit(publish(directory))
                return 0
            return serve(directory, stop)
        finally:
            os.close(fd)
    except BlockingIOError:
        emit({'event': 'busy'})
        return 1
    except Exception as exc:
        # RPC arguments/output may contain secrets; never dump those here.
        emit({'event': 'error', 'error': type(exc).__name__,
              'reason': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else 'inspect node logs'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
