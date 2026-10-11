# Swap protocol boundary (0066)

These modules are the first port from the installed `pre-privkeyio-port-20261010`
checkpoint to privkeyio's engine at `482c8c521166ddc9e244cd6d44a2d8a55bd0d529`.
They use only the Python standard library and change no C code or signing rules.

`chain_identity.py` validates explicit protocol profiles. Both Bitcoin and the
new XBT fork use `bitcoin`/`bc` on mainnet and `regtest`/`bcrt` in the fixture.
The profiles additionally inspect the `getinfo.our_features` vectors and the
decoded invoice feature vector. XBT requires compulsory bit 512, not optional
513. The pinned engine advertises optional unified capability 515 in init/node;
grant admission must require persisted channel-type bit 514. Names alone are
not evidence of the persisted signing type. BTC and legacy XBT reject these bits.

The explicit binding includes profile, environment, raw network and node ID.
`same_binding()` rejects absent, changed or legacy bindings instead of assigning
new authority. A binding is not a grant and never authorizes a payment. Existing
grant/journal readers have not yet been wired to this new format. The installed
0064 implementation and its original checkouts remain separate.

Feature declarations do not attest the backend. `mainnet_xbt_backend()` preserves
the installed package's synced/unpruned Knots check and BLAKE2b activation hash
at 961640. The new package must invoke this before starting its node. The engine
also rejects 80-byte backend headers at/above its activation height. No mainnet
check accepts a regtest substitute or a shared genesis hash.

`bolt11.py` produces unsigned BTC or new-fork XBT invoices with payment hash,
secret, amount, expiry and CLTV. It includes required bit 512 only for new XBT,
and advertises no multipart support. It cannot hold HTLCs, create grants, send
payments or register an invoice; the ported gate must enroll and validate the
CLN-signed invoice before publication. Legacy `lnxbt` encoding remains in the
old checkpoint and is never silently rewritten.

Run pure tests with `python3 tools/swap/test_chain_identity.py -v`.
The controller keeps a byte-identical copy of `chain_identity.py` and exposes
`Client.inspect_profile()` as a separate read-only adapter. Existing pairing,
grants, workers and recovery paths do not yet use that adapter.

`test_engine.py` always creates fresh temporary directories and starts only
loopback regtest nodes. It checks real RPC identities, wrong-chain refusal,
signed invoice round trips, a funded unified-signature channel and payments
before/after restart. It does not test a complete swap, restart during an HTLC,
on-chain HTLC recovery, legacy funded-channel migration or the full UI package.
The 0066 VM launcher runs it in a network-disabled disposable container with
pinned release binaries and public source files only.

0066a corrects the fixture's plugin selection: `offers` must be enabled because
it owns the `decode` RPC, including BOLT11 decoding. Each node now checks its
required RPCs through `help` before the invoice/channel tests. Failed RPCs print
the method and numeric error code, with full synthetic replies retained in
`rpc-errors.jsonl`; invoice arguments are omitted from exception messages.
Restart logs are appended so the original startup remains available.
Run `python3 tools/swap/test_engine_fixture.py -v` for these harness regressions.

0066b also enables `topology`, which owns `getroute`, `listchannels`, and
`listincoming`. The preflight checks these methods explicitly. The selected
plugin manifests were exercised directly from both pinned BTC/XBT releases;
removing `topology` reproduced the missing methods in both. This checks RPC
registration, while actual node/plugin initialization and the funded scenario
still require the complete disposable VM fixture.

0066c corrects the distinction between registration and runtime availability.
`--developer` defaults to deprecated APIs disabled, and `help` hides `getroute`
because topology marks it deprecated in v26.06. Both versions of the actual
release manifests retain this method even when the getmanifest request sets
allow-deprecated-apis=false. The fixture never invokes this RPC, and this
version of pay computes its route internally. The required-method list now
contains the invoked RPCs and the nondeprecated plugin dependencies used here;
deprecated APIs remain explicitly disabled. Topology stays enabled for
listchannels. Runtime help, actual plugin activity and deprecation configuration
are saved to startup-reports.jsonl, and progress.json identifies the current
stage. Each payment must also be paid on the recipient with the expected hash
and amount; both nodes must have no pending HTLCs before the settled restart.
The complete daemon fixture still requires the packaging VM.

0066d explicitly requests `newaddr bech32` for the fixture's P2WPKH funding
deposit. The no-argument call returns only `p2tr` with deprecated APIs disabled;
assuming a `bech32` result was incorrect. The source branch in
wallet/walletrpc.c and the optional fields in the newaddr response schema were
checked. Unexpected address replies fail before the regtest deposit is sent.

0066e corrects the earlier audit of `pay`: its handler in `plugins/pay.c`
returns -32601 when deprecated APIs are disabled, although registration and
`help` still expose it. The reason is to permit the modern xpay override; this
minimal fixture does not enable xpay. Registration alone was insufficient.

The fixture now uses core `sendpay` plus `waitsendpay`, matching the direct
payment helper used by the pinned fork's unified-channel tests. The legacy pay
plugin is disabled; deprecated APIs remain disabled. Topology stays enabled
for invoice creation's `listincoming` dependency. The route uses the actual
confirmed channel SCID, decoded recipient/amount/secret and invoice final CLTV.
One full part (`partid=0`, `groupid=1`) is sent exactly once per fresh invoice;
uncertain send/wait replies fail without resubmission. Both replies must match
the attempt, and only a completed wait plus the exact recipient receipt passes.
Both channel endpoints must be reestablished before a send. This also closes a
restart readiness gap: a connected peer can precede channel reestablishment.
The original SCID, channel/funding identity and signing type must survive restart.
This verifies a direct channel's engine behavior, not public route selection or
the complete controller's retry/recovery behavior. Those remain later milestones.

Remaining port work: persisted grant/contract versions, gate registration and
incoming bindings, route execution and exact-attempt recovery, both funded swap
matrices, full StartOS XBT identity/backend/UI/backup integration, then an explicit
upgrade path. The upstream wrapper still has package ID `c-lightning`; it must
not be installed as an XBT replacement in this state.
