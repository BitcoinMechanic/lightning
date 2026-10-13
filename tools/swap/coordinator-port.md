# Coordinator port boundaries (0082c)

## Transport-specific authorization errors (0082c)

The 0082b HTTPS fixture reached both funded topologies, then asserted the
commando authorization wrapper code 19537 against a native CLNrest response.
CLNrest forwards the core `checkrune` error: a scope restriction is HTTP 401
with code 1502, as the pinned `tests/test_clnrest.py` also requires. A validly
encoded rune with an altered signature returns HTTP 401 with code 1501.
Positional arrays are a distinct parser rejection (HTTP 500, no RPC code),
before authorization. None of these responses proves a permitted mutation.

The fixture now checks each case strictly, including the error-object shape,
and records only its fixed case/profile/role labels, HTTP status and numeric
RPC code in `https-scope.jsonl`. It never includes remote messages, data,
parameters or credentials. All denied calls must leave the mutation log
unchanged. Synthetic regression tests cover all 48 requests on both chains
and both coordinator roles; funded enforcement still requires the VM run.
No credential restrictions, daemon binaries or worker behavior change.

The runner retains the original eighteen completed 0082a peer-case receipts
with their own unchanged provenance and runs six fresh HTTPS cases. Native
startup evidence keeps its original 0082b fixture identifier; that preflight
has not changed.

## Explicit native plugin registration (0082b)

The 0082a run completed the retained eighteen funded cases, then the first
HTTPS node rejected `--clnrest-host` as unknown. The 0070 engines were built
with `--disable-rust`: `plugins/Makefile` excludes CLNrest from the generated
built-in list, and `plugins_set_builtin_plugins_dir` loads that list rather
than scanning installed files. Copying the pinned binary alone was insufficient.

The HTTPS fixture now explicitly registers the corresponding prefix's CLNrest
with `--important-plugin` before startup and checks it is active before TLS
readiness. Restarts keep one registration, the original port and certificates.
Intermediate nodes keep CLNrest disabled. No daemon binary or worker changes.

`test_clnrest_startup.py` drives the actual pinned BTC and XBT release plugins
through getmanifest/init, verifies certificate-authenticated HTTPS with
missing-rune rejection, and restarts on the same certificates after pipe EOF.
This needs no daemon RPC or funded wallet and is a preflight, not payment
evidence. The packaging runner can retain the exact completed 0082a remote,
customer and peer-worker receipts after checking their original provenance,
unchanged runtime sources and matching backend/checkpoint pins. It runs six
fresh funded HTTPS cases and keeps retained provenance separately. A full run
without prior receipts starts with HTTPS to catch startup errors before funding
the other matrices.

## Reply-time admission correction (0082a)

The retained peer cross-chain matrix exposed confirmation sampling `now`
before its two inspections. A normal reply across a second boundary was
therefore misclassified as future evidence. Confirmation now samples time
after both replies and uses that time for admission and durable approval.
The 30-second observation bound, future-timestamp rejection and quote expiry
remain unchanged. A delay that makes either observation stale or crosses
quote expiry refuses before approval or enrollment. Other worker admission
paths already sample time after inspection; their behavior is retained.

The same audit found pairing could save grants that expired during its
read-only identity calls. Both grants are now checked against the time after
both replies, before the pairing write. Deterministic regressions reproduce
the original confirmation refusal in both directions and the pairing expiry
case. Unit/TLS tests are local evidence; the complete twenty-four funded VM
cases are still required. No compiled engine or credential scope changes.

## Saved HTTPS pairing and native CLNrest (0082)

Base: annotated `privkeyio-cross-worker-20261012`, engine tree
`7746f2d1dee1169d3cb55826eee769f8d6d10d30`. The operator reported all eighteen
0080 cases passed, then completed the 0081 tower checkpoint and VM sync.

