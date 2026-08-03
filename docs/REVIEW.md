# Design review

This version exists because the first one was reviewed and did not survive it.
Every finding below is recorded with what was wrong, what changed, and the test
that would fail if the fix regressed.

Kept in the repository deliberately. A registry that asks operators to trust its
measurements should be able to show its own working, and a reviewer arriving cold
should not have to rediscover which decisions were deliberate.

---

## Summary

| | Found | Fixed | Covered by a test |
|---|---|---|---|
| Shipping blockers | 3 | 3 | 3 |
| Security | 5 | 5 | 5 |
| Trust model | 8 | 8 | 8 |
| Correctness | 8 | 8 | 7 |
| Operational | 8 | 8 | 5 |

The one correctness item without a direct test is the day-boundary guard, which
depends on wall-clock timing; it is exercised indirectly through the attestation
gate. Three operational items are configuration rather than code.

---

## Blockers

### B1 — The contract did not compile

`submitAdverseReports` overflowed the stack under solc 0.8.24 legacy codegen with
the optimizer both on and off, which is stock Remix either way. Anyone following
the deployment instructions hit a compiler error on step one.

**Fixed.** The loop body moved into `_applyReport`, which resets the stack frame.
The canonical build now uses viaIR for headroom (20,991 bytes against the 24,576
limit) but the source compiles under legacy codegen too, so viaIR is a size
decision rather than a crutch.

*CI fails the build if the registry exceeds EIP-170.*

### B2 — The documented constructor did not exist

The README described three arguments; the code took one. Admin was silently the
deploying key and the fee was hardcoded.

**Fixed.** `constructor(admin, feeRecipient, endpointWriteFee)`, with the
documentation and `scripts/deploy.py` generated from the same reality.

*`test_registry.py`: "registry deploys", "fee recipient set", "write fee set".*

### B3 — The checker could never report

The bot declared `errorBps` as `uint16[]`; the contract declared `uint32[]`.
Different selector — `0x76fbd6da` against `0xf93b29ec` — so the transaction hit no
function. Latent, too: the submit path returned early when nothing was adverse, so
a fresh deployment looked healthy until the first endpoint degraded, at which
point the exception was swallowed by a broad `except` and appeared once a minute
as `cycle error:`.

**Fixed structurally, not by correcting the types.** `scripts/build.sh` generates
`contract/abi/*.json` and `web/vendor/abi.js` from compiler output. No ABI is
transcribed by hand anywhere. The two checker entry points also merged into a
single `attest(Attestation[])` taking one struct, so there is one decoder and one
selector to get wrong.

*CI fails if the committed artifacts differ from a fresh build.*

---

## Security

### S1 — The SSRF guard was bypassable, and advertised

`safe_host()` resolved the hostname and checked the address, then handed the name
to aiohttp, which resolved it again. Two holes: a short TTL returns a public
address to the check and a private one to the connection, and
`allow_redirects=True` meant a registered endpoint could simply reply
`302 → http://169.254.169.254/`. Reachable by anyone willing to pay one L1 coin,
against every checker at once.

**Fixed.** Validation happens inside a custom `aiohttp` resolver, on the address
the socket will actually open to. Redirects are never followed — a public API that
returns a 3xx is not healthy, and following it would hand an attacker a request to
a target of their choosing. IPv4-mapped and 6to4-wrapped addresses are unwrapped
before the check.

*`test_checker.py`: eight rejection cases, resolver guard, live redirect refusal.*

### S2 — Unbounded response reads

`await r.read()` buffered the whole body before truncating. Snapshot endpoints
fell through to a plain GET, so a registered "snapshot service" pointing at an
endless stream — or at a real 40 GB chain snapshot — killed the checker.

**Fixed.** Bodies stream with a hard ceiling and abort past it. Snapshot endpoints
get a ranged request for the first kilobyte.

*`test_checker.py`: "oversized body is aborted", against a server that streams 13 MB.*

