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

## Protocol-bound incoming gate (0068)

The operator reported the complete 0066e engine fixture passed on the packaging
VM, then exported, pushed and synced checkpoint `privkeyio-engine-20261010`.
0068 adds an isolated incoming gate on that source checkpoint. It does not wire
the new gate into package activation, grants, controller execution or the UI.

`held_gate.py` uses a new schema-2 journal with format `swap-held-gate-v1`.
Every journal and quote carries the explicit BTC/new-XBT protocol binding.
The arriving hook is matched to the actual confirmed local channel and its
committed, untrimmed HTLC using read-only `getinfo` and `listpeerchannels`.
The persisted binding includes the channel ID, SCID, peer, funding transaction
and output, numeric signing type, and HTLC ID. Stable amount, hash, secret and
absolute expiry fields are saved before the gate reports `held`.

Admission accepts exactly one full payment part on a channel in the quote's
approved set. There is no customer SCID entry or outgoing payment RPC in this
gate. The future grant adapter must supply the approved set; this fixture uses
the actual channel directly. The initial implementation requires confirmed
SCIDs and does not admit zero-conf/alias-only channels or splice transitions.

Recovery requires the same snapshot and funding binding. It does not renew
admission expiry or impose a fresh connected/reestablished requirement while
the engine is replaying held HTLCs before peer connections. New admission does
require a synced node and a reestablished channel. Mismatched accepted replays
remain unresolved and cannot be released by this process. Node/RPC uncertainty
during an accepted replay similarly requires inspection and a clean restart.

Release/failure intent is saved before either the hook reply or operator reply.
The exact terminal request is idempotent; the opposite terminal request refuses.
`release_intent` is deliberately not called `settled`. Status omits the payment
secret, preimage and raw hook. Journal writes use a private directory, no-follow
file opens, a lifetime writer lock, exclusive temporary files, file fsync,
atomic replacement and directory fsync. An uncertain write permanently blocks
that process and leaves hooks unresolved. Missing, legacy, malformed or changed
identity records refuse startup; the plugin never creates replacement journals.

`gate_plugin.py` is static and requires startup on regtest with an explicit
protocol option and a pre-created fixture journal. `fixture_gate_plugin.py`
adds a test-only pause after terminal persistence, before any response. Neither
plugin is included in a StartOS service image or accepts mainnet activation.
`initialize_fixture()` is only for fresh synthetic regtest state; it is not an
activation, backup, restore or journal-migration API.

The new `test_gate_engine.py` reuses the tested 0066e Lab, without modifying it.
On each chain it funds a fresh channel, checks ordinary payments through the
gate, and runs release/failure after restarting both endpoints while held. It
also interrupts each terminal reply after its durable write and restarts both
nodes to replay the original decision. Every scenario checks the same funding,
HTLC and outgoing part/group/created-index, plus the exact settled balances on
both endpoints. It then checks startup refusal of legacy, wrong-profile and
missing journals. This is direct-channel gate recovery, not a routed swap or
full controller recovery test. It keeps deprecated APIs disabled and never
retries a sendpay after an uncertain response.

### Required engine fix before live swap authority

Inspection of this pinned core found an important lifecycle hazard:
`lightningd/plugin.c:destroy_plugin` synthesizes PLUGIN_TERMINATED for pending
requests before processing the plugin's importance. In
`lightningd/plugin_hook.c:plugin_hook_callback`, that error advances to the next
hook/final callback. An important-plugin shutdown alone therefore does not
guarantee a held payment stays unresolved when the plugin dies independently.
Normal whole-node shutdown is different: it enters LD_STATE_SHUTDOWN and clears
pending requests before plugin destruction. These paths are not interchangeable.

0068's runtime tests cover whole-node graceful restart and lost terminal replies.
They DO NOT establish plugin-only crash safety, power-loss/fs rollback safety,
on-chain timeout recovery, restored-backup safety, routing/grant/controller
integration or migration of funded legacy channels. A core change must prevent
held-hook fallthrough on important-plugin death and pass process-crash tests on
both engines before this gate can authorize outgoing swap funds. Production
activation is intentionally unavailable here. The fixture receipt records
`plugin_only_crash_safe: false` and `production_activation_supported: false`.

Run local tests with `python3 tools/swap/test_held_gate.py -v` and
`python3 tools/swap/test_gate_fixture.py -v`. The full engine fixture requires
the packaging VM; local pure/subprocess checks are not funded runtime evidence.

0068a fixes the terminal-reply fault injector's process lifetime. Its original
sleep loop ignored stdin EOF. Ordinary CLN shutdown closes a plugin's pipes but
does not guarantee that the process is killed. The paused child could therefore
outlive lightningd with its journal lock held, causing replacement startup to
refuse. This was reproduced locally using a real child process, pipe and flock.
The corrected pause drains input without dispatching or replying, exits on EOF
via SystemExit, and lets the plugin's finally block close its journal lock.
The fixture also waits for that original lock to become available before
starting a replacement; it never deletes the lock file or forces a lock bypass.
New real-process tests verify no terminal reply escapes, the recorded intent
is retained, and a fresh writer can acquire the same lock after pipe closure.
The gate's journal format, binding rules and release/failure semantics are
unchanged. The separate important-plugin death blocker is still unresolved.
