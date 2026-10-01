"""Public reason codes for definite refusals before quote creation."""
REASONS = {
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
