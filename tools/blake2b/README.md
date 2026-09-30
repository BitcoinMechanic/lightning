# BLAKE2b port: header hashing and parsing

Patch 1 supplies a standard-library-only Python hash oracle and upstream
vectors. Patch 2 adds the native C hash implementation and an opt-in block
parser path. Patch 3 enables it for an experimental private `xbt-regtest`
network with separate Lightning identity. There is no XBT mainnet entry.
These patches do not establish that funded channels are safe.

Run from the checkout root:

```sh
python3 tools/blake2b/check_headers.py
```

With CLN's normal build dependencies installed, build and run the native tests:

```sh
git submodule update --init --recursive
./configure --disable-rust
make -j8 bitcoin/test/run-block_blake2b bitcoin/test/run-bitcoin_block_from_hex
./bitcoin/test/run-block_blake2b
./bitcoin/test/run-bitcoin_block_from_hex
```

If this checkout is already configured, keep its existing configure options.
The existing Bitcoin test is silent on success. The new test prints five
vector confirmations followed by `Native header and block parser tests OK`.
It is discovered by the existing `bitcoin/test/Makefile` wildcard and included
in `check-units`. Run it from the repository root, or pass the JSON fixture
path as its first argument.

## Native implementation

`bitcoin/block_blake2b.c` computes the Knots hash directly from the 164 serialized
bytes, using existing SHA-256 code and CLN's existing libsodium dependency for
BLAKE2b-256. This avoids reserializing fields or confusing wire time with the
logical timestamp. Its output uses CLN's internal block-ID byte order.

`bitcoin/block.c` reads v2 headers only when `has_blake2b_headers` is true in the
supplied chain parameters and the version high bit is set. Historical v1
headers still use SHA256d. The Elements path remains separate. There is no
mainnet entry. Patch 3 supplies the private regtest network entry and separate
Lightning identity described below.

The parser checks v2 header/body transaction-count agreement and restores the
logical timestamp after hashing, including 32-bit wraparound. It rejects
failed transaction decodes, partial transactions without inputs/outputs, and
counts impossible for the supplied bytes before allocating transaction arrays.
The temporary decoded buffer now belongs to the block so failed parses free it.

Native tests use the unchanged Knots vectors for block IDs and synthetic
transaction bodies for parser checks. These are deliberately not consensus-valid
blocks: the test does not check PoW or merkle roots. Coverage includes both
time-offset modes, wraparound, truncation at every byte, excessive counts,
count disagreement, Bitcoin opt-out, legacy headers, and a synthetic Elements
dynafed header. The existing real Bitcoin block fixture also passes.

Validation performed for patch 2: both native test binaries and the Python
checker pass. The new native test's translation unit (including the production
header hash and block parser sources) also passes AddressSanitizer and
UndefinedBehaviorSanitizer; dependency libraries were not instrumented. Leak
detection was disabled because this execution environment does not allow
LeakSanitizer to inspect `/proc` tasks. No full daemon or channel integration
test has been run.

## Pinned sources

- CLN base: `0c13f1f10295ab9501da8ceff6fa9e391cfb5022`
- Knots tag: `v29.4.2.knots20260508`
- Knots commit: `58398baf33e588779685ead478e6397bb28ed3d6`
- Header serialization: `src/primitives/block.h`
- Hash construction: `src/primitives/block.cpp`, `CBlockHeader::GetHash`
- Vectors: `src/test/data/block_header_v2.json`

The fixture at `tests/data/blake2b/block_header_v2.json` is copied unchanged
from that Knots commit. Upstream: https://github.com/bitcoinknots/bitcoin
Bitcoin Knots/Core is MIT licensed; see the accompanying upstream COPYING.

## Verified by this checker

All five upstream vectors match, including intermediate tagged SHA-256 hashes,
both BLAKE2b-256 passes, ASIC input bytes, the XOR mask, and displayed block ID.
The fixtures cover profiles 0 through 3, enabled/disabled time offset, zero and
nonzero XOR keys, and mask-clear selectors including 255.
Bitcoin genesis retains its historical SHA256d ID. Truncated/overlong inputs
and mismatches between header length and the version high bit are rejected.
These are header-format/hash checks, not full consensus checks.

## Initial source audit and remaining work

1. Native header hashing and opt-in parsing are implemented in patch 2 and
   enabled for the isolated test network in patch 3.
2. Time-on-wire, XOR/reversal, profiles, and mask selectors are covered by the
   native vector tests. Patch 3 also cross-checks a real fork-active Knots block.
3. Patch 3 separates inherited genesis from Lightning identity for private
   tests. A public mainnet identifier and invoice prefix remain undecided;
   the experimental values below are not a public interoperability standard.
