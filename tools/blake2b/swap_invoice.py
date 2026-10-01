"""Minimal unsigned BOLT11 invoice for signinvoice; BTC and reverse XBT regtest.

Defaults to 100,000 sats; no MPP or routing hints. CLN supplies the signature.
Used by the experimental regtest service, not a general BOLT11 library.
"""
import time

CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


def polymod(values):
    check = 1
    generators = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    for value in values:
        top = check >> 25
        check = ((check & 0x1ffffff) << 5) ^ value
        for i, generator in enumerate(generators):
            if (top >> i) & 1:
                check ^= generator
    return check


def encode(hrp, words):
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    check = polymod(expanded + words + [0] * 6) ^ 1
    checksum = [(check >> (5 * (5 - i))) & 31 for i in range(6)]
    return hrp + '1' + ''.join(CHARSET[w] for w in words + checksum)


def uint_words(value):
    if value < 0:
        raise ValueError('negative integer')
    words = []
    while value:
        words.insert(0, value & 31)
        value >>= 5
    return words or [0]


def byte_words(raw):
    acc = bits = 0
    words = []
    for byte in raw:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            words.append((acc >> bits) & 31)
        acc &= (1 << bits) - 1
    if bits:
        words.append((acc << (5 - bits)) & 31)
    return words


def unsigned_invoice(payment_hash, payment_secret, amount_msat=100000000, expiry=3600,
                     currency="bcrt", final_cltv=120, live_reverse=False):
    if currency == 'xbt':
        if live_reverse is not True:
            raise ValueError('live reverse invoice requires explicit activation')
        from reverse_live import enabled
        enabled()
    if currency not in ('bc', 'bcrt', 'xbtrt', 'xbt') or type(final_cltv) is not int or not 1 <= final_cltv <= 2016:
        raise ValueError('unsupported invoice network or CLTV')
    if type(amount_msat) is not int or not 0 < amount_msat <= 2100000000000000000:
        raise ValueError('invalid invoice amount')
    if type(expiry) is not int or not 0 < expiry <= 3600:
        raise ValueError('invalid invoice expiry')
    raw_hash, raw_secret = bytes.fromhex(payment_hash), bytes.fromhex(payment_secret)
    if len(raw_hash) != 32 or len(raw_secret) != 32:
        raise ValueError('hash and secret must be 32 bytes')
    timestamp = int(time.time())
    if not 0 <= timestamp < (1 << 35):
        raise ValueError('timestamp outside BOLT11 range')
    words = [(timestamp >> (5 * (6 - i))) & 31 for i in range(7)]

    def tag(letter, payload):
        words.extend([CHARSET.index(letter), len(payload) >> 5, len(payload) & 31])
        words.extend(payload)

    tag('p', byte_words(raw_hash))
    tag('s', byte_words(raw_secret))
    description = (b'Experimental swap: pay XBT, receive BTC' if currency == 'xbt'
                   else b'Regtest swap: pay XBT, receive BTC' if currency == 'xbtrt'
                   else b'Regtest swap: pay BTC, receive XBT' if currency == 'bcrt'
                   else b'Experimental swap: pay BTC, receive XBT')
    tag('d', byte_words(description))
    tag('x', uint_words(expiry))
    tag('c', uint_words(final_cltv))
    # Required variable-length onion and payment-secret features; no MPP.
    tag('9', uint_words((1 << 8) | (1 << 14)))
    hrp = 'ln' + currency + ('1m' if amount_msat == 100000000 else str(amount_msat * 10) + 'p')
    return encode(hrp, words + [0] * 104)
