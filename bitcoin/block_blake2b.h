#ifndef LIGHTNING_BITCOIN_BLOCK_BLAKE2B_H
#define LIGHTNING_BITCOIN_BLOCK_BLAKE2B_H
#include "config.h"
#include <ccan/short_types/short_types.h>
#include <stdbool.h>
#include <stddef.h>

struct bitcoin_blkid;

#define BITCOIN_HEADER_V2_FLAG 0x80000000U
#define BITCOIN_HEADER_V2_SIZE 164
#define BITCOIN_HEADER_V2_TIME_OFFSET 4

/* Hash exactly one Knots v2 serialized header, returning CLN's internal
 * (reverse of RPC display) block-ID byte order.  Does not validate PoW or
 * consensus rules.  Returns false for a wrong size/version; leaves id alone
 * on failure.  Timestamp bytes must be the original time-on-wire. */
bool bitcoin_block_blake2b_hash(const u8 *header, size_t len,
			      struct bitcoin_blkid *id);

#endif /* LIGHTNING_BITCOIN_BLOCK_BLAKE2B_H */
