#include "config.h"
#include <bitcoin/block.h>
#include <bitcoin/block_blake2b.h>
#include <ccan/crypto/sha256/sha256.h>
#include <sodium/crypto_generichash_blake2b.h>
#include <string.h>

/* Knots v29.4.2.knots20260508, src/primitives/block.cpp, GetHash().
 * Work with serialized bytes: no host-endian structs or alignment assumptions.
 * SHA256 remains the tagged commitment hash; BLAKE2b uses 32-byte output
 * parameters (not truncation of BLAKE2b-512).
 */
static void block_tagged_hash(u8 out[32], const char *tag,
			      const u8 *data, size_t len)
{
	struct sha256 taghash, hash;
	struct sha256_ctx ctx;

	sha256(&taghash, tag, strlen(tag));
	sha256_init(&ctx);
	sha256_update(&ctx, taghash.u.u8, sizeof(taghash.u.u8));
	sha256_update(&ctx, taghash.u.u8, sizeof(taghash.u.u8));
	sha256_update(&ctx, data, len);
	sha256_done(&ctx, &hash);
	memcpy(out, hash.u.u8, sizeof(hash.u.u8));
}

bool bitcoin_block_blake2b_hash(const u8 *header, size_t len,
			      struct bitcoin_blkid *id)
{
	u8 prev_ordered[32], prev_hidden[32], key_hash[32];
	u8 h1_data[119], h1[32], h2_data[96], h2[32];
	u8 first_data[52], first[32], asic[160], second[32];
	u8 mask[32] = { 0 };
	const u8 *key;
	size_t asic_len;
	u8 flags, clear_bits, key_nonzero = 0;

	if (len != BITCOIN_HEADER_V2_SIZE || !header
	    || !(header[3] & 0x80))
		return false;

	flags = header[110];
	clear_bits = header[111];
	key = header + 112;
	for (size_t i = 0; i < 32; i++)
		prev_ordered[i] = header[35 - i];
	block_tagged_hash(key_hash, "Bitcoin block hash PoW XOR key", key, 16);

	memcpy(h1_data, header, 4);                /* complete version */
	memcpy(h1_data + 4, prev_ordered, 32);
	memcpy(h1_data + 36, header + 128, 4);     /* height */
	memcpy(h1_data + 40, header + 36, 32);     /* merkle root */
	memcpy(h1_data + 72, header + 68, 4);      /* time-on-wire */
	h1_data[76] = 0;                          /* reserved 40-bit time */
	memcpy(h1_data + 77, header + 72, 4);      /* nBits */
	memcpy(h1_data + 81, header + 108, 2);     /* txcount: u16 -> u32 */
	h1_data[83] = h1_data[84] = 0;
	h1_data[85] = flags;
	h1_data[86] = clear_bits;
	memcpy(h1_data + 87, key_hash, 32);
	block_tagged_hash(h1, "Bitcoin block header 1", h1_data, sizeof(h1_data));

	memcpy(h2_data, h1, 32);
	memset(h2_data + 32, 0, 32);
	memcpy(h2_data + 64, header + 132, 32);    /* merge-mining RHS */
	block_tagged_hash(h2, "Merge-mining hook", h2_data, sizeof(h2_data));

	memset(first_data, 0, 4);
	memcpy(first_data + 4, h2, 32);
	memcpy(first_data + 36, header + 88, 16); /* extranonce */
	if (crypto_generichash_blake2b(first, sizeof(first),
				     first_data, sizeof(first_data), NULL, 0) != 0)
		return false;

	switch (flags & 3) {
	case 0:
		block_tagged_hash(prev_hidden, "Bitcoin prevblock header, hashed",
				  prev_ordered, sizeof(prev_ordered));
		memset(prev_hidden, 0, 6);
		memcpy(asic, prev_hidden, 32);
		memcpy(asic + 32, header + 76, 8);  /* nonce, nonce2 */
		memcpy(asic + 40, header + 104, 4); /* offset */
		memcpy(asic + 44, header + 84, 4);  /* nonce3 */
		memcpy(asic + 48, first, 32);
		asic_len = 80;
		break;
	case 1:
		memcpy(asic, header + 76, 12);     /* nonce, nonce2, nonce3 */
		memcpy(asic + 12, header + 104, 4);
		memcpy(asic + 16, first, 32);
		memcpy(asic + 48, h2, 32);
		asic_len = 80;
		break;
	default: /* profile 2: 48 zero bytes; profile 3: 80 zero bytes */
		asic_len = (flags & 3) == 2 ? 48 : 80;
		memset(asic, 0, asic_len);
		memcpy(asic + asic_len, h2, 32);
		memcpy(asic + asic_len + 32, header + 76, 8);
		memcpy(asic + asic_len + 40, header + 104, 4);
		memcpy(asic + asic_len + 44, header + 84, 4);
		memcpy(asic + asic_len + 48, first, 32);
		asic_len += 80;
		break;
	}
	if (crypto_generichash_blake2b(second, sizeof(second),
				     asic, asic_len, NULL, 0) != 0)
		return false;

	for (size_t i = 0; i < 16; i++)
		key_nonzero |= key[i];
	if (key_nonzero) {
		block_tagged_hash(mask, "Bitcoin block hash PoW XOR mask", key, 16);
		memset(mask, 0, clear_bits / 8);
		mask[clear_bits / 8] &= 0xffU >> (clear_bits % 8);
	}
	for (size_t i = 0; i < 32; i++)
		id->shad.sha.u.u8[31 - i] = second[i] ^ mask[i];
	return true;
}
