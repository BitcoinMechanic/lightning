# Coordinator port boundaries (0072)

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

Next integration: versioned local grant/enrollment records and immutable send
intents using these identities/plans, new gate binding reconciliation, then
funded bidirectional controller matrices before StartOS activation/UI work.