`customer_https.py` exposes only `swap-customer-call` and its existing named
operation schemas. Endpoints are canonical HTTPS origins. The supplied CA
bundle is the trust store, with hostname verification and TLS 1.2 minimum.
The client has no proxy environment support, redirect following, raw RPC,
transport fallback or automatic retry. Every operation first checks the exact
saved grant through authenticated `info`; the coordinator validates its current
node identity before returning it. Recovery may inspect expired/paused grants.
The worker and node remain responsible for new-admission checks.

CLNrest's pinned success response is HTTP 201 with a JSON object. Request and
response size are bounded; duplicate keys and non-finite JSON constants refuse.
All HTTP/TLS/parse failures return a fixed private-safe uncertainty label. A
refused or missing response never means that a send or release did not happen.
The existing sole-send rule remains in the unchanged cross-chain worker.

`worker_pairing.py` stores a separate private, locked, atomic
`swap-worker-https-pairing-v1` journal. Explicit fresh setup verifies both
coordinators with read-only calls before saving direction, grants, endpoints,
CA bundles and scoped credentials. The binding is immutable. Every fresh worker
loads it and checks all contracts against its exact grants before any request.
Missing pairing is not reconstructed during recovery. Credential rotation and
grant renewal are separate service integration work. No old authority or
pairing is promoted to the migrated protocol.

Lock order is pairing then worker. An uncertain pairing write freezes that
instance; a fresh process reads the durable result. Either restore marker
blocks execution and invoice access. Cached status remains available, without
credentials, endpoints or network requests. This is a restore barrier, not
proof that executing a restored backup is safe.

The 0082 fixture adds six funded cases through native CLNrest HTTPS, retaining
the prior eighteen peer-RPC cases. It reuses both compiled C engines. Their
prefixes lack Rust plugins, so the overlay checks and copies only CLNrest from
the exact BTC release and original XBT release already in the image. No C/Rust
rebuild or external download is required. A fixed binary hash mismatch refuses
before funded execution.

CLNrest generates per-node certificates in disposable directories. Rune checks
exercise wrong methods, grants, parameter counts, operations and forged runes;
wrong CA/identity refuses. JSON arrays are rejected by CLNrest parsing before
rune authorization, and are checked separately. Coordinator logs remain local:
CLNrest can log private request parameters, so raw logs and credential, pairing,
worker and payment journals never enter the diagnostic archive.

Recipient route planning also uses the scoped HTTPS client. The HTTPS worker
process receives configuration only during explicit pairing.
Subsequent processes receive an action and contract ID and read saved pairing.
Both directions retain confirmation, one part/attempt, public-prefix/private-
tail routes, worker SIGKILL after four reply boundaries, pending daemon restart,
paused/expired enrolled recovery, original invoice/bindings/HTLCs/attempt and
exact twelve balances. The shared cross-chain scenario has an injectable test
driver; the prior worker algorithm and peer fixture behavior remain unchanged.

Local tests also use real TLS sockets with synthetic coordinator responses,
including wrong CA/hostname, redirect refusal, actual process kills and a send
reply dropped after execution. These are not funded CLNrest evidence; the VM
must run all twenty-four funded cases before READY.

Scope remains regtest source integration. StartOS service wiring and full node
UI, live market fetching, on-chain/backup recovery, legacy funded-channel
migration and production activation remain pending. Pairing does not replace
the installed controller's existing read-only probe or live execution paths.

## Durable cross-chain worker (0080)

Base: annotated `privkeyio-customer-20261011`, engine tree
`185c768045321a6126c7c1c85d9b019a1c6ff8c9`. The operator reported both 0078
matrices passed, completed the 0079 tower checkpoint, then synced the VM.
This patch changes only `tools/swap` Python sources and documentation. It reuses
the exact compiled crash-fixed engines and preserves existing authority scope.

`cross_worker.py` owns a new private, locked, atomically replaced
`swap-cross-worker-v1` journal. Missing state is not automatically recreated;
old worker formats are not imported. The contract digest binds both grants,
full outgoing route, independent incoming secret, immutable market terms and
expiry. Confirmation is persisted before either enrollment. Enrollment IDs are
derived from the exact grant and payload and checked on every reply. Enroll,
publication and resolution intents recover only their same idempotent operation.
One unresolved swap prevents preparing another in that worker journal.

