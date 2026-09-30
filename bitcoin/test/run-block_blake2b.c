#include "config.h"
#include "../block.c"
#include "../block_blake2b.c"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <bitcoin/tx.h>
#include <ccan/str/hex/hex.h>
#include <ccan/tal/grab_file/grab_file.h>
#include <common/json_parse_simple.h>
#include <common/setup.h>
#include <common/utils.h>
#include <stdio.h>

/* Test-only chain parameters. No production network opts in yet. */
static const struct chainparams blake2b_test = {
	.network_name = "blake2b-header-test",
	.has_blake2b_headers = true,
};

/* Syntactically valid coinbase, used only to test parsing. Synthetic blocks
 * below do not have valid PoW/merkle commitments and must not be broadcast. */
static const char txhex[] =
	"0100000001"
	"0000000000000000000000000000000000000000000000000000000000000000"
	"ffffffff020000ffffffff010100000000000000015100000000";

static char *tohex(const tal_t *ctx, const u8 *raw, size_t len)
{
	char *hex = tal_arr(ctx, char, hex_str_size(len));
	assert(hex_encode(raw, len, hex, tal_bytelen(hex)));
	return hex;
}

static struct bitcoin_block *parse(const u8 *raw, size_t len,
				   const struct chainparams *params)
{
	char *hex = tohex(tmpctx, raw, len);
	struct bitcoin_block *b = bitcoin_block_from_hex(tmpctx, params,
							 hex, strlen(hex));
	tal_free(hex);
	return b;
}

static u8 *make_block(const u8 *header, size_t hdrlen, u8 count)
{
	size_t txlen = hex_data_size(strlen(txhex));
	u8 *raw = tal_arr(tmpctx, u8, hdrlen + 1 + count * txlen);

	assert(count < 0xfd);
	memcpy(raw, header, hdrlen);
	raw[hdrlen] = count;
	for (size_t i = 0; i < count; i++)
		assert(hex_decode(txhex, strlen(txhex), raw + hdrlen + 1 + i * txlen,
				  txlen));
	return raw;
}

static void test_vector(const char *json, const jsmntok_t *v)
{
	u8 header[BITCOIN_HEADER_V2_SIZE];
	struct bitcoin_blkid id, expected, sentinel;
	struct bitcoin_txid expected_txid;
	struct bitcoin_block *b;
	char *hex, *expected_hex;
	u8 *raw;
	u32 timestamp;
	const jsmntok_t *fields;

	hex = json_strdup(tmpctx, json, json_get_member(json, v, "serialized"));
	assert(hex_decode(hex, strlen(hex), header, sizeof(header)));
	expected_hex = json_strdup(tmpctx, json, json_get_member(json, v, "block_hash"));
	assert(bitcoin_blkid_from_hex(expected_hex, strlen(expected_hex), &expected));
	assert(bitcoin_block_blake2b_hash(header, sizeof(header), &id));
	assert(bitcoin_blkid_eq(&id, &expected));

	memset(&sentinel, 0xa5, sizeof(sentinel));
	for (size_t i = 0; i < sizeof(header); i++) {
		id = sentinel;
		assert(!bitcoin_block_blake2b_hash(header, i, &id));
		assert(bitcoin_blkid_eq(&id, &sentinel));
	}
	assert(!bitcoin_block_blake2b_hash(header, sizeof(header) + 1, &id));
	assert(!bitcoin_block_blake2b_hash(NULL, sizeof(header), &id));
	header[3] &= 0x7f;
	assert(!bitcoin_block_blake2b_hash(header, sizeof(header), &id));
	header[3] |= 0x80;

	/* Upstream vectors have 1 or 3 transactions. Keep their header intact. */
	assert(header[109] == 0);
	raw = make_block(header, sizeof(header), header[108]);
	b = parse(raw, tal_bytelen(raw), &blake2b_test);
	assert(b);
	bitcoin_block_blkid(b, &id);
	assert(bitcoin_blkid_eq(&id, &expected));
	assert(tal_count(b->tx) == header[108]);
	fields = json_get_member(json, v, "fields");
	assert(json_to_u32(json, json_get_member(json, fields, "nTime"), &timestamp));
	assert(b->hdr.timestamp == timestamp);
	/* Independently calculated SHA256d of txhex, in RPC display order. */
	assert(bitcoin_txid_from_hex("a50d38efd7ce391689739ac2b7c2521e7016dd4979a87c1b50ff22bcebe1b97b", 64, &expected_txid));
	for (size_t i = 0; i < tal_count(b->tx); i++) {
		assert(b->tx[i]->chainparams == &blake2b_test);
		assert(bitcoin_txid_eq(&b->txids[i], &expected_txid));
	}
	tal_free(b);

	/* Truncation at every byte tests header, CompactSize and tx failures. */
	for (size_t i = 0; i < tal_bytelen(raw); i++)
		assert(!parse(raw, i, &blake2b_test));
	/* Opt-in must not turn Bitcoin's parser into a v2 parser. */
	assert(!parse(raw, tal_bytelen(raw), chainparams_for_network("bitcoin")));
	/* Declared transaction count must agree with the header. */
	raw[sizeof(header)]--;
	assert(!parse(raw, tal_bytelen(raw), &blake2b_test));
	raw[sizeof(header)]++;
	/* Impossible CompactSize count must be rejected before allocation. */
	raw[sizeof(header)] = 0xff;
	memset(raw + sizeof(header) + 1, 0xff, 8);
	assert(!parse(raw, tal_bytelen(raw), &blake2b_test));
	tal_free(raw);

	/* Knots deliberately wraps time addition at 32 bits. */
	memset(header + 68, 0xff, 4);
	header[68] = 0xfe;
	memset(header + 104, 0, 4);
	header[104] = 4;
	header[110] |= BITCOIN_HEADER_V2_TIME_OFFSET;
	raw = make_block(header, sizeof(header), header[108]);
	b = parse(raw, tal_bytelen(raw), &blake2b_test);
	assert(b && b->hdr.timestamp == 2);
	assert(bitcoin_block_blake2b_hash(header, sizeof(header), &id));
	assert(bitcoin_blkid_eq(&b->hdr.hash, &id));
	tal_free(b);
	tal_free(raw);
	printf("Knots vector OK: %s\n",
	       json_strdup(tmpctx, json, json_get_member(json, v, "name")));
}

