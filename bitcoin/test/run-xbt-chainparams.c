#include "config.h"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <ccan/str/str.h>
#include <ccan/tal/grab_file/grab_file.h>
#include <common/bolt12.h>
#include <common/setup.h>
#include <common/utils.h>
#include <stdio.h>

int main(int argc, const char *argv[])
{
	const struct chainparams *btc, *regtest, *xbt, *decoded;
	const u8 *cursor;
	u8 *wire;
	size_t len;
	struct bitcoin_blkid unknown = { 0 };

	common_setup(argv[0]);
	btc = chainparams_for_network("bitcoin");
	regtest = chainparams_for_network("regtest");
	xbt = chainparams_for_network("xbt-regtest");
	assert(btc && regtest && xbt);
	assert(!chainparams_for_network("xbt"));
	assert(xbt->testnet && xbt->has_blake2b_headers && !xbt->is_elements);
	assert(bitcoin_blkid_eq(&regtest->genesis_blockhash, &xbt->genesis_blockhash));
	assert(!bitcoin_blkid_eq(chainparams_get_chainhash(regtest),
				 chainparams_get_chainhash(xbt)));
	assert(chainparams_get_chainhash(btc) == &btc->genesis_blockhash);
	assert(chainparams_by_chainhash(&xbt->genesis_blockhash) == regtest);
	assert(chainparams_by_chainhash(chainparams_get_chainhash(xbt)) == xbt);
	assert(chainparams_by_lightning_hrp("bcrt") == regtest);
	assert(chainparams_by_lightning_hrp("xbtrt") == xbt);
	assert(streq(xbt->onchain_hrp, "bcrt"));
	assert(xbt->ln_port != regtest->ln_port);
	assert(xbt->rpc_port != regtest->rpc_port);

	/* Internal daemon serialization must retain the fork, not map it back
	 * to ordinary regtest through the shared genesis. */
	wire = tal_arr(tmpctx, u8, 0);
	towire_chainparams(&wire, xbt);
	len = tal_count(wire);
	assert(len == 32);
	cursor = wire;
	fromwire_chainparams(&cursor, &len, &decoded);
	assert(decoded == xbt && cursor && len == 0);
	len = 31;
	cursor = wire;
	fromwire_chainparams(&cursor, &len, &decoded);
	assert(!cursor && !decoded);

	/* BTC behavior is unchanged, but XBT requires an explicit matching
	 * init networks TLV, rejecting absent, empty, BTC and unknown IDs. */
	assert(chainparams_accepts_peer_networks(btc, NULL, 0));
	assert(!chainparams_accepts_peer_networks(xbt, NULL, 0));
	assert(!chainparams_accepts_peer_networks(xbt, chainparams_get_chainhash(xbt), 0));
	assert(!chainparams_accepts_peer_networks(xbt, chainparams_get_chainhash(regtest), 1));
	assert(!chainparams_accepts_peer_networks(regtest, chainparams_get_chainhash(xbt), 1));
	assert(!chainparams_accepts_peer_networks(xbt, &unknown, 1));
	assert(chainparams_accepts_peer_networks(xbt, chainparams_get_chainhash(xbt), 1));

	/* BOLT12's omitted chain means BTC, never the inherited-genesis fork. */
	assert(bolt12_chain_matches(NULL, btc));
	assert(!bolt12_chain_matches(NULL, xbt));
	assert(!bolt12_chain_matches(chainparams_get_chainhash(regtest), xbt));
	assert(!bolt12_chain_matches(chainparams_get_chainhash(xbt), regtest));
	assert(bolt12_chain_matches(chainparams_get_chainhash(xbt), xbt));
	/* Optional live-block cross-check against the full node's RPC ID. */
	if (argc == 3) {
		char *hex = grab_file_str(tmpctx, argv[1]);
		struct bitcoin_block *block;
		struct bitcoin_blkid expected;

		chainparams = xbt;
		assert(hex);
		assert(bitcoin_blkid_from_hex(argv[2], strlen(argv[2]), &expected));
		block = bitcoin_block_from_hex(tmpctx, xbt, hex, strlen(hex));
		assert(block);
		assert(bitcoin_blkid_eq(&block->hdr.hash, &expected));
		puts("Live Knots block parsing and hash OK");
	}
	common_shutdown();
	puts("XBT regtest identity tests OK");
	return 0;
}
