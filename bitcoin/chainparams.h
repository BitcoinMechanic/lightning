#ifndef LIGHTNING_BITCOIN_CHAINPARAMS_H
#define LIGHTNING_BITCOIN_CHAINPARAMS_H

#include "config.h"
#include <bitcoin/block.h>
#include <common/amount.h>
#include <common/bip32.h>

#define ELEMENTS_ASSET_LEN 33

struct chainparams {
	const char *network_name;
	/* Unfortunately starting with signet, we now have diverging
	 * conventions for the "BIP173" Human Readable Part (HRP).
	 * On onchain signet, the HRP is `tb` , but on Lightning
	 * signet the HRP is `tbs`.
	 */
	const char *onchain_hrp;
	const char *lightning_hrp;
	/*'bip70_name' is corresponding to the 'chain' field of
	 * the API 'getblockchaininfo' */
	const char *bip70_name;
	const struct bitcoin_blkid genesis_blockhash;
	/* Lightning protocol/domain identity for a shared-history fork. NULL
	 * means the historical genesis-based identity. Never substitute this
	 * for the actual block-zero hash in on-chain validation. */
	const struct bitcoin_blkid *lightning_chainhash;
	const int rpc_port;
	/**
	 * BOLT 1:
	 *
	 * The default TCP port depends on the network used. The most common networks are:
	 *
	 * - Bitcoin mainet with port number 9735 or the corresponding hexadecimal `0x2607`;
	 * - Bitcoin testnet with port number 19735 (`0x4D17`);
	 * - Bitcoin signet with port number 39735 (`0x9B37`).
	 */
	const int ln_port;
	const char *cli;
	const char *cli_args;
	/* The min numeric version of cli supported */
	const u64 cli_min_supported_version;
	const struct amount_sat dust_limit;
	const struct amount_sat max_funding;
	const struct amount_msat max_payment;
	/* Total coins in network */
	const struct amount_sat max_supply;
	const u32 when_lightning_became_cool;
	const u8 p2pkh_version;
	const u8 p2sh_version;

	/* Whether this is a test network or not */
	const bool testnet;

	/* Version codes for BIP32 extended keys in libwally-core*/
	const struct bip32_key_version bip32_key_version;
	const bool is_elements;
	/* Accept Knots v2 headers as well as historical SHA256d headers.
	 * Only the experimental XBT regtest network enables this for now. */
	const bool has_blake2b_headers;
	const u8 *fee_asset_tag;
};

static inline const struct bitcoin_blkid *
chainparams_get_chainhash(const struct chainparams *params)
{
	return params->lightning_chainhash ? params->lightning_chainhash
		: &params->genesis_blockhash;
}

/* Forks with a separate Lightning identity require an explicit networks TLV.
 * Preserve the historical behavior for peers omitting it on other chains. */
static inline bool chainparams_accepts_peer_networks(
	const struct chainparams *params, const struct bitcoin_blkid *chains,
	size_t num_chains)
{
	if (!chains)
		return params->lightning_chainhash == NULL;
	for (size_t i = 0; i < num_chains; i++) {
		if (bitcoin_blkid_eq(&chains[i], chainparams_get_chainhash(params)))
			return true;
	}
	return false;
}

/**
 * chainparams_for_network - Look up blockchain parameters by its name
 */
const struct chainparams *chainparams_for_network(const char *network_name);

/**
 * chainparams_by_bip173 - Helper to get a network by its bip173 name
 *
 * This lets us decode BOLT11 addresses.
 */
const struct chainparams *chainparams_by_lightning_hrp(const char *lightning_hrp);

/**
 * chainparams_by_chainhash - Look up a Lightning protocol chain identity
 */
const struct chainparams *chainparams_by_chainhash(const struct bitcoin_blkid *chain_hash);

/**
 * chainparams_get_network_names - Produce a comma-separated list of network names
 */
const char *chainparams_get_network_names(const tal_t *ctx);

/**
 * chainparams_get_ln_port - Return the lightning network default port by
 * network if the chainparams is initialized, otherwise 9735 as mock port
 */
int chainparams_get_ln_port(const struct chainparams *params);
#endif /* LIGHTNING_BITCOIN_CHAINPARAMS_H */