4. Knots retains SHA256/RIPEMD160 script hash operations. Do not replace
   Lightning payment hashing with the proof-of-work algorithm.
5. Knots' active reduced-data rules cap ordinary stack elements at 256 bytes
   and restrict tapscript conditionals, among other changes. A successful
   header checker does not establish channel compatibility. Validate actual
   funding, commitment, HTLC success/timeout, revocation, anchor and close
   transactions against the pinned node with fork rules active.
6. Use isolated keys/data and fork-specific funding for the test harness.
   Audit transaction replay across the shared-history chains, including closes.
7. After native support: two-node fork-active tests, including forced closes,
   recovery and reorg behavior. Only then implement the BTC-LN to XBT-LN swap
   coordinator with persistent state, liquidity/rate checks and cross-chain
   timeout margins. BTC-LN to on-chain XBT remains secondary.

Next milestone: run the full smoke test on a host with Unix-domain sockets,
then validate funding, payments, closes and forced-close recovery on regtest.


## Patch 3: isolated XBT regtest

| Setting | Value |
| --- | --- |
| CLN network | `xbt-regtest` |
| Knots RPC chain name | `regtest` |
| Default Knots RPC port used by bcli | `19443` |
| Default Lightning peer port | `20846` |
| On-chain address prefix | `bcrt` (unchanged Knots regtest) |
| Lightning invoice currency prefix | `xbtrt` (`lnxbtrt...`) |
| Lightning chain ID, displayed as a block hash | `bb32a0663fb07db5ea96d8091e8182e749d1bbb891656752eb7e7a790240201d` |

The identifier is SHA256 of the literal ASCII string
`BitcoinMechanic/lightning:experimental:xbt-regtest:v1`, represented in the
usual block-ID byte order. It is explicitly an experimental Lightning domain
identifier, not a new genesis block. The real regtest genesis is unchanged.
No public XBT mainnet identity or invoice standard is being asserted here.

`chainparams_get_chainhash` returns the override for this test network and the
actual genesis hash for every existing network. Protocol messages, gossip,
BOLT12, internal daemon serialization and the wallet network check use it.
The existing wallet database key is still named `genesis_hash` for compatibility,
but stores the Lightning network identity. The `xbt-regtest` subdirectory also
keeps configuration and wallet state separate from ordinary `regtest`.

XBT peer initialization requires an explicit, matching `networks` TLV. A peer
with the ordinary BTC regtest ID, an empty list, or no networks TLV is refused.
Ordinary BTC behavior for peers omitting the TLV is preserved.

The bcli backend requires version 29.4.2 or later and checks `getdeploymentinfo`
on each `getchaininfo` poll: the BLAKE2b fork must be configured at height 1 and
active for the next block. An ordinary Bitcoin/Knots regtest backend is refused.
For this test network, `getchaininfo` also reports `blake2b_active: true` and
`blake2b_activation_height: 1`; lightningd requires these fields, so a replacement
backend plugin cannot silently omit the check. As before, backend plugins are
trusted; these fields are a checked declaration, not a cryptographic proof.
The deployment check establishes the PoW fork schedule, not every consensus
setting (such as RDTS expiry). The harness supplies those settings explicitly.

Build and run unit tests from the checkout root, with the existing venv active:

```sh
source .venv/bin/activate
make -j8
./bitcoin/test/run-xbt-chainparams
./bitcoin/test/run-block_blake2b
./bitcoin/test/run-bitcoin_block_from_hex
```

Run the disposable full smoke test with binaries from the pinned Knots release:

```sh
python tools/blake2b/smoke_regtest.py \
  --bitcoind /absolute/path/to/knots/bin/bitcoind \
  --bitcoin-cli /absolute/path/to/knots/bin/bitcoin-cli
```

The harness uses only Python's standard library. It creates fresh temporary
Knots and CLN data directories, disables external peer discovery, uses loopback
and available local ports, mines test coins, and stops its processes on exit.
It never funds a channel. `--work-dir /new/empty/path` retains logs and data;
the directory must not already exist. Without that argument, temporary files
are removed after success or failure. Prefer a short path for Unix socket limits.

The XBT Knots instance uses `-testactivationheight=blake2b@1`,
`-rdtsexpiry=2147483647`, and a private test headline. A second instance leaves
the fork unscheduled to test rejection. This is a local regtest profile, not
mainnet replay or a claim of full consensus compatibility.

The full test checks live block parsing/hash against Knots RPC, wrong-backend
refusal, XBT-to-XBT peer connection, BTC peer rejection, the XBT invoice prefix,
BTC refusal to pay that invoice, and refusal of a copied BTC wallet database.

