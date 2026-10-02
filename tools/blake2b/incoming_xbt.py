"""Unbound incoming XBT admission, gated to service-regtest until live migration."""
import re
from live_pilot import require_untrimmed
from quote_refusal import QuoteRefused

POLICY = 'any-normal-v1'
KEYS = ('channel_id', 'funding_txid', 'funding_outnum', 'peer_id', 'short_channel_id')


def unbound(terms):
    if not isinstance(terms, dict) or 'incoming_policy' not in terms:
        return False
    if (terms['incoming_policy'] != POLICY or terms.get('profile') != 'reverse-service-regtest-v1'
            or 'xbt_channel' in terms or 'payer_id' in terms):
        raise ValueError('unbound reverse policy requires service regtest and no fixed payer')
    return True


def eligible(channels, amount):
    result = []
    for c in channels:
        if (c.get('state') != 'CHANNELD_NORMAL' or c.get('peer_connected') is not True
                or c.get('htlcs') or not c.get('short_channel_id')
                or type(c.get('receivable_msat')) is not int or c['receivable_msat'] < amount):
            continue
        try:
            require_untrimmed(c, amount)
        except (KeyError, TypeError, RuntimeError):
            continue
        result.append(c)
    if not result:
        raise QuoteRefused('insufficient_xbt_liquidity')
    return result


def validate_pin(state, channel=None):
    if not unbound(state['reverse_quote']):
        raise RuntimeError('unbound incoming policy required')
    pin, binding = state.get('incoming_channel'), state['xbt_binding']
    if (not isinstance(pin, dict) or set(pin) != set(KEYS)
            or not isinstance(binding, list) or len(binding) != 2
            or type(binding[1]) is not int or binding[1] < 0
            or pin['short_channel_id'] != binding[0]
            or not isinstance(pin['channel_id'], str) or not re.fullmatch('[0-9a-f]{64}', pin['channel_id'])
            or not isinstance(pin['funding_txid'], str) or not re.fullmatch('[0-9a-f]{64}', pin['funding_txid'])
            or type(pin['funding_outnum']) is not int or not 0 <= pin['funding_outnum'] <= 65535
            or not isinstance(pin['peer_id'], str) or not re.fullmatch('0[23][0-9a-f]{64}', pin['peer_id'])):
        raise RuntimeError('original incoming XBT funding pin missing or changed')
    if channel is not None and any(channel.get(k) != pin[k] for k in KEYS):
        raise RuntimeError('original incoming XBT funding output changed')


def pin(state, rpc):
    if not unbound(state['reverse_quote']) or 'incoming_channel' in state:
        raise RuntimeError('incoming XBT pin must be captured exactly once')
    binding = state['xbt_binding']
    if not isinstance(binding, list) or len(binding) != 2:
        raise RuntimeError('incoming XBT binding malformed')
    channels = [c for c in rpc(state['xbt_cli'], 'listpeerchannels')['channels']
                if c.get('short_channel_id') == binding[0]]
    if len(channels) != 1:
        raise RuntimeError('incoming XBT channel unavailable or ambiguous')
    c = channels[0]
    htlcs = [h for h in c.get('htlcs', []) if h.get('id') == binding[1] and h.get('direction') == 'in']
    expected = dict(payment_hash=state['payment_hash'], amount_msat=state['xbt_amount_msat'],
                    expiry=state['xbt_expiry'], state='RCVD_ADD_ACK_REVOCATION')
    if (c.get('state') != 'CHANNELD_NORMAL' or c.get('peer_connected') is not True
            or len(htlcs) != 1 or any(htlcs[0].get(k) != v for k,v in expected.items())
            or htlcs[0].get('local_trimmed', False)):
        raise RuntimeError('incoming XBT HTLC not committed and enforceable')
    require_untrimmed(c, state['xbt_amount_msat'])
    candidate = dict(state, incoming_channel={k:c[k] for k in KEYS})
    validate_pin(candidate, c)
    state['incoming_channel'] = candidate['incoming_channel']
