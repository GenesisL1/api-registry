# Threat model

What this system assumes, what it defends, and where it does not.

For reporting a vulnerability, see [SECURITY.md](../SECURITY.md). For the review
that produced these defences, see [REVIEW.md](REVIEW.md).

## Trust boundaries

| Party | Trusted with | Cannot |
|---|---|---|
| Anyone | Registering endpoints, for a fee | Affect another operator's record |
| Instance owner | Their own records | Alter measurements, clear a flag by paying |
| Authorized checker | One attestation per endpoint epoch per UTC day | Flag anything alone; alter records |
| Administrator | The checker set and registry parameters, on a two-day delay | Take fees, lower the quorum below 2, flag an endpoint directly |
| Fee recipient | Receiving accrued fees | Block registrations |

The administrator is the most powerful party and is deliberately constrained:
authorization is delayed, the quorum floor and fee ceiling are compile-time
constants, and there is no function that writes a reliability verdict directly.
An administrator who wants to flag an endpoint must authorize two checkers, wait
two days, and have them both attest — publicly, on-chain, under their own
addresses. Point `admin` at a multisig.

## Contract

**A registered URL is untrusted input.** `_validateUrl` accepts only
`http(s)`, `ws(s)` and `grpc(s)` origins, requires printable ASCII, and rejects
embedded credentials, quotes, angle brackets, backslashes and backticks — the
characters that would let a record escape its field when rendered, or its argument
when probed. The frontend escapes independently; neither layer relies on the other.

**Fees never move on a write path.** `createInstance`, `addEndpoints`,
`updateEndpoint`, `updateInstance` and `reactivateEndpoint` take value and leave it
in the contract. `withdrawFees()` is a separate, permissionless call that always
sends to the configured recipient. A recipient that reverts on receipt cannot
block registration — verified in `test_registry.py` against a contract that
rejects every transfer.

**Reentrancy.** All payable entry points carry the guard, and none makes an
external call before completing its state changes. The only external call in the
contract is the withdrawal.

**Batch attestations degrade, they do not revert.** A stale epoch, an unknown
endpoint, a duplicate for the day, an out-of-range value: each entry is skipped.
One bad entry never discards the batch. This matters most in the ordinary case of
a checker rebuilt from backup replaying a day it already submitted.

**Epoch binding.** Every attestation names the epoch it measured. If the operator
edits the endpoint between measurement and inclusion, the attestation is dropped
rather than applied to a record it does not describe. This closes both the honest
race and a checker deliberately sitting on stale measurements.

**Paid history laundering is closed.** `updateEndpoint` charges the fee
unconditionally but only advances the epoch when `keccak256(kind ‖ url)` changes.
`adverseDayCount` is never reset by any path.

## Checker

The checker parses attacker-controlled responses from arbitrary hosts continuously.
Treat it as untrusted code that happens to hold a key, and run it under the
hardened unit in `bot/`.

**SSRF.** Validation happens at the address the socket will actually open to,
inside a custom `aiohttp` resolver — not on a hostname later handed to a different
resolver, which leaves a rebinding window. Private, loopback, link-local,
multicast, reserved, IPv4-mapped and 6to4-wrapped addresses are all refused.
Redirects are never followed: a public API that returns a 302 is not healthy, and
following it would hand an attacker a request to a target of their choosing.

**Bounded reads.** Bodies stream with a hard ceiling and abort past it, so a
registered "snapshot service" pointing at an endless stream cannot exhaust memory.
Snapshot endpoints are probed with a ranged request for the first kilobyte.

**Key handling.** Read from `CHECKER_PRIVATE_KEY` only. There is no command-line
flag, because one would appear in `ps`, in shell history and in the logged
systemd `ExecStart`. A checker without a key is fully functional as a read-only
measurement node.

**Nothing is marked done until it is mined.** A transaction hash means accepted
into a mempool. The checker waits for the receipt and checks `status == 1` before
recording an attestation as submitted, so a revert or a reorg retries instead of
silently losing a day.

**Claim thresholds.** A daily claim requires ≥1,000 counted probes spanning ≥20
hours. Rate-limited probes are excluded from the uptime denominator entirely.

## Frontend

**No third-party runtime.** ethers, both fonts and the ABI are served from the
same origin. Nothing is fetched from a CDN, so there is no third-party that can
change what a signing page executes. The ABI is generated from compiler output
during the build, so it cannot drift from the deployed contract.

**Telemetry is verified before it is displayed.** Each feed's detached signature
is recovered against the exact bytes served and matched against the on-chain
checker roster. A feed that is unsigned, signed by an unauthorized address,
describing a different registry or chain, or older than thirty minutes is
discarded with a visible reason. `test/e2e.js` includes a validly signed feed from
an unauthorized key and asserts its numbers never render.

**No inline handlers.** Everything is delegated from a single listener keyed on
`data-act`; the only interpolated values are numeric ids. All rendered strings pass
through an escaper.

**Suggested CSP**, as shipped in the proxy examples:

```
default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self';
img-src 'self' data:; connect-src *; base-uri 'none'; form-action 'none';
frame-ancestors 'none'
```

`connect-src` is open because the page talks to whatever RPC and checker feeds are
configured. Narrow it to your actual hosts if you can.

## Known limits

These are deliberate, not oversights.

- **The instance cap is not a Sybil defence.** Four instances per address costs
  one new address to bypass. It exists to stop accidental flooding. A real defence
  needs a stake, which is a governance decision rather than a code one.
- **Checkers are permissioned.** The registry is permissionless to write to but
  not to measure. Anyone can publish a feed; only authorized checkers can attest.
  Bonding and slashing would replace administrator judgement here, and neither is
  implemented.
- **Proof of control proves control of a host, not of an honest service.** An
  operator who genuinely runs `rpc.example.org` can still serve wrong data. The
  probes check chain ID and block progress, which catches the obvious cases.
- **Quorum assumes independence.** Two checkers on the same operator's
  infrastructure are one checker. Nothing in the contract can detect this; it is an
  operational responsibility.
- **A checker can lie within its measurement.** It cannot lie about which endpoint
  or epoch, cannot exceed one report per day, and cannot act alone — but a
  colluding quorum can flag a healthy endpoint. The operator's recourse is
  `contestEndpoint`, and the evidence is public.
- **`withdrawFees` is permissionless by design.** Anyone may trigger the sweep;
  the destination is always the configured recipient, so there is nothing to gain.

## Reporting

Privately, through [GitHub Security Advisories](https://github.com/GenesisL1/api-registry/security/advisories/new).
Please include the commit or deployment address you are looking at. Full policy
in [SECURITY.md](../SECURITY.md).