The authenticated transport is supplied by a trusted caller and must pin its
endpoint and saved customer credential to the contract's grant. The worker has
no raw local RPC, route search, credential minting, grant renewal or repricing.
The fixture driver uses CLN commando, whose peer ID and rune enforce that
boundary. A production HTTPS/pairing transport remains separate work.

The existing scoped `inspect` operation now also accepts exactly an `entry_id`
owned by that grant. It returns current synced height, reserve, grant status
and readiness of the bound channel
without trying to advertise an already-held receiving channel. It is read-only;
the operation/rune sets and all payment permissions remain unchanged. Old
`swap-authority-call` credentials still cannot call the customer interface.

Before sending, the worker stores fresh enrolled inspections, the exact
committed incoming channel/HTLC/expiry and the unsent outgoing enrollment. It
checks reserves, active grants, quote expiry and remaining incoming blocks
against the saved outgoing relative delay plus safety margin. Heights of the
two independent chains are never subtracted. The durable send intent precedes
the single outgoing send call. After that intent, the worker only observes the
original attempt; even an empty node record cannot authorize resubmission or
incoming failure. A crash before request delivery therefore remains uncertain.

Every observed entry is revalidated against the original grant and payload.
Previously recorded fields cannot disappear or change, except the defined
publication and pending-attempt progressions. A pre-existing unowned send or
resolution is refused. Release requires the outgoing attempt's validated
preimage and terminal HTLC history. A failed outgoing attempt must also be
terminal before failure is propagated. Release/failure intent is saved before
the remote gate call; a reply alone never means the incoming HTLC settled.
Completion requires both node authorities' terminal evidence.

Paused or expired grants continue enrolled recovery after send. Before any
send intent, an expired but held quote can fail safely; explicit cancellation
has the same no-send requirement. Unsent outgoing reservations retire only
after recipient-invoice expiry with the node's empty-history checks. Unpaid
published quotes likewise wait for outgoing retirement. Interrupted setup that
cannot recover its original enrollment/publication remains blocked; this patch
does not invent an abandonment operation or bypass authority history. A restore
marker blocks all execution and invoice access while preserving redacted cached
status. Closing-channel/on-chain recovery and restored-backup handling remain
separate work; this is not an on-chain recovery or power-loss claim.

The funded fixture uses four nodes on each of two independent chains. Both
coordinator legs have a two-edge public prefix and private final channel; neither
customer has a direct channel to its coordinator. BTC and XBT heights differ.
The recipient receives 4,000 sats. Fixed synthetic Neoxa-shaped snapshots at
bid 0.49 / ask 0.5 and zero markup produce different incoming amounts including
the outgoing fee, both above the previous 2,000-sat fixture's dust threshold.
No live market fetch is claimed. The outgoing recipient fixture holds its own
invoice to control failure/restart; only that fixture owns its preimage. The
worker learns it from outgoing payment evidence and controls incoming release.
Incoming customer hashes must have no core invoice entry.

Each worker operation runs in a fresh process. SIGKILL after successful remote
enrollment, invoice publication, payment send and resolution checks reply-loss
recovery without a second payment. Both directions test release, failure and
all-eight-daemon restart while pending; enrolled grants are then paused and
their fixture clocks expired. Receipts require unchanged contract, invoice,
actual incoming binding, both HTLC histories, original attempt and all twelve
channel balances. The prior twelve remote/customer cases also run on the new
overlay. Only the VM's complete eighteen-case receipt set can print READY.

Scope remains isolated regtest source: no StartOS installer, production node
activation, full UI/pairing, HTTPS/CLNrest, live market feed, on-chain recovery,
automatic payment retries, MPP or legacy funded-channel migration.

## Customer contract and publication interface (0078)

Base: annotated `privkeyio-remote-20261011`, engine tree
`45d99f93e565b851b941bec1aed5d0b9567fe34a`. The operator reported the complete
0076 remote matrix passed, then completed the 0077 tower checkpoint and VM
sync. This patch reuses the compiled engines and all bounded payment rules.