### S3 — A signing page loaded ethers from a CDN with no SRI

A CDN compromise or a hostile DNS answer could rewrite the `to` and `value` of
every transaction an operator signed.

**Fixed.** ethers, both fonts and the ABI are served from the same origin. The
page makes no third-party requests at all, which was the bundle's own selling
point everywhere except this one script tag.

### S4 — Private key accepted as a command-line argument

Visible in `ps`, in shell history, and in the `ExecStart` line systemd logs.

**Fixed.** `CHECKER_PRIVATE_KEY` from the environment only. The flag is gone.

*CI fails if anything under `web/` mentions a private key.*

### S5 — Default bind was `0.0.0.0`

The documentation said to bind localhost behind a proxy; the code default put
Python's `http.server` on the public internet for anyone following the quickstart.

**Fixed.** Default `127.0.0.1`, security headers and a CSP on every response, no
directory listings, and a hardened systemd unit.

---

## Trust model

The original hero text promised "independently verify". It could not.

### D1 — Telemetry was unsigned

`status.json` named a `checker_address` but nothing bound the file to that key.
Any host serving a listed feed could publish arbitrary numbers, and feeds were
combined by unweighted mean, so one bad feed among three moved every number by a
third.

**Fixed.** Each snapshot is published with a detached signature over its exact
bytes — detached, because signing a document that must contain its own signature
invites canonicalization bugs. The directory recovers the signer and discards any
feed not attributable to a currently authorized checker, with a visible reason.
Feeds are combined by median.

*`e2e.js` serves a validly signed feed from an unauthorized key and asserts its
numbers never render. `test_checker.py` verifies the signature recovers, and that
a one-byte edit recovers a different address.*

### D2 — On-chain attestations were written and never read

`getLatestAdverse` appeared in neither the frontend nor the checker. The
quorum-verified aggregates — the most trustworthy data in the system, and the
entire justification for the attestation layer — were invisible.

**Fixed.** The directory shows, per endpoint, how many checkers attested what and
on which day, alongside the lifetime count of flagged days.

*`e2e.js`: "the on-chain attestation is displayed", "the lifetime flagged-day count is displayed".*

### D3 — The reliability verdict was fully centralized

The admin set the checker roster, the quorum and the threshold, with no delay and
no floor. `setConfiguration(fee, 10000, 1)` plus one sock puppet flagged anything.

**Fixed.** Quorum floor of 2 as a compile-time constant. Threshold floor of 50%.
Fee ceiling of 1,000 L1 coin. Two-day timelock on authorization, configuration and
fee-recipient changes. Revocation stays immediate, because the delay should
protect against adding power, never against removing it.

*`test_registry.py`: floors rejected, early commits rejected, revocation immediate.*

### D4 — Reputation laundering cost one L1 coin

`updateEndpoint` never checked whether the URL changed. Calling it with the
identical URL bumped the epoch, cleared `unreliable`, and deleted the aggregate —
and since local stats filter by epoch, the checker's history went too.

**Fixed.** The epoch advances only when `keccak256(kind ‖ url)` changes. The fee is
charged either way. `adverseDayCount` is a lifetime counter that no path resets.

*`test_registry.py`: "paid no-op does not advance the epoch", "lifetime adverse count is not erased by an edit".*

### D5 — Flagging was a one-way door with a paid exit

Once flagged, the checker stopped probing, so no evidence of recovery could
accumulate and the only exit was a paid write. Combined with D3, that is a
pay-to-be-relisted lever whether or not anyone intends to pull it.

**Fixed.** Flagged endpoints are still probed, at one cycle in ten. A quorum of
healthy attestations clears the flag on-chain, free.

*`test_registry.py`: "healthy quorum clears the flag".*

### D6 — No proof of endpoint control

Nothing stopped anyone registering `rpc.genesisl1.org` under their own name.

