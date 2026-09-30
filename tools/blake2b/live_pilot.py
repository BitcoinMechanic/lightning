"""Explicit one-quote live pilot policy; not a general exchange service."""
import fcntl
import json
import os
from pathlib import Path

PROFILE = 'live-pilot-v1'
PROFILE_V2 = 'live-pilot-v2'
PROFILE_MARKET = 'live-market-v1'
BTC_MSAT = 1000000
XBT_MSAT = 2000000
MIN_CLTV = 288
MAX_CLTV = 2016
INVOICE_CLTV = 300
CLOSE_BLOCKS = 72


def is_live(data):
    profile = data.get('profile', 'regtest')
    if profile not in ('regtest', PROFILE, PROFILE_V2, PROFILE_MARKET):
        raise ValueError('unknown swap profile')
    return profile in (PROFILE, PROFILE_V2, PROFILE_MARKET)


def amounts(data):
    if data.get('profile') == PROFILE_MARKET:
        from market_policy import state_amounts
        return state_amounts(data)
    return (2000000, 4000000) if data.get('profile') == PROFILE_V2 else (BTC_MSAT, XBT_MSAT)


def replacement(config, rpc):
    """Prove v1 was aborted before spending; never reset the old records."""
    path = Path(config['previous_state'])
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError('replacement requires the original absolute state path')
    path = path.resolve()
    locks = []
    try:
        for name in (path.parent / 'service.lock', Path(str(path) + '.lock')):
            fd = os.open(name, os.O_CREAT | os.O_RDWR, 0o600)
            locks.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = json.loads(path.read_text())
        if (old.get('profile') != PROFILE or old.get('phase') != 'btc_failed'
                or old.get('pre_spend_aborted') is not True or 'preimage' in old
                or old['btc_cli'] != config['btc_cli'] or old['xbt_cli'] != config['xbt_cli']):
            raise RuntimeError('previous swap is not an eligible pre-spend cancellation')
        verify_state(old, rpc)
        payment_hash = old['payment_hash']
        status = rpc(config['btc_cli'], 'xbt-quote-status', payment_hash)
        if (status['payment_hash'] != payment_hash or status['phase'] != 'failed'
                or status['binding'] != old['btc_binding']):
            raise RuntimeError('previous BTC gate failure is not confirmed')
        attempts = rpc(config['xbt_cli'], 'listsendpays')['payments']
        if any(p['payment_hash'] == payment_hash for p in attempts):
            raise RuntimeError('previous XBT attempt exists; replacement refused')
        channels = rpc(config['btc_cli'], 'listpeerchannels')['channels']
        if any(h['payment_hash'] == payment_hash for c in channels for h in c.get('htlcs', [])):
            raise RuntimeError('previous BTC HTLC still pending')
        return payment_hash, old['btc_binding'][0]
    finally:
        for fd in reversed(locks):
            os.close(fd)


def incoming_preflight(config, channel_id, rpc, amount_msat=None):
    channels = [c for c in rpc(config['btc_cli'], 'listpeerchannels')['channels']
                if c.get('short_channel_id') == channel_id]
    btc_amount = amounts(config)[0] if amount_msat is None else amount_msat
    if (len(channels) != 1 or channels[0]['state'] != 'CHANNELD_NORMAL'
            or not channels[0]['peer_connected'] or channels[0].get('htlcs')
            or channels[0]['receivable_msat'] < btc_amount):
        raise RuntimeError('original BTC channel not ready for replacement quote')
    require_untrimmed(channels[0], btc_amount)


def verify_nodes(data, rpc):
    ids = []
    for key, network in (('btc_cli', 'bitcoin'), ('xbt_cli', 'xbt')):
        info = rpc(data[key], 'getinfo')
        if info['network'] != network:
            raise RuntimeError('live pilot network mismatch')
        ids.append(info['id'])
    if ids != data['node_ids'] or ids[0] == ids[1]:
        raise RuntimeError('live pilot operator identity mismatch')


def verify_state(data, rpc):
    if not is_live(data):
        return
    btc_amount, xbt_amount = amounts(data)
    if (data.get('quote_gate') is not True or data.get('btc_deadline_guard') is not True
            or data.get('xbt_amount_msat') != xbt_amount
            or data.get('btc_amount_msat') != btc_amount):
        raise RuntimeError('live pilot limits or guards changed')
    verify_nodes(data, rpc)


def require_reserves(data, rpc):
    for key in ('btc_cli', 'xbt_cli'):
        info = rpc(data[key], 'getinfo')
        if any(k.startswith('warning_') for k in info):
            raise RuntimeError('live pilot node reports a warning')
        funds = rpc(data[key], 'listfunds')['outputs']
        amount = sum(o['amount_msat'] for o in funds
                     if o['status'] == 'confirmed' and not o['reserved'])
        if amount < 50000000:
            raise RuntimeError('live pilot needs 50000 confirmed unreserved sats on each operator')


def require_untrimmed(channel, amount):
    # Conservative non-anchor success weight also covers smaller timeout txs.
    # This is a current-fee check, not protection against future fee increases.
    fee = channel['feerate']['perkw']
    dust = channel['dust_limit_msat']
    if type(fee) is not int or type(dust) is not int or fee <= 0 or dust < 0:
        raise RuntimeError('invalid live channel fee data')
    if amount <= dust + ((703 * fee + 999) // 1000) * 1000:
        raise RuntimeError('pilot amount too small for current channel fees')


def check_channels(state, rpc):
    btc_amount, xbt_amount = amounts(state)
    if (state.get('profile') in (PROFILE_V2, PROFILE_MARKET)
            and state.get('btc_channel') != state['btc_binding'][0]):
        raise RuntimeError('incoming channel differs from preflight binding')
    incoming = [c for c in rpc(state['btc_cli'], 'listpeerchannels')['channels']
                if c.get('short_channel_id') == state['btc_binding'][0]]
    route = state['route']
    if len(incoming) != 1 or len(route) != 1 or route[0]['delay'] != 40:
        raise RuntimeError('live pilot channel binding mismatch')
    c = incoming[0]
    matches = [h for h in c.get('htlcs', []) if h['id'] == state['btc_binding'][1]
               and h['direction'] == 'in' and h['payment_hash'] == state['payment_hash']]
    if (c['state'] != 'CHANNELD_NORMAL' or not c['peer_connected']
            or len(matches) != 1 or matches[0].get('local_trimmed', False)
            or matches[0]['state'] != 'RCVD_ADD_ACK_REVOCATION'
            or matches[0]['amount_msat'] != btc_amount):
        raise RuntimeError('live BTC HTLC not committed and enforceable')
    require_untrimmed(c, btc_amount)
    outgoing = [c for c in rpc(state['xbt_cli'], 'listpeerchannels')['channels']
                if c.get('short_channel_id') == route[0]['channel']
                and c['peer_id'] == route[0]['id']]
    if (len(outgoing) != 1 or outgoing[0]['state'] != 'CHANNELD_NORMAL'
            or not outgoing[0]['peer_connected']
            or outgoing[0]['spendable_msat'] < xbt_amount):
        raise RuntimeError('live XBT channel not ready')
    require_untrimmed(outgoing[0], xbt_amount)