`customer_remote.py` uses new method `swap-customer-call`, fresh explicit local
`swap-customer-credential` setup and format `swap-customer-remote-v1`. Old
`swap-authority-call` runes cannot invoke it; old journals are never relabelled.
Its existing enroll/send/release operations retain the same finite local
authority. Incoming adds `inspect` and `invoice`; outgoing adds `inspect` and
`plan`. No request chooses an RPC method, directory or widened planning limits.
The old facade still has exactly its previous operations and RPC allowlist.

`plan` takes only a recipient invoice and uses the grant's approved pins and
fee/delay/hop ceilings. It creates no enrollment or payment. `inspect` returns
the exact grant, fresh identity-checked height and confirmed unreserved wallet
balance; incoming inspection also checks receiving liquidity, remote hint
policy and the conservative fee/dust threshold. No endpoint, credential or
raw wallet output is returned. The caller must treat inspection as private
execution data, not operator status.

`customer_invoice.py` requires an already registered incoming enrollment. It
saves timestamp, hints and unsigned bytes in a third locked journal before
signing. Hints use the peer's remote alias when required; admission is still
bound to approved funding/signing-type pins. A lost signature reply can only
sign the same bytes. A saved invoice reply is returned unchanged after pause,
expiry and restart. Every signed/cached reply is decoded by CLN and checked
for profile, hash, secret, payee, exact amount, description, expiry, final CLTV,
all hints and the exact one-part feature vector. No core invoice or preimage
is created. First publication after expiry refuses. Write uncertainty freezes
the instance; a restore marker in any journal blocks the whole facade. Lock
order is authority, credential, publication, with fresh state per operation.

`cross_contract.py` defines `swap-cross-contract-v1`: both immutable grants,
explicit BTC/new-XBT identities, exact outgoing plan, independently secreted
incoming quote and a Neoxa price snapshot. `market_quote.py` ports the installed
controller's quote arithmetic unchanged, without its legacy profiles or grant
logic. The quote includes routing cost, uses asks forward and bids reverse,
and defaults to zero markup when the caller supplies the normal zero setting;
the saved markup is explicitly confirmed. There is no HTTP fetch in this port.
Every term contributes to the confirmation digest. New admission checks fresh
observations, 50,000,000-msat confirmed unreserved reserves on both nodes,
grant budgets and expiry. The incoming minimum covers the outgoing relative
delay plus 6 propagation and 144 recovery blocks; the payer invoice adds 24
blocks. Heights on independent chains are never subtracted from each other.
The held-margin helper requires the exact remote enrollment and committed
incoming binding. Recovery must not reprice or replace this contract.

The contract is policy/data, not durable approval or a worker. The next worker
must save explicit confirmation before enrollment, preserve both enrollment
IDs and actual incoming/attempt evidence, record its sole-send intent, release
only from a validated outgoing preimage, and keep unknown sends unresolved.
Closing-channel/on-chain recovery and backup restore remain separate gates.

The 0078 VM matrix first repeats 0076 against the shared credential refactor.
It then tests independently signed customer invoices over two public edges
and a private final channel, on both engines, with release, failure and held
restart. Customer hashes must be absent from the core invoice database before
and after payment. Fresh workers must return identical invoice bytes and HTLC
binding after lost sign/publication/resolution replies; all six channel balances
and the original one-part payment attempt must agree. The fixture's customer
pays a 374-block final delta; this does not widen the coordinator's 288-block
outgoing grant. Old-rune/raw-method refusals and read-only planning are checked
through actual commando. Local tests and synthetic receipt tests do not prove
the funded matrix; only the VM verifier can report READY.

Scope: no complete cross-chain worker, StartOS installer/UI activation,
HTTPS/CLNrest, live market-priced settlement, on-chain recovery or legacy funded
channel migration is claimed by 0078. The installed swap flow stays separate.

## Remote authority boundary (0076)