Validation performed here:

- Full C build completed with Rust disabled; native identity/header tests pass.
- Real pinned Knots binaries mined both private test chains.
- A live v2 block parsed successfully and its native hash matched Knots RPC.
- The ordinary BTC regtest backend was rejected for `xbt-regtest`.
- The full smoke test reached CLN block loading, then was blocked because this
  execution environment prohibits Unix-domain socket creation. Peer/invoice/
  wallet integration checks therefore remain pending on the user's machine.
  `--backend-only` runs only the subset verified here and prints that limitation.

The full isolation smoke test subsequently passed on the user's tower, including
peer rejection, invoice rejection, and copied-wallet rejection.

### Funded channel test

After the isolation test passes, run:

```sh
python tools/blake2b/funded_regtest.py \
  --bitcoind /absolute/path/to/knots/bin/bitcoind \
  --bitcoin-cli /absolute/path/to/knots/bin/bitcoin-cli
```

This uses the same disposable regtest setup and standard-library-only harness.
It deposits 0.02 test coins into Alice, opens a 1,000,000-sat channel to Bob,
waits for both ends to become ready, pays a 100,000-sat invoice, and requests
a cooperative close with unilateral fallback disabled. It verifies invoice
settlement, the confirmed spend of the funding output, and confirmed unspent
closing outputs in both wallets against Knots. No real coins are used.
Use `--work-dir /tmp/xbt-funded-1` to retain data and logs in a new directory.
Without it, temporary data is removed even on failure.

Funded channels, mutual/unilateral closes, HTLC timeout/preimage recovery, and
the experimental BTC-to-XBT swap scenarios have passed individual regtest runs
on the user's tower. This is still a regtest prototype, not a production service.

## Regression runner

Activate the existing venv, then run from the checkout root:

```sh
python tools/blake2b/regression.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --jobs 2
```

The runner builds nothing and uses the active Python interpreter. It includes
Python unit tests, native header/parser tests, startup isolation, four funded
channel scenarios, and all twenty swap scenarios. Keep the existing CLN and
native test binaries built. Default concurrency is one; `--jobs 2` overlaps
independent cases. Workers share a locked port reservation file. Each live case
gets its own Knots backends, wallets, CLN nodes, and short data directory.

The printed `/tmp/cxr-*` directory retains every case log and live node directory,
plus `summary.json` containing names, exact commands, exit codes, and durations.
Failures do not stop the remaining cases. Overall exit code is 0 only when every
selected case passes, 1 for failures, or 130 if interrupted. Ctrl-C interrupts
active harnesses so their normal cleanup can stop their nodes; queued cases are
marked NOT RUN. The SIGKILL scenarios deliberately kill only their own operators.
This runner does not test power loss, chain reorgs, or production readiness.

List names or select cases (repeat `--only`):

```sh
python tools/blake2b/regression.py --list
python tools/blake2b/regression.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli \
  --only swap-pending-kill --only swap-outgoing-binding --jobs 2
```

Use `--output /tmp/cln-regression-1` for a new named output directory. Remove
retained test directories yourself after reviewing their logs. The copied
controller state and node wallets are disposable regtest fixtures.

## Swap with an on-chain XBT claim

```sh
python tools/blake2b/swap_regtest.py \
  --bitcoind ../bitcoind --bitcoin-cli ../bitcoin-cli --onchain-preimage
```

The XBT receiver force-closes with a 200,000-sat HTLC pending. The harness
supplies the receiver's chosen preimage to its claim hook only after confirming
the commitment. It checks the confirmed HTLC-success witness, the XBT operator's
on-chain preimage extraction, and the receiver's CSV-delayed wallet sweep.
The swap controller remains offline during this sequence, then obtains the
preimage from CLN's completed outgoing payment and releases the 100,000-sat BTC
HTLC. The original BTC channel stays open. XBT on-chain fees apply, so this
scenario verifies recovery outputs instead of the four off-chain balance deltas.
BTC block height remains fixed; cross-chain deadlines and reorg handling are
not covered. The regression runner includes this as `swap-onchain-preimage`.

For the complementary timeout outcome, use `--onchain-timeout` (regression
case `swap-onchain-timeout`). The receiver is stopped with XBT held pending;
the XBT operator force-closes and recovers its HTLC via a confirmed timeout
transaction and CSV-delayed sweep. A pre-expiry controller invocation must
still preserve the held BTC payment. Only after CLN reports the original XBT
attempt definitively failed does controller recovery fail the bound BTC HTLC.
Both BTC channel balances must return to their initial values. XBT recovery
incurs on-chain fees. BTC height again remains fixed in this controlled fixture.
