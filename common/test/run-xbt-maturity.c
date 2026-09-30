#include "config.h"
#include "../utxo.c"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <common/setup.h>
#include <stdio.h>

int main(int argc UNUSED, const char *argv[])
{
	u32 height = 973440;
	struct utxo utxo = { .is_in_coinbase = true, .blockheight = &height };
	const char *unchanged[] = { "bitcoin", "regtest", "xbt-regtest", "signet" };
	u32 heights[] = { 973439, 973440, 974797, 979919, 979920 };

	common_setup(argv[0]);
	for (size_t i = 0; i < sizeof(unchanged) / sizeof(unchanged[0]); i++) {
		chainparams = chainparams_for_network(unchanged[i]);
		assert(chainparams);
		assert(utxo_is_immature(&utxo, height) == 99);
		assert(utxo_is_immature(&utxo, height + 98) == 1);
		assert(utxo_is_immature(&utxo, height + 99) == 0);
	}
	chainparams = chainparams_for_network("xbt");
	assert(chainparams && chainparams->wallet_coinbase_maturity == 6480);
	for (size_t i = 0; i < sizeof(heights) / sizeof(heights[0]); i++) {
		height = heights[i];
		assert(utxo_is_immature(&utxo, height) == 6479);
		assert(utxo_is_immature(&utxo, height + 99) == 6380);
		assert(utxo_is_immature(&utxo, height + 6478) == 1);
		assert(utxo_is_immature(&utxo, height + 6479) == 0);
		assert(utxo_is_immature(&utxo, height + 6480) == 0);
		/* Moving the tip backwards must make the output immature again. */
		assert(utxo_is_immature(&utxo, height + 6478) == 1);
	}
	/* Policy does not switch off at the consensus release height. */
	height = 979919;
	assert(utxo_is_immature(&utxo, 979920) == 6478);
	utxo.is_in_coinbase = false;
	utxo.blockheight = NULL;
	assert(utxo_is_immature(&utxo, 974797) == 0);
	common_shutdown();
	puts("XBT coinbase relay-maturity tests OK");
	return 0;
}