Base: annotated `privkeyio-authority-20261011`, engine tree
`33d099923dfbf993b2c0806f3634fa76385a611c`. The operator reported all six
0074 local authority scenarios passed and completed the 0075 tower checkpoint
and VM sync. Compiled engines and the local authority format stay unchanged.
This adds a static disposable-regtest RPC facade, with no StartOS activation.

`remote_authority.py` uses a separate private locked journal with format
`swap-remote-authority-v1`. Every operation opens fresh state, acquiring the
authority lock before the credential lock. Missing, restored, legacy and
cross-profile state refuses. Plugin paths use CLN's network directory from
init, including its `regtest` component.

Local `swap-authority-credential(grant_id, confirmed)` exposes a rune for an
existing approved grant. Durable mint intent precedes the sole `createrune`
call. A lost reply is recovered through `showrunes` by the exact restriction
set; a known rune and unique ID must remain unchanged. Missing or duplicate
matches, changed credentials and revocation refuse without reminting. An
uncertain journal write blocks that instance. Old grants are never promoted.

The rune allows only `swap-authority-call`, one exact grant ID, exactly three
named parameters, and the operations for that grant's incoming/outgoing kind.
The facade checks the exact fields and ownership of every enrollment. It has
no raw RPC, grant creation/renewal/pause, route search, close or credential-mint
operation. Admission expiry and pause are enforced by the existing authority;
there is no rune time caveat that would cut off enrolled recovery. Core rune
blacklisting still revokes access.

Enrollment requires a confirmation digest of the exact payload. This binds
the caller's request, not proof of a human decision, incoming funds on another
chain, a price or a timing policy. The bearer is a trusted controller with the
finite local grant. Authenticated `evidence` returns only a selected enrollment
and may contain its preimage: this is private execution data, not user status.
`info` omits credentials and never rewrites the authority/credential journals.

`authority_plugin.py` has no HTLC hook and only a fixed local RPC allowlist.
CLN's authenticated transport must check the rune before dispatch. Local Unix
RPC remains administrative. The fixture uses the real pinned C `commando`
transport; it invokes credential APIs and authenticated request/refusal controls
before funding. It covers both engines' routed release/failure/held restart,
raw-method/wrong-grant/positional/extra-field/forged-rune refusals, lost mint and
payment-operation replies, paused/expired recovery, budgets, original HTLC and
attempt, exact six balances, restore barriers and core revocation. Fault clocks
and reply loss exist only in the separate fixture plugin. Diagnostic archives
omit raw daemon/RPC logs and credential journals.

Schemas and transport code were audited in both pinned source trees. Local
tests are not funded runtime evidence: the VM must emit the complete 0076
receipt. No HTTPS/CLNrest, complete cross-chain controller, on-chain recovery,
backup restoration or production activation is established by this patch.

Next: versioned cross-chain contracts, signed customer invoice publication,
controller confirmation, reserve/timing checks, market pricing and recovery;
then both complete funded controller directions before full StartOS XBT
UI/backend/backup activation. This facade remains regtest-only.

## Local authority integration (0074)

Base: annotated `privkeyio-routes-20261011`, engine tree
`4b573074e95a7a6787dad2ebc166dece405928b4`. The operator reported the six
0072 funded route scenarios passed, pushed the 0073 checkpoint and synced the
VM. The new patch changes only Python source, tests and this documentation;
the compiled, crash-fixed BTC/XBT engines are reused.

`bounded_authority.py` introduces `swap-bounded-authority-v1`. It accepts only
explicit BTC/new-XBT regtest bindings. Fresh fixture initialization is separate
from opening existing state. Missing, legacy or cross-profile journals fail;
there is no format upgrade that confers new-fork authority on an old grant.

One private locked journal contains immutable grants, their current selection,
pause flags, and complete enrollments. Each locally confirmed grant pins its
kind, protocol, environment, node, funding/signing-type channels, expiry, slot
limit and amount budgets. Outgoing per-payment and aggregate amounts include
routing fees; incoming budgets count the full received amount. Failed, retired
and expired enrollments still consume their original reservation. Replacing a
grant requires every previous enrollment to reconcile as terminal.

