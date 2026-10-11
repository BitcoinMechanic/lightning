# Coordinator port boundaries (0076)

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
