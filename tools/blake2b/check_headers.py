#!/usr/bin/env python3
"""Independent Knots header-v2 hash oracle; not consensus validation.

Python standard library only. This does not change lightningd or enable XBT.
See README.md for the pinned upstream specification and vector provenance.
"""
import hashlib
import json
from pathlib import Path
import struct
import unittest


def tagged_hash(tag, payload):
    taghash = hashlib.sha256(tag.encode('ascii')).digest()
    return hashlib.sha256(taghash + taghash + payload).digest()


def header_hashes(raw):
    """Return intermediate digests and RPC-display block hash for one header.

    Accept exactly one serialized 80-byte v1 or 164-byte v2 header. The
    version's high bit selects the format. This is specifically the Knots
    format, not Elements' unrelated high-bit/dynafed format.
    """
    if len(raw) < 4:
        raise ValueError('truncated version')
    version = struct.unpack_from('<I', raw)[0]
    size = 164 if version & 0x80000000 else 80
    if len(raw) != size:
        raise ValueError(f'expected exactly {size} header bytes')
    if size == 80:
        digest = hashlib.sha256(hashlib.sha256(raw).digest()).digest()
        return {'block_hash': digest[::-1].hex()}

    # Preserve time-on-wire: adding the offset here would hash the wrong time.
    nonce = raw[76:80]
    nonce2, nonce3 = raw[80:84], raw[84:88]
    extranonce, offset = raw[88:104], raw[104:108]
    txcount = struct.unpack_from('<H', raw, 108)[0]
    flags, clear_bits = raw[110], raw[111]
    key, height, mm_rhs = raw[112:128], raw[128:132], raw[132:164]
    prev_ordered = raw[4:36][::-1]
    key_hash = tagged_hash('Bitcoin block hash PoW XOR key', key)
    h1 = tagged_hash(
        'Bitcoin block header 1',
        raw[:4] + prev_ordered + height + raw[36:68] + raw[68:72]
        + b'\x00' + raw[72:76] + struct.pack('<I', txcount)
        + bytes([flags, clear_bits]) + key_hash)
    h2 = tagged_hash('Merge-mining hook', h1 + bytes(32) + mm_rhs)
    first = hashlib.blake2b(bytes(4) + h2 + extranonce, digest_size=32).digest()
    profile = flags & 3
    if profile == 0:
        prev_hidden = tagged_hash('Bitcoin prevblock header, hashed', prev_ordered)
        asic_input = bytes(6) + prev_hidden[6:] + nonce + nonce2 + offset + nonce3 + first
    elif profile == 1:
        asic_input = nonce + nonce2 + nonce3 + offset + first + h2
    else:
        asic_input = bytes(48 if profile == 2 else 80) + h2 + nonce + nonce2 + offset + nonce3 + first
    second = hashlib.blake2b(asic_input, digest_size=32).digest()
    mask = bytearray(32)
    if any(key):
        mask[:] = tagged_hash('Bitcoin block hash PoW XOR mask', key)
        count, bits = divmod(clear_bits, 8)
        mask[:count] = bytes(count)
        mask[count] &= 0xff >> bits
    # Knots reverses the XOR result into uint256 storage; RPC reverses it
    # back when displaying. Thus display order here is the XOR order.
    result = bytes(a ^ b for a, b in zip(second, mask))
    return {
        'xor_key_hash': key_hash.hex(), 'h1': h1.hex(), 'h2': h2.hex(),
        'blake2b_1': first.hex(), 'blake2b_2': second.hex(),
        'mask': mask.hex(), 'block_hash': result.hex(),
        'asic_profile': profile, 'asic_input': asic_input.hex(),
    }


class HeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[2] / 'tests/data/blake2b/block_header_v2.json'
        cls.vectors = json.loads(path.read_text())['headers']

    def test_knots_vectors(self):
        for vector in self.vectors:
            with self.subTest(vector=vector['name']):
                result = header_hashes(bytes.fromhex(vector['serialized']))
                for field, value in result.items():
                    self.assertEqual(value, vector[field], field)

    def test_live_headers(self):
        path = Path(__file__).resolve().parents[2] / 'tests/data/blake2b/live_headers.json'
        for vector in json.loads(path.read_text())['headers']:
            with self.subTest(height=vector['height']):
                self.assertEqual(header_hashes(bytes.fromhex(vector['serialized']))['block_hash'],
                                 vector['block_hash'])

    def test_legacy_genesis(self):
        raw = bytes.fromhex(
            '01000000' + '00' * 32
            + '3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a'
            + '29ab5f49ffff001d1dac2b7c')
        self.assertEqual(header_hashes(raw)['block_hash'],
                         '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f')

    def test_truncation_and_trailing_data(self):
        raw = bytes.fromhex(self.vectors[0]['serialized'])
        for size in range(len(raw)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                header_hashes(raw[:size])
        with self.assertRaises(ValueError):
            header_hashes(raw + b'\x00')

    def test_version_controls_length(self):
        raw = bytearray.fromhex(self.vectors[0]['serialized'])
        raw[3] &= 0x7f
        with self.assertRaises(ValueError):
            header_hashes(raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
