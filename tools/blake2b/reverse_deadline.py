"""Opt-in XBT deadline close for the fixed reverse regtest profile.

Called under the controller lock only after the original BTC attempt is
verified pending. Thirty XBT blocks is a test policy, not a guarantee about
independent live chains. This module never sends or resolves a payment.
"""


def protect(path, state, rpc, save, gate):
    if state.get('xbt_deadline_guard') is not True:
        return
    if (state.get('profile') != 'reverse-regtest-v1' or state['phase'] != 'outgoing_started'
            or not state.get('durable_gate') or state.get('xbt_onchain_claim') is not True):
        raise RuntimeError('reverse deadline requires a pending durable regtest swap')
    cli = state['xbt_cli']
    info = rpc(cli, 'getinfo')
    if info['network'] != 'xbt-regtest' or info['id'] != state['node_ids'][0]:
        raise RuntimeError('reverse deadline operator identity mismatch')
    # The controller also checks full immutable quote terms via gate_status.
    if (gate['phase'] != 'held' or gate['payment_hash'] != state['payment_hash']
            or gate['binding'] != state['xbt_binding'] or gate['cltv_expiry'] != state['xbt_expiry']):
        raise RuntimeError('reverse deadline quote binding mismatch')
    intent = state.get('xbt_close_intent')
    if intent is None and state['xbt_expiry'] - info['blockheight'] > 30:
        return
    pin = state.get('incoming_channel')
    keys = {'channel_id', 'funding_txid', 'funding_outnum', 'peer_id', 'short_channel_id'}
    if not isinstance(pin, dict) or set(pin) != keys or pin['short_channel_id'] != state['xbt_binding'][0]:
        raise RuntimeError('reverse deadline original channel pin missing')
    matches = [c for c in rpc(cli, 'listpeerchannels')['channels'] if c.get('channel_id') == pin['channel_id']]
    if len(matches) != 1 or any(matches[0].get(k) != pin[k] for k in keys):
        raise RuntimeError('reverse deadline original channel changed')
    channel = matches[0]
    expected = dict(channel=pin, binding=state['xbt_binding'], payment_hash=state['payment_hash'],
                    expiry=state['xbt_expiry'])
    if intent is not None and intent != expected:
        raise RuntimeError('reverse deadline close intent changed')
    if channel['state'] in ('AWAITING_UNILATERAL', 'FUNDING_SPEND_SEEN', 'ONCHAIN'):
        return  # CLN owns the close, including a lost close RPC reply.
    if channel['state'] not in ('CHANNELD_NORMAL', 'CHANNELD_SHUTTING_DOWN'):
        raise RuntimeError('unexpected reverse deadline channel state')
    htlcs = [h for h in channel.get('htlcs', [])
             if h['id'] == state['xbt_binding'][1] and h['direction'] == 'in']
    expected_htlc = dict(payment_hash=state['payment_hash'], amount_msat=state['xbt_amount_msat'],
                         expiry=state['xbt_expiry'], state='RCVD_ADD_ACK_REVOCATION')
    if len(htlcs) != 1 or any(htlcs[0].get(k) != v for k, v in expected_htlc.items()):
        raise RuntimeError('reverse deadline original incoming HTLC changed')
    if intent is None:
        state['xbt_close_intent'] = expected
        save(path, state)  # Exact channel and HTLC before the close mutation.
    result = rpc(cli, 'close', pin['channel_id'], 1)
    if result.get('type') != 'unilateral':
        raise RuntimeError('reverse deadline close outcome uncertain')
    state['xbt_close_result'] = result
    save(path, state)
