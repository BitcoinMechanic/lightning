#include "config.h"
#include <assert.h>
#include <bitcoin/privkey.c>
#include <bitcoin/pubkey.c>
#include <bitcoin/script.c>
#include <bitcoin/shadouble.c>
#include <bitcoin/signature.c>
#include <bitcoin/tx.c>
#include <bitcoin/varint.c>
#include <ccan/str/hex/hex.h>
#include <common/setup.h>
#include <common/utils.h>
#include <stdio.h>
#include <string.h>

/* Compare the estimator against serialized, signed recovery witnesses.
 * A baseline control must demonstrate underestimation for every CSV case. */
int main(int argc, const char *argv[])
{
    const u32 csvs[] = { 1, 16, 17, 127, 128, 4032, 32767 };
    struct privkey key;
    struct pubkey pub;
    struct bitcoin_signature sig;
    struct sha256_double hash;
    bool baseline = argc == 2 && strcmp(argv[1], "--expect-underestimate") == 0;
    size_t shortfalls = 0;

    assert(argc == 1 || baseline);
    common_setup(argv[0]);
    chainparams = chainparams_for_network("bitcoin");
    memset(&key, 1, sizeof(key));
    memset(&hash, 0, sizeof(hash));
    assert(pubkey_from_privkey(&key, &pub));
    sign_hash(&key, &hash, &sig.s);
    sig.sighash_type = SIGHASH_ALL;

    for (size_t i = 0; i < sizeof(csvs)/sizeof(csvs[0]); i++) {
        const u8 *script = bitcoin_wscript_to_remote_anchored(tmpctx, &pub, csvs[i]);
        const u8 *signature = stack_sig(tmpctx, &sig);
        size_t slen = tal_bytelen(script), siglen = tal_bytelen(signature);
        /* Outpoint, empty scriptSig length, sequence, then witness vector. */
        size_t actual = (32 + 4 + 1 + 4) * 4 + varint_size(2)
            + varint_size(siglen) + siglen + varint_size(slen) + slen;
        size_t estimate = bitcoin_tx_input_weight(false,
            bitcoin_tx_input_witness_weight(UTXO_P2WSH_FROM_CLOSE));
        assert(slen >= 37 && slen <= 39 && siglen <= 71);
        if (estimate < actual) shortfalls++;
        printf("csv=%u script=%zu signature=%zu actual=%zu estimated=%zu\n",
               csvs[i], slen, siglen, actual, estimate);
        if (!baseline && estimate < actual) return 1;
    }
    if (baseline && shortfalls != sizeof(csvs)/sizeof(csvs[0])) return 1;
    puts(baseline ? "BASELINE: all seven recovery witnesses underestimated"
                  : "PATCHED: all seven recovery witnesses covered");
    common_shutdown();
    return 0;
}
