"""Static customer diagnostics. Never serialize exception text or RPC replies."""
MESSAGES = {
    'api_unreachable': ('Cannot connect to the quote API.', 'Check the SSH tunnel and quote service, then resume the same attempt.'),
    'api_credentials': ('The quote API rejected the credential.', 'Check the installed customer credential. Preserve and resume the same attempt.'),
    'api_outcome_unknown': ('The quote request outcome is unknown.', 'Preserve the attempt and resume it to reconcile the original request; do not create a replacement.'),
}


class CustomerError(ValueError):
    def __init__(self, reason):
        if reason not in MESSAGES:
            raise ValueError('unknown customer diagnostic')
        self.reason = reason
        super().__init__(MESSAGES[reason][0])

    def public(self):
        message, action = MESSAGES[self.reason]
        return dict(event='customer_action_required', reason=self.reason, message=message,
                    next_step=action, automatic_resubmission=False)


def channel_reason(channels, role, amount=None, balance='spendable_msat'):
    """One bound channel only; malformed input stays an unclassified error."""
    if role not in ('btc', 'xbt'):
        raise ValueError('unknown channel role')
    if len(channels) != 1 or channels[0]['state'] != 'CHANNELD_NORMAL':
        return role+'_channel_unavailable'
    channel = channels[0]
    if type(channel['peer_connected']) is not bool or not isinstance(channel.get('htlcs', []), list):
        raise ValueError('invalid channel readiness data')
    if not channel['peer_connected']:
        return role+'_peer_disconnected'
    if channel.get('htlcs'):
        return role+'_channel_busy'
    if amount is not None:
        value = channel[balance]
        if type(value) is not int or value < 0:
            raise ValueError('invalid channel balance')
        if value < amount:
            return ('insufficient_'+role+'_receive_liquidity' if balance == 'receivable_msat'
                    else 'insufficient_'+role+'_send_liquidity')
    return None
