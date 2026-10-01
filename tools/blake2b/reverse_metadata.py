"""Bounded final-hop metadata from a signed, separately bound BTC invoice.

A bolt11 argument alone does not make sendpay include payment_metadata.
This validator contains no RPCs and never prints payload contents.
"""
import re


def invoice_metadata(decoded):
    features = decoded.get('features', '0')
    if not isinstance(features, str) or not re.fullmatch('[0-9a-fA-F]{1,512}', features):
        raise ValueError('unsupported invoice feature encoding')
    bits = int(features, 16)
    if any(bits & (1 << bit) for bit in range(0, bits.bit_length(), 2) if bit not in (8, 14, 16, 48)):
        raise ValueError('invoice requires unsupported features')
    metadata = decoded.get('payment_metadata')
    if metadata is not None and (not isinstance(metadata, str) or len(metadata) > 1024
                                 or len(metadata) % 2 or not re.fullmatch('[0-9a-fA-F]*', metadata)):
        raise ValueError('invoice payment metadata is malformed or exceeds the 512-byte inspection cap')
    if bits & (1 << 48) and metadata is None:
        raise ValueError('invoice requires payment metadata but none was decoded')
    return metadata
