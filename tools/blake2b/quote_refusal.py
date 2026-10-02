"""Public reason codes for definite refusals before quote creation."""
REASONS = {
    'btc_peer_disconnected': 'The required BTC peer is disconnected. Reconnect it before retrying the quote.',
    'xbt_peer_disconnected': 'The required XBT peer is disconnected. Reconnect it before retrying the quote.',
    'btc_channel_unavailable': 'The required BTC channel is unavailable or not yet normal.',
    'xbt_channel_unavailable': 'The required XBT channel is unavailable or ambiguous.',
    'btc_channel_busy': 'The required BTC channel has pending HTLCs. Wait for them to resolve.',
    'xbt_channel_busy': 'The required XBT channel has pending HTLCs. Wait for them to resolve.',
    'insufficient_btc_receive_liquidity': 'Insufficient BTC inbound channel liquidity for this receipt.',
    'insufficient_xbt_receive_liquidity': 'Insufficient XBT inbound channel liquidity for this payment.',
    'insufficient_btc_send_liquidity': 'Insufficient BTC outbound channel liquidity for this payment.',
    'insufficient_xbt_send_liquidity': 'Insufficient XBT outbound channel liquidity for this receipt.',
    'invoice_expiring': 'The destination invoice has expired or has too little time remaining.',
    'btc_price_cap': 'The BTC quote exceeds the receiving price cap.',
    'market_reference_gap': 'Ordinary exchange limit prices differ too far from the ticker price.',
    'market_slippage': 'Exchange prices exceed the allowed slippage; request a quote when the market meets policy.',
    'market_depth': 'Insufficient exchange limit-order liquidity for this quote.',
    'market_spread': 'Exchange spread is crossed or exceeds the allowed limit.',
    'market_stale': 'Exchange price timestamp is stale or in the future.',
    'market_inconsistent': 'Exchange ticker and order book disagree.',
    'insufficient_xbt_liquidity': 'Insufficient XBT customer-to-operator liquidity.',
    'insufficient_btc_liquidity': 'Insufficient BTC operator liquidity.',
    'price_cap': 'The XBT quote exceeds your price cap.',
    'operator_reserve': 'Operator on-chain reserve is below the required minimum.',
    'no_route': 'No BTC route within the configured limits.',
}


class QuoteRefused(ValueError):
    def __init__(self, reason):
        if reason not in REASONS:
            raise ValueError('unknown public refusal code')
        self.reason = reason
        super().__init__(REASONS[reason])

    def public(self):
        return dict(error='quote_refused', reason=self.reason,
                    quote_created=False, payment_started=False)


def market_refusal(error):
    """Classify only known validator failures at the read-only market boundary."""
    from reverse_check import DiagnosticError
    if not isinstance(error, DiagnosticError) or error.details.get('stage') != 'market.validation':
        return None
    return {
        'limit bid differs from ticker beyond reference gap limit': 'market_reference_gap',
        'bid proceeds fall below slippage limit': 'market_slippage',
        'insufficient limit-order bid depth': 'market_depth',
        'no limit-order bid liquidity': 'market_depth',
        'crossed or excessive market spread': 'market_spread',
        'stale or future ticker timestamp': 'market_stale',
        'inconsistent ticker and order book': 'market_inconsistent',
    }.get(error.details.get('validation'))
