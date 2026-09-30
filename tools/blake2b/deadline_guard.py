"""Opt-in BTC regtest close policy, run during pending reconciliation.

Thirty BTC blocks is a fixture threshold, not a cross-chain safety guarantee.
The caller holds the controller lock. No payment is failed or resent here.
"""


def protect(path, state, rpc, save):
    if not state.get('btc_deadline_guard'):
        return
    if not state.get('quote_gate') or state['phase'] != 'outgoing_started':
        raise RuntimeError('deadline guard requires a pending quoted swap')
    cli, payment_hash = state['btc_cli'], state['payment_hash']
    status = rpc(cli, 'xbt-quote-status', payment_hash)
    if (status['payment_hash'] != payment_hash or status['binding'] != state['btc_binding']
            or status['phase'] != 'held'):
        raise RuntimeError('deadline quote binding or phase mismatch')
    info = rpc(cli, 'getinfo')
    if info['network'] != 'regtest':
        raise RuntimeError('deadline guard is regtest only')
    channels = rpc(cli, 'listpeerchannels')['channels']
    intent = state.get('btc_close_intent')
    if intent is None:
        spend = rpc(cli, 'xbt-spend-info', payment_hash)
        if spend['payment_hash'] != payment_hash or spend['binding'] != state['btc_binding']:
            raise RuntimeError('deadline held HTLC binding mismatch')
        if spend['cltv_expiry'] - info['blockheight'] > 30:
            return
        matches = [c for c in channels if c.get('short_channel_id') == state['btc_binding'][0]]
        if len(matches) != 1:
            raise RuntimeError('deadline channel missing or ambiguous')
        channel = matches[0]
        htlcs = [h for h in channel.get('htlcs', [])
                 if h['id'] == state['btc_binding'][1] and h['direction'] == 'in'
                 and h['payment_hash'] == payment_hash and h['expiry'] == spend['cltv_expiry']]
        if len(htlcs) != 1 or channel['state'] != 'CHANNELD_NORMAL':
            raise RuntimeError('deadline needs the original incoming HTLC on a normal channel')
        intent = {'channel_id': channel['channel_id'], 'binding': state['btc_binding'],
                  'payment_hash': payment_hash, 'expiry': spend['cltv_expiry']}
        state['btc_close_intent'] = intent
        save(path, state)  # Persist exact target before a potentially interrupted RPC.
    else:
        if intent['binding'] != state['btc_binding'] or intent['payment_hash'] != payment_hash:
            raise RuntimeError('deadline close intent mismatch')
        matches = [c for c in channels if c['channel_id'] == intent['channel_id']]
        if len(matches) != 1:
            raise RuntimeError('deadline close target missing or ambiguous')
        channel = matches[0]
    if channel['state'] in ('AWAITING_UNILATERAL', 'FUNDING_SPEND_SEEN', 'ONCHAIN'):
        return  # CLN owns the close even if the controller lost its RPC reply.
    if channel['state'] not in ('CHANNELD_NORMAL', 'CHANNELD_SHUTTING_DOWN'):
        raise RuntimeError('unexpected deadline channel state; inspect node state')
    if (channel.get('short_channel_id') != intent['binding'][0]
            or not any(h['id'] == intent['binding'][1] and h['direction'] == 'in'
                       and h['payment_hash'] == payment_hash and h['expiry'] == intent['expiry']
                       for h in channel.get('htlcs', []))):
        raise RuntimeError('original incoming HTLC missing before close retry')
    result = rpc(cli, 'close', intent['channel_id'], 1)
    if result.get('type') != 'unilateral':
        raise RuntimeError('deadline close did not report unilateral commitment')
    state['btc_close_result'] = result
    save(path, state)