**Fixed.** DNS TXT at `_genesisl1.<host>`, or `/.well-known/genesisl1-registry.txt`,
containing the owning address. Checkers verify and attest; a quorum sets
`verified`. Any URL change revokes it.

*`test_registry.py` and `test_checker.py` both cover it, including that a checker cannot self-quorum.*

### D7 — The Sybil limit was cosmetic

Two instances per address costs one new address to bypass, while constraining
honest multi-region operators.

**Partly fixed, and now stated honestly.** Raised to four, so a legitimate
operator is not the only party inconvenienced. `docs/THREAT-MODEL.md` says plainly
that this is a speed bump, not a Sybil defence; a real one needs a stake, which is
a governance decision.

### D8 — Free instance creation and free unlimited text

`createInstance` with an empty endpoint array charged nothing, and
`updateInstance` was free forever on a 256-byte mutable field that renders in the
directory — an unbounded free on-chain billboard.

**Fixed.** At least one endpoint required; `updateInstance` charges the fee.

*`test_registry.py`: "zero-endpoint instance is rejected", "free metadata edits are rejected".*

---

## Correctness

### C1 — Epoch race defeated the reset semantics

Reports were attributed to the epoch at inclusion time, not measurement time. An
operator who paid to fix an endpoint could be immediately re-flagged with stale
data describing the endpoint they had just replaced.

**Fixed.** The attestation carries the epoch it measured; a mismatch is skipped.

*`test_registry.py`: "stale epoch is ignored".*

### C2 — One bad entry reverted the whole batch

`ReportNotAdverse` and `DuplicateReport` fired inside the loop. Worst in the most
likely case: a checker restored from backup retried the day, hit a duplicate on
the first entry, and landed nothing until the UTC day rolled over.

**Fixed.** Every unusable entry is skipped, not reverted.

*`test_registry.py`: "one stale entry does not discard the batch".*

### C3 — Submissions marked done on send

`send_raw_transaction` returning a hash means accepted into a mempool. A revert, a
reorg or a dropped transaction silently lost the day.

**Fixed.** The checker waits for the receipt and checks `status == 1`.

### C4 — "24h" reports filed on twenty minutes of data

The sample floor computed to 60 probes.

**Fixed.** At least 1,000 counted probes spanning at least 20 hours.

*`test_checker.py`: thin samples and short spans both refused.*

### C5 — Day-boundary skew

The checker's `now // 86400` against the chain's `block.timestamp / 1 days`
produced spurious duplicates near midnight.

**Fixed.** The checker holds attestations within ten minutes of the boundary.

### C6 — WebSocket and gRPC probes were a bare TCP connect

Anything listening passed — a hung process, a load balancer over a dead backend,
an SSH daemon on the wrong port. A broken WebSocket endpoint showed a green dot.

**Fixed.** WebSocket completes the handshake and makes a real `eth_chainId` and
`eth_blockNumber` call. gRPC exchanges the HTTP/2 preface and requires a SETTINGS
frame in reply.

*`test_checker.py`: live WebSocket round trip; a listening non-HTTP/2 port fails.*

### C7 — Path concatenation assumed a bare origin

**Fixed.** URLs are parsed and validated; the contract rejects anything that is
not a clean origin.

### C8 — View functions reverted rather than clamping

`cursor + limit` under checked arithmetic reverted near `uint64` max.

**Fixed.** Clamped, with out-of-range pages returning empty.

*`e2e.js`: "out-of-range page is empty, not a revert".*

---

## Operational

### O1 — Probe load pointed at the project's own bottleneck

Nine requests per endpoint per minute per checker, including
`validators?pagination.limit=100` — the most expensive query in the set, and the
one most likely to hit the 2 MB truncation and be scored as *down*. A synchronised
fleet would find the known RPC-side 429 ceiling and then report the throttling it
caused as an outage, with a paid write to clear it.

**Fixed.** Burst of 2, per-cycle jitter, `pagination.limit=1`, and 429/503
recorded as throttled and excluded from the uptime denominator entirely.

