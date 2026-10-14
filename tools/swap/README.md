# Swap protocol boundary (0066)

Latest addition: **0082** connects the migrated worker through native CLNrest
HTTPS with a private, immutable pairing journal. Both exact grants and node
identities are checked before pairing is saved. Every fresh process retains
the same endpoints, CA bundles and scoped credentials. Certificate/hostname
verification is mandatory; there are no redirects, proxies, raw RPC methods,
fallback transports or automatic retries. Uncertain transport replies preserve
the existing recovery rules. Cached status performs no network requests.

The VM runner retains all eighteen prior funded cases and adds six HTTPS cases:
both directions, failure, worker SIGKILL reply recovery and pending restart.
It verifies and reuses the CLNrest release binaries already present in the
compiled fixture image. `test_customer_https.py` checks transport/pairing
boundaries; `test_https_fixture.py` exercises real local TLS and subprocess
recovery with synthetic responses (requires openssl for disposable certificates).
Only the packaging VM can establish the funded native CLNrest result. No
StartOS installer, live market fetch or production activation is added.

**0080** adds a durable cross-chain worker. It saves explicit
confirmation before enrollment, publishes the original signed customer invoice,
binds the committed incoming HTLC and current per-chain safety margin, then
records its sole outgoing-send intent. Lost replies and fresh processes never
replace the route, price, invoice, incoming binding or outgoing attempt. Unknown
sends remain unresolved. Release requires the original outgoing preimage and
terminal payment/HTLC evidence; failure requires a terminal failed attempt or
proof that this worker never submitted a payment. Both sides must reconcile
terminal history before the worker reports completion.

The VM runner retains both 0078 matrices and adds six actual cross-chain cases:
release, failure and pending restart in each direction, through public prefixes
and private tails. Worker SIGKILL controls cover enrollment, invoice, send and
resolution replies; restart cases restart all eight Lightning nodes and pause
and expire the enrolled grants. `test_cross_worker.py` and
`test_cross_fixture.py` are local tests, not funded evidence. The real matrix
must pass on the packaging VM. This is regtest source only: synthetic price
snapshots, scoped commando transport, no HTTPS, UI or installer activation.
See `coordinator-port.md` for the remaining recovery boundaries.

**0078** adds fresh customer-scoped credentials, bounded
read-only route planning/inspection, and a durable signed customer invoice
publisher. Its separate method and journals do not upgrade 0076 credentials.
`cross_contract.py` binds both protocol-specific grants, a route, incoming terms
and exact Neoxa quote arithmetic; its confirmation and timing checks are data
policy, not an executing controller. Local tests are `test_customer_invoice.py`,
`test_cross_contract.py` and `test_customer_fixture.py`. The VM runner retains
the 0076 remote matrix and adds six funded signed-invoice recovery cases with
no corresponding core invoice entry. Full cross-chain execution remains next;
there is no installer, HTTPS activation or live market feed in this patch.

**0076** adds fresh grant-scoped rune credentials and a static
regtest remote facade. Lost mint replies recover the exact stored credential;
uncertain absence never permits another mint. The funded fixture uses real CLN
commando authentication on both engines, including raw-method refusals and
original-attempt recovery. It does not enable StartOS HTTPS or the cross-chain
controller. See `coordinator-port.md` for the precise boundary. Run local tests
with `python3 tools/swap/test_remote_authority.py -v` and
`python3 tools/swap/test_remote_fixture.py -v`; funded execution requires the VM.

**0074** adds versioned local bounded authority, atomic
enrollment/budget reservation, a durable one-send intent, exact incoming/attempt
reconciliation and terminal HTLC evidence. See
[coordinator-port.md](coordinator-port.md) for the source/API audit and scope.
Run `python3 tools/swap/test_bounded_authority.py -v` and
`python3 tools/swap/test_authority_fixture.py -v` locally. The funded authority
matrix requires the isolated VM runner; local tests are not runtime evidence.
The authority has no remote credential or installed endpoint. Cross-chain
policy, price confirmation, HTTPS grants and the full worker remain separate.

These modules are the first port from the installed `pre-privkeyio-port-20261010`
checkpoint to privkeyio's engine at `482c8c521166ddc9e244cd6d44a2d8a55bd0d529`.
Those initial modules use only the Python standard library. The separate 0070
core lifecycle change is described below; signing rules remain unchanged.

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