Enrollment and reservation are one atomic durable replacement, so there is no
separate missing-record crash window. Exact enrollment recovery remains valid
after pause or expiry. A payment hash cannot be enrolled twice in either local
kind. The plan's complete signed invoice, route, fee/delay/hop limits and channel
pin are immutable. Admission decodes the invoice again, verifies features and
amount/secret/destination/expiry, checks current funding/signing type and
liquidity, and refuses pre-existing payment/HTLC history. It never replans.

`send()` saves its intent before the only `sendpay` call site. All subsequent
calls observe group 1, part 0, exact amounts/invoice/destination and original
created index/time. Zero rows after an intent stay unknown; the process cannot
retire that enrollment or send again. Terminal outcome regressions, replacement
HTLCs, changed funding, contradictory preimages and missing original history
block advancement. A failed journal write freezes the current instance even
when the atomic replacement may already have succeeded.

Incoming registration similarly saves its exact quote first. Registration and
gate resolution can repeat only the same idempotent gate operation. Incoming
binding is learned from the actual held HTLC, checked against approved pins and
core history, then saved once with HTLC ID, absolute expiry and history index.
The grant remains recoverable after pause/expiry. Gate `release_intent` and
`fail_intent` are never enough to declare completion: matching terminal
`listhtlcs` state and a quiet normal channel are also required. Unpaid expired
quotes and unsent expired plans can become terminal only with empty original
history; a send intent can never use that path.

A `restored.json` marker blocks all operational calls, including reconciliation
writes, while allowing redacted cached status. This is a restore barrier, not a
backup integration or restoration protocol. Closing, missing and archived
channels remain blocked for dedicated on-chain recovery; this patch does not
reuse the old normal-channel/closed-channel shortcuts or authorize a close.

The VM fixture uses the proven four-node public-prefix/private-tail topology
on each chain. Two separate local authorities own the source and recipient.
Each operation opens a fresh journal/authority instance. The matrix covers
release, failure and all-daemon held restart, lost registration/send/resolution
replies, exact duplicate enrollment, amount-budget exhaustion despite a spare
slot, and paused/expired recovery. It checks the same six endpoint balances,
original incoming and outgoing HTLCs, payment identity and immutable plan.

This module is local authority under a trusted caller, not evidence that an
incoming payment on another chain exists. The fixture chooses resolution and
uses synthetic core-invoice traps. There are no runes, HTTPS service, production
activation, installer, customer invoice publisher, price policy, cross-chain
timing/reserve checks, on-chain recovery, or automatic payment retries here.
The next integration must bind this authority to fresh scoped remote credentials
and both sides of the cross-chain contract, then port the controller's approval,
timing, reserve, pricing and recovery checks before StartOS activation/UI work.

## Route adapter audit (0072)

Base: annotated `privkeyio-plugin-crash-20261011`, engine tree
`c58fcd05f59cbd7a4092c4936185df72dcf0f244`. The operator reported the
0070a baseline, graceful-restart and plugin-SIGKILL matrices passed and completed
the 0071 tower checkpoint and VM sync. No migrated installer exists yet.

## What must change together

| Boundary | Installed code | Migration requirement |
| --- | --- | --- |
| Identity | `pilot_node.Node` identifies XBT as network `xbt`; forward invoice checks require `lnxbt` / currency `xbt`. | Bind the explicit BTC/new-XBT protocol, environment and node ID. Both new engines share `regtest` / `bcrt` and `bitcoin` / `bc`. Check node and invoice features without relabelling replies. |
| Channel authority | Old `FIELDS` pins ID, SCID, peer and funding output. | Include numeric channel-type bits. XBT admission requires persisted unified type 514. Preserve actual incoming funding/HTLC binding after admission. |
| Incoming recovery | `swap_session.terminal` expects `xbt-quote-status` phases `resolved` / `failed` and a two-item binding. | The new gate uses a structured funding/signing-type/HTLC binding and durable `release_intent` / `fail_intent`. These are decisions, not settlement receipts. Reconcile core history and the original outgoing attempt. |
| Route selection | Legacy wrappers bake currency, amount and timing rules into `routed_plan` / `reverse_plan`. | Use one protocol-bound planner with explicit limits. Public prefixes and signed private tails share one fee, delay and hop budget. No widened search changes saved authority. |
| Payment recovery | Group 1, part 0; intent persisted before `sendpay`; uncertain replies observed. | Keep that ordering. Store exact invoice/route/channel and original attempt identity. Zero rows after an uncertain send do not authorize resubmission. Unknown results stay unknown. |
| Credentials and restore | Direction-specific session/rune records and restore markers. | Use a new authority format and fresh explicit operator approval. Never reinterpret old grants/journals as new-fork authority. Restore must invalidate setup while preserving original recovery evidence. |

