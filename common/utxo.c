#include "config.h"
#include <bitcoin/chainparams.h>
#include <common/utxo.h>
#include <common/utils.h>

size_t utxo_spend_weight(const struct utxo *utxo, size_t min_witness_weight)
{
	size_t witness_weight;
	bool p2sh = (utxo->utxotype == UTXO_P2SH_P2WPKH);

	witness_weight = bitcoin_tx_input_witness_weight(utxo->utxotype);

	/* If the min is less than what we'd use for a 'normal' tx,
	 * we return the value with the greater added/calculated */
	if (witness_weight < min_witness_weight)
		return bitcoin_tx_input_weight(p2sh,
					       min_witness_weight);

	return bitcoin_tx_input_weight(p2sh, witness_weight);
}

u32 utxo_is_immature(const struct utxo *utxo, u32 blockheight)
{
	if (utxo->is_in_coinbase) {
		u32 maturity = chainparams->wallet_coinbase_maturity
			? chainparams->wallet_coinbase_maturity : 100;
		u64 eligible_tip;
		/* We got this from a block, it must have a known
		 * blockheight. */
		assert(utxo->blockheight);

		/* Mempool transactions target the NEXT block: a coinbase at H
		 * is relayable once the tip reaches H + maturity - 1. */
		eligible_tip = (u64)*utxo->blockheight + maturity - 1;
		if (blockheight >= eligible_tip)
			return 0;
		return eligible_tip - blockheight > UINT32_MAX
			? UINT32_MAX : eligible_tip - blockheight;
	} else {
		/* Non-coinbase outputs are always mature. */
		return 0;
	}
}

const char *utxotype_to_str(enum utxotype utxotype)
{
	switch (utxotype) {
	case UTXO_P2SH_P2WPKH:
		return "p2sh_p2wpkh";
	case UTXO_P2WPKH:
		return "p2wpkh";
	case UTXO_P2WSH_FROM_CLOSE:
		return "p2wsh_from_close";
	case UTXO_P2TR:
		return "p2tr";
	}
	abort();
}