## Important-plugin death (0070)

0068a's eight funded recovery scenarios were reported passed and preserved in
the annotated `privkeyio-gate-20261011` checkpoint. 0070 changes
`lightningd/plugin.c`: a dying important plugin's pending requests are freed
without synthetic failure callbacks, and shutdown is requested before startup
completion callbacks or stop notifications can initiate more work. Ordinary
shutdown takes precedence; a non-important plugin retains the existing
PLUGIN_TERMINATED behavior. Pending-request destructors still remove the map
entries while the plugin object exists. No HTLC, invoice or signing rule changes.

The pinned BTC v26.06.9 and XBT v26.06.9-blake2b.7 plugin.c files are identical
before this fix. `important-plugin.patch` carries the same change for the BTC
source build; the build runner verifies byte-identical resulting files. Fixture
binaries use explicit `-swap0070` versions and remain outside package installers.

`test_plugin_teardown.py` compiles the actual destructor body with observation
stubs. It checks request-callback suppression, startup completion ordering,
ordinary shutdown and non-important-plugin behavior. It is not a memory-lifetime
or funded daemon test. The unchanged body must fail this regression.

`test_plugin_crash.py` has three disposable regtest modes. Baseline mode uses
the original binaries to expose important-plugin hook fallthrough. Regression
mode reruns the eight whole-node restart scenarios on the source-built engines.
Patched mode kills the actual gate child with SIGKILL during hold and after
durable release/failure intent. It requires exit code 1 from lightningd, an
unchanged committed incoming HTLC row before restart, unchanged gate journal,
and the original pending sender attempt. Recovery must retain funding, signing
type, HTLC binding and attempt identity, and produce the expected preimage or
failure plus exact channel balance deltas. Ordinary payments before/after and
non-important-plugin fallthrough are controls.

The crash fixture deliberately registers gate quotes for ordinary core invoices
as a fallthrough trap: the receiver already has the preimage and could settle
them if the gate is skipped. Actual gate invoices remain independent of the
core invoice database. The fixture sends one full part once, checks real child
process ownership before signaling, and opens the core database read-only. The
PID marker and terminal pause exist only in `fixture_gate_plugin.py`.

Local checks are not proof of the complete crash matrix. The VM runner emits
READY only after the original-binary controls, patched restart matrix and
patched SIGKILL matrix all produce complete receipts. This covers process death,
not power loss, backup rollback, on-chain timeouts, public routing or controller
integration. Production activation and installers remain unavailable.

0070a corrects the crash fixture, retaining the exact 0070 compiled core fix.
The first baseline run logged hook fallthrough and a complete HTLC set before
important-plugin shutdown, but the offers plugin's asynchronous invoice_payment
hook delayed core settlement. The old assertion incorrectly required a durable
fulfillment with that downstream hook still running. After the trap invoice has
been created, decoded and held, 0070a stops offers on the disposable receiver
and verifies the complete remaining plugin set. This is done in both original
and patched fault scenarios. Fresh daemon starts restore all selected plugins;
the whole-node restart regression continues using the original plugin set.
The baseline must still persist the exact trap preimage. Patched crashes still
require the full incoming row and journal to remain unchanged, and now also
reject any core hook-continuation or invoice-resolution trace after SIGKILL.
The non-important control verifies the exact sender preimage. A small diagnostic
record captures HTLC state and preimage presence before an assertion can fail.
No payment, gate, core or service configuration is changed by this correction.

## 0094 deadline protection candidate

The separate regtest coordinator supports explicitly reviewed incoming grants
with `close_before_blocks: 72` and format `swap-bounded-authority-v2`. Existing
grants and credentials retain their previous authority. Before its single send,
the worker arms the exact committed incoming HTLC and rechecks the contract's
remaining-block margin. The node's customer facade watches armed enrollments
locally, including while the controller or outgoing coordinator is unavailable.
It records the exact close intent before dispatching `close` once. A lost reply
or crash between intent and dispatch never authorizes a second close or payment.
Admission expiry and pause do not stop enrolled recovery; restore markers do.

A narrow engine patch allows an already committed final HTLC to reach its held
gate after a closing-channel restart. New arrivals and replayed forwarding HTLCs
remain refused there; a recovery hook that continues cannot invoke a core invoice.
The original important-plugin shutdown/crash fix and the XBT package’s 0088b
recovery fee-weight patch are retained in the new build. A saved preimage
uses the core's existing persisted fulfillment and onchaind claim path.