The old `prior()` / `terminal_channel()` paths also need careful porting:
normal channels with no HTLCs, retained on-chain channels and archived closed
channels are different evidence. Do not carry across their shortcut checks or
rename gate phases to make old state machines accept the new gate.

## What 0072 implements

`route_plan.py` is a read-only adapter. It uses only `getinfo`, `decode`,
`listpeerchannels`, `getroutes` and `listsendpays`. Its new-format plan binds
protocol, environment, node, signed invoice, exact route, fee/delay/hop limits
and the first local channel's funding/signing type. It is data, not a grant.
The adapter refuses mainnet and legacy XBT, requires a ready local channel and
does not write credentials, shared routing layers, payment records or journals.

Initial bounds remain one route, at most four hops, 10,000 msat routing fee,
288 blocks total delay, recipient final CLTV at most 40, and amount at most
1,000,000,000 msat. These are adapter ceilings, not approved spending limits.
The future grant layer must supply equal or stricter limits and approved pins.
No automatic retries, MPP execution, zero-conf/alias-only local channel pins,
splice transitions or route selection restricted inside askrene are added.
A route whose first hop is outside the supplied pins is refused.

Private tails use the signed hint's fee and CLTV arithmetic; prefixes use
`auto.localchans`, `auto.sourcefree`, and `maxparts=1`. Only routing refusals
205/206 allow another bounded read-only candidate. Code 215 means insufficient
funds and propagates; missing APIs, transport failures and unknown errors do
not become a misleading 'no route'. Confirmed private channels may advertise
a remote alias in their hints. That wire alias is preserved; it is not used
to replace the incoming gate's actual funding/SCID binding.

The attempt observer checks exact invoice, hash, group/part, destination,
delivered/sent amount, created index/time and valid completion preimage. It
reads history after invoice expiry without searching a new route. Pending,
failed, complete and not-observed remain distinct. It does not authorize
incoming release or claim incoming settlement.

## Runtime API audit and fixture

The pinned BTC and XBT sources install the C router as
`libexec/c-lightning/plugins/cln-askrene`. Its worker is forked inside the same
executable; there is no separate `askrened`. `install-program --disable-rust`
builds this plugin. The existing 0070 image contains both compiled engines;
0072 adds only Python source in a small image layer. The fixture enables the
router alongside the already-proven bcli/offers/topology/txprepare/spenderp
set. Legacy pay/xpay stay disabled, as do deprecated APIs.

Before funding, both engines must initialize the router, expose all required
methods, and actually answer `getroutes` on an empty graph with code 205.
`listsendpays`, `listhtlcs` and `listclosedchannels` are also invoked before
funding; help/manifest presence alone is insufficient. The request keys were
checked against this pinned source's JSON schemas. `newaddr bech32` and named
arguments encoded as JSON preserve the earlier fixture fixes.

The funded matrix has four fresh nodes on each chain: sender, public relay,
hint-entry relay, recipient. Two public channels precede a private final
channel. Authentic core-invoice hints provide a three-hop, 200-block route
costing 2,000 msat. A 144-block search must refuse without creating an attempt.
For each chain, the fixture tests release, failure and all-node restart while
held. It discards the send reply, reloads the saved plan and observes the
original attempt. All six endpoint balances, incoming funding/HTLC/expiry,
terminal HTLC history and the immutable route must agree. Core invoices are
deliberate fixture traps; production gate invoices remain independently signed.