static void test_legacy(void)
{
	u8 header[80] = { 0 };
	u8 *raw;
	struct bitcoin_blkid expected;
	struct bitcoin_block *b;

	header[3] = 0x20;
	sha256_double(&expected.shad, header, sizeof(header));
	raw = make_block(header, sizeof(header), 1);
	b = parse(raw, tal_bytelen(raw), &blake2b_test);
	assert(b);
	assert(bitcoin_blkid_eq(&b->hdr.hash, &expected));
	tal_free(b);
	for (size_t i = 0; i < tal_bytelen(raw); i++)
		assert(!parse(raw, i, &blake2b_test));
	tal_resize(&raw, tal_bytelen(raw) + 1);
	raw[tal_bytelen(raw) - 1] = 0;
	assert(!parse(raw, tal_bytelen(raw), &blake2b_test));
	tal_free(raw);

	/* BTC high-bit versions remain historical-format SHA256d, not v2. */
	header[3] |= 0x80;
	sha256_double(&expected.shad, header, sizeof(header));
	raw = make_block(header, sizeof(header), 1);
	b = parse(raw, tal_bytelen(raw), chainparams_for_network("bitcoin"));
	assert(b);
	assert(bitcoin_blkid_eq(&b->hdr.hash, &expected));
	tal_free(b);
	tal_free(raw);
}

static void test_elements(void)
{
	/* Minimal synthetic dynafed header: version, prev, merkle, time,
	 * height, two null params, empty signing witness, empty tx vector. */
	u8 raw[80] = { 0 };
	struct bitcoin_block *b;
	struct bitcoin_blkid expected;

	raw[3] = 0x80;
	/* Signing witness and transaction vector are excluded from hash. */
	sha256_double(&expected.shad, raw, 78);
	b = parse(raw, sizeof(raw), chainparams_for_network("liquid-regtest"));
	assert(b);
	assert(bitcoin_blkid_eq(&b->hdr.hash, &expected));
	assert(tal_count(b->tx) == 0);
	tal_free(b);
}

int main(int argc, const char *argv[])
{
	char *json;
	jsmntok_t *toks;
	const jsmntok_t *headers, *v;
	size_t i;

	common_setup(argv[0]);
	chainparams = chainparams_for_network("bitcoin");
	json = grab_file_str(tmpctx, argc > 1 ? argv[1] :
			    "tests/data/blake2b/block_header_v2.json");
	assert(json);
	toks = json_parse_simple(tmpctx, json, strlen(json));
	assert(toks);
	headers = json_get_member(json, toks, "headers");
	assert(headers && headers->type == JSMN_ARRAY && headers->size == 5);
	json_for_each_arr(i, v, headers)
		test_vector(json, v);
	test_legacy();
	test_elements();
	common_shutdown();
	printf("Native header and block parser tests OK\n");
	return 0;
}