The scoped `chain` operation checks fresh funding-spend, HTLC witness, expiry,
CSV sweep and wallet observations. It does not cache success. Confirmations and
wallet ownership are supplied by the paired CLN; the funded fixture separately
checks confirmed backend blocks and reconciles every satoshi. A close/release
reply is never settlement proof. On-chain cases remain `onchain_recovery`,
nonterminal and unavailable for grant renewal or another swap in that journal.
0096 adds the separate, explicitly reviewed retirement flow described below.

The 0094 runner requires original-binary replay controls, six patched funded
cases in both directions (claim, timeout, closing restart), and the prior six
service cases on the new engines. Local unit checks are not funded acceptance.
The existing 0093 evidence keeps its original provenance. This is a source/test
candidate only: no installer, production activation, coordinator backup support,
legacy-channel migration, multipart payments or automatic payment retries.


### Reviewed on-chain retirement (0096 migration)

After a protected incoming channel closes, recovery stays nonterminal until an
operator explicitly retires it. `coordinator_admin_cli.py` now accepts local
`recovery_review` (`entry_id`) and `recovery_confirm` (`entry_id`, the returned
`confirmation`, `confirmed: true`). Review does not write authority. Confirmation
freshly checks the synced node, absence of the original active channel, the exact
closed funding/peer/signing type, and raw claim-plus-wallet-sweep or timeout
transactions. The resolving proof must have at least 100 confirmations. A close
reply, a status string, and a preimage alone cannot retire an entry.

CLN prunes closed HTLC rows on restart. The previously persisted HTLC identity,
actual incoming binding, decision and close intent remain mandatory; missing
rows never permit rebinding. Spent wallet sweep outputs can still establish
historical ownership via `listfunds(spent=true)` and the freshly confirmed raw
transaction. Confirmation heights and wallet ownership remain authenticated CLN
observations, not independent chain proofs. The funded fixture also reconciles
transactions against its separate regtest backend.

Local retirement does not finish the cross-chain swap. The controller's
`recovery_review` (`cid`) and `recovery_confirm` (`cid`, `confirmation`,
`confirmed: true`) independently reread both original scoped enrollments. Only
the exact terminal outgoing attempt, paired with the corresponding reviewed
incoming claim or timeout, permits `onchain_settled` or `onchain_failed`. These
are local CLI actions for this migration milestone. No remote rune gains a new
operation. Existing credentials keep their previous scope.

Retirement appends evidence; it does not delete history, refund grant slots,
change the original route/attempt, or authorize another payment/close. Lost
confirmation replies recover the same durable record. Reviewed grant renewal
requires currently eligible funding, never the closed channel. Authority
admission, controller admission and service recovery checks reread retired
history; missing evidence, a changed proof, a lower observed height, or an
unavailable coordinator blocks further work. Restore fences still refuse all
execution. Retirement review tokens bind the exact evidence rather than a wall
clock expiry; every confirmation repeats the checks.

`test_retirement_engine.py` extends all six funded deadline scenarios through
closed-channel history, real HTLC pruning on restart, separate confirmations,
controller SIGKILL after retirement, and renewal on new funding. Run the 0096
acceptance bundle on the packaging VM before treating these additions as funded
acceptance. The 0096a repair rebuilds BTC with the anchor-recovery witness
weight correction; the XBT binary and all prior evidence are retained. This
milestone produces no installer and does not change installed services.

0096a fixes the BTC `UTXO_P2WSH_FROM_CLOSE` fee estimate. The earlier 0088b
package overlay corrected this only for XBT. The posted BTC transaction weighs
891 WU (223 vbytes); the old estimator omits 36 WU for its 37-byte witness
script. At 253 perkw it paid 216 sats. The corrected estimate covers the full
script, including the largest supported lease CSV, and requires no fee override.
`btc-recovery-fees.patch` applies only to the pinned BTC release; the existing
XBT overlay remains unchanged. `test_btc_recovery_weight.c` checks seven signed
CSV witnesses against the original and patched estimator. The retirement fixture
explicitly spends an original recovered output in every replacement funding
transaction at default fees and checks backend relay acceptance. No installer
or production activation is added; funded acceptance remains required on the VM.