*`test_checker.py`: "throttled probes leave the denominator".*

### O2 — Full registry re-read every cycle

**Fixed.** The registry keeps a `revision` counter bumped on every write. The
checker reads one word and re-syncs only when it moves.

### O3 — The directory died with a single RPC

An API-availability directory going dark when one API goes down is the failure
mode that gets screenshotted.

**Fixed.** RPC failover across a list, then self-bootstrapping: once the register
holds healthy EVM endpoints, the directory prefers those over its configured seed.

### O4 — The fee recipient could brick registration

`_forwardFees` pushed value on every write path. Pointing it at a Cosmos module
account — the community pool being the obvious temptation — means the bank
module's blocked-address list rejects the transfer and every registration reverts.

**Fixed.** Fees accrue in the contract; a separate permissionless `withdrawFees()`
pulls them.

*`test_registry.py` makes a contract that reverts on receipt the fee recipient and
asserts registration still works.*

### O5 — Single-step admin transfer

**Fixed.** `transferAdmin` / `acceptAdmin`. Instance handover got the same
treatment.

### O6 — Unpinned compiler and EVM target

`pragma ^0.8.24` with a modern Remix pulls Cancun opcodes onto a chain that may
not have them.

**Fixed.** `pragma solidity 0.8.24;` exactly, `evmVersion: paris` pinned in the
build script, and `standard-input.json` shipped for verification.

### O7 — The frontend could not do what the README claimed

`addEndpoints`, `updateInstance` and `reactivateEndpoint` were missing, so an
operator could not add a fourth endpoint without Remix — or had to delete the
instance and pay again for every endpoint.

**Fixed.** All reachable, including restoring a removed endpoint, which required
the directory to fetch inactive records for owned instances since they are
otherwise invisible.

### O8 — The interface disagreed with the contract

`reliabilityThresholdBps` was read from a static value in `config.js` and never
from the chain.

**Fixed.** The threshold, the fee and the quorum all come from the contract.

*`e2e.js`: "the fee is read from the contract", "the reliability target is read from the contract".*

---

## What did not change

Three things in the original were right and were kept.

**Adverse-only anchoring.** Writing only negative claims to the chain, under
quorum, with everything else off-chain, is the correct shape. Most designs in this
space try to put the whole telemetry stream on-chain and collapse under gas costs.
The only addition is that recovery attestations are now anchored too, since
clearing a flag needs the same evidentiary standard as setting one.

**The epoch concept.** Resetting measurement when the thing being measured changes
is exactly right. It needed to be non-purchasable and race-free, not replaced.

**The no-build constraint.** For community-operated infrastructure, a directory
that anyone can inspect, host and audit without a toolchain is worth real effort.
It is now honoured completely rather than almost.

---

## Structural changes

Two things changed shape rather than being fixed.

**Two contracts instead of one.** Hardening pushed the registry past EIP-170.
State, writes and funds live in `GenesisL1APIRegistry`; read aggregation lives in
the stateless `GenesisL1RegistryLens`. This turned out better than a size
workaround: the read API can evolve by deploying a new lens and changing one line
of configuration, without touching the contract holding the records.

**One checker entry point.** `submitReports` and `submitVerifications` merged into
`attest(Attestation[])`. One decoder, one selector, and a checker makes at most one
transaction per day.

---

## Where this is still weak

Stated plainly, because a review that ends on a clean bill is not a review.

- The instance cap is not a Sybil defence.
- Checkers are permissioned. Anyone can publish a feed; only authorized checkers
  can attest. Bonding and slashing would replace administrator judgement, and
  neither is implemented.
- Quorum assumes independence, and nothing in the contract can verify it.
- A colluding quorum can flag a healthy endpoint. The operator's recourse is
  `contestEndpoint` and a public record.
- Proof of control proves control of a host, not honesty of the service.

Full detail in [THREAT-MODEL.md](THREAT-MODEL.md).
