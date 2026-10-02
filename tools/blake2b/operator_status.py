"""Read-only, identifier-free operator health and liquidity summary."""
import argparse
from collections import Counter
import http.client
import json
from pathlib import Path
import subprocess
import time

from service_manager import private_load
from swap_rpc import RPC

SERVICES = ('cln-btc-operator', 'cln-xbt-operator', 'cln-swap-recovery', 'cln-swap-quotes')


def service_states():
    p = subprocess.run(['systemctl', '--user', 'is-active', *SERVICES],
                       capture_output=True, text=True, timeout=10)
    rows = p.stdout.splitlines()
    allowed = {'active', 'inactive', 'failed', 'activating', 'deactivating', 'unknown'}
    return {name: rows[i] if i < len(rows) and rows[i] in allowed else 'unknown'
            for i, name in enumerate(SERVICES)}


def api_probe(port):
    # No credentials, quote request, or operator action. Check the auth boundary.
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('POST', '/v1/quote', body=b'{}',
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        return {'reachable': True, 'unauthenticated_request_rejected': response.status == 401}
    except (OSError, http.client.HTTPException):
        return {'reachable': False, 'unauthenticated_request_rejected': None}
    finally:
        connection.close()


def channel_summary(channels):
    states = Counter()
    spendable = receivable = pending = connected = ready = 0
    for c in channels:
        state = c.get('state')
        # Never copy arbitrary backend strings into public output.
        states['normal' if state == 'CHANNELD_NORMAL' else 'other'] += 1
        pending += len(c.get('htlcs', []))
        if state != 'CHANNELD_NORMAL' or c.get('peer_connected') is not True:
            continue
        connected += 1
        if c.get('htlcs'):
            continue
        ready += 1
        for key in ('spendable_msat', 'receivable_msat'):
            value = c.get(key)
            if type(value) is not int or value < 0:
                raise ValueError('missing capacity')
        spendable += c['spendable_msat']
        receivable += c['receivable_msat']
    return dict(normal_channels=states['normal'], other_channels=states['other'],
                connected_normal_channels=connected, clear_connected_channels=ready,
                pending_htlcs=pending, clear_channel_spendable_sats=spendable//1000,
                clear_channel_receivable_sats=receivable//1000)


def summarize(settings, settings_dir, *, rpc=RPC.call, probe=api_probe,
              services=service_states, now=time.time, port=19840):
    answer = dict(read_only=True, checked_at=int(now()), route_checked=False)
    try:
        answer['services'] = services()
    except Exception:
        answer['services_available'] = False
    answer['quote_api'] = probe(port)
    nodes = {}
    for index, (role, network) in enumerate((('btc', 'bitcoin'), ('xbt', 'xbt'))):
        try:
            cli = settings[role+'_cli']
            info = rpc(cli, 'getinfo')
            if info['id'] != settings['node_ids'][index] or info['network'] != network:
                raise ValueError('identity mismatch')
            channels = rpc(cli, 'listpeerchannels')['channels']
            nodes[role] = dict(rpc_ready=True,
                              warning_present=any(k.startswith('warning_') for k in info),
                              **channel_summary(channels))
            if role == 'xbt':
                nodes[role]['bound_customer_channel'] = channel_summary(
                    [c for c in channels if c.get('peer_id') == settings['receiver_id']])
        except Exception:
            nodes[role] = dict(rpc_ready=False, inspection_required=True)
    answer['nodes'] = nodes
    try:
        health = private_load(Path(settings_dir)/'health.json')
        stamp = health['checked_at']
        if type(stamp) is not int:
            raise ValueError()
        age = int(now())-stamp
        reports = health['swaps']
        attention = sum(r.get('outcome') in ('needs_inspection', 'needs_manual_resume',
                        'needs_manual_start', 'authorization_expired') for r in reports)
        answer['recovery_snapshot'] = dict(available=True, age_seconds=age,
            stale=not 0 <= age <= 30, nonterminal_reports=len(reports),
            attention_reports=attention)
    except Exception:
        answer['recovery_snapshot'] = dict(available=False, stale=True)
    counts = Counter()
    root = Path(settings['swap_root'])/'api-requests'
    try:
        if root.is_symlink():
            raise ValueError()
        for path in root.glob('*.json'):
            try:
                phase = private_load(path).get('phase')
                counts[phase if phase in ('creating', 'quoted', 'refused') else 'unreadable'] += 1
            except Exception:
                counts['unreadable'] += 1
        answer['quote_requests'] = dict(available=True,
            creating_or_uncertain=counts['creating'], refused=counts['refused'],
            published=counts['quoted'], unreadable=counts['unreadable'])
    except Exception:
        answer['quote_requests'] = dict(available=False)
    return answer


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings', type=Path, default=Path.home()/'.config/cln-swaps/settings.json')
    p.add_argument('--api-port', type=int, default=19840)
    args = p.parse_args()
    try:
        if not 1024 <= args.api_port <= 65535:
            raise ValueError()
        path = args.settings.expanduser()
        print(json.dumps(summarize(private_load(path), path.parent, port=args.api_port), indent=2))
        return 0
    except Exception:
        print(json.dumps(dict(event='operator_status_unavailable', details='withheld', read_only=True)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