Local tests do not establish this runtime result. Only the VM runner's strict
receipt verifier may print READY. The existing gate/crash evidence is retained,
not reclassified as routed coordinator evidence. 0072 is not a cross-chain
swap, bounded grant port, controller worker, HTTPS interface, on-chain recovery
test, backup restore solution, legacy-channel migration or installable UI.

The local grant/enrollment and send-intent work is now implemented by 0074
above. Funded bidirectional controller matrices remain a later integration gate.

## 0084 service fixture

`test_service_engine.py` uses the separate controller `migration/` service
through fresh action processes and a continuously running worker. Both chains
retain the exact 0083 engine/transport runtime. The fixture supplies synthetic
market snapshots and SIGKILL hooks through `fixture_service_driver.py`; these
controls are absent from the service CLI. Six funded cases require exact original
contracts, signed invoices, incoming bindings, attempts and twelve balances.
The fixture reuses one journal for three swaps per direction and checks renewed
grants preserve terminal history, plus cached history with coordinators stopped.
No installer, StartOS action registration or production activation is included.

## 0092 coordinator administration prerequisite

The operator reported the fresh 0090b nodes paid 10,000 XBT sats A to B,
1,000 B to A, and 1,000 each direction after both services restarted. Funding
and UI passwords were retained. This is operator-reported node/UI acceptance;
it does not establish live swap execution, funded StartOS restore or migration
of legacy channels.

0092 adds local, bounded grant review/confirmation in the isolated migration
engine sources. Eligible channels are selected automatically; no SCID entry.
A 120-second review records exact channel funding and limits before confirmation.
A lost confirmation recovers the same grant; it cannot renew, unpause or enlarge
it. Saved grants are never reconstructed from missing authority state. Incoming
liquidity must cover the amount cap; outgoing liquidity also covers the fee cap.
More than eight eligible channels is refused pending a separate selection UX.

Restored swap journals now observe both their own restored.json and the shared
swap-restored.json or existing node-restored.json parent markers. Gate hooks
cannot fall through or disclose a saved preimage after a restore marker appears.
This is a refusal barrier, not a funded swap recovery or credential-revocation
solution. A marker on a restored copy does not revoke a still-running original.

All migrated swap execution remains regtest-only. No installer, StartOS grant
menus, backup enablement or mainnet activation is included. The package backup
and update guards also recognize coordinator-admin-v1, so introducing the new
journal cannot bypass the existing coordinator-state refusal. Deadline/on-chain
protection and coordinator-aware backup/restore must precede live activation.

`coordinator_admin_cli.py --disposable-regtest --network-directory DIR --profile
PROFILE` takes one finite JSON action on stdin: initialize, review, confirm,
cancel, pause or status. It requires an already initialized authority journal.
Only review accepts limits and lifetime (300–86,400 seconds); confirm requires
its exact saved confirmation ID and explicit true. It never forwards arbitrary
RPC, chooses endpoints or creates credentials. Customer credential minting is
still a separate local facade operation after the authority lock is released.

`restore_barrier.fence(ROOT)` durably writes a private shared marker. A future
restore adapter must call it on stopped restored data before starting daemons.
This patch does not wire or enable coordinator backup hooks. Missing, malformed
or inconsistent payment journals are never reconstructed by admin initialization.

## 0092a populated restore-check correction

The operator's 0092 run reached the end of the six funded swap cases, then
stopped in the final restore assertion. The fixture had fenced journals before
constructing the publisher. Populated publisher validation calls guarded
Authority.entry(), so it correctly refused at construction; the earlier empty
journal test missed that dependency. No production restore check is relaxed.

The fixture now validates stopped journals before writing the restore marker,
checks all loaded objects refuse execution, then verifies a fresh customer
facade refuses during startup before any RPC. It counts both populated incoming
publishers and all four credential journals and preserves exact journal bytes.
Local regression covers both profiles with published invoices and credentials,
alongside empty journals and a corrupt-invoice control that must fail before
fencing. The corrected six-case funded run is still required on the VM.
