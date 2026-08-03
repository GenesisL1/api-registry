# Security policy

## Reporting

Report privately through
[GitHub Security Advisories](https://github.com/GenesisL1/api-registry/security/advisories/new).
Please do not open a public issue for anything exploitable.

Include the commit or deployed address you are looking at, what an attacker gains,
and a way to reproduce it. A failing test against `test/` is the most useful form
a report can take.

We aim to acknowledge within 72 hours and to ship a fix or a mitigation plan
within 14 days for anything that lets one party affect another party's records,
funds or reliability verdict.

If a live deployment is affected, say so explicitly — the registry is not
upgradeable, so mitigation may mean rotating checkers or migrating to a new
deployment, and that changes the timeline.

## Scope

**In scope**

- Anything that lets one party change another's record, verdict or funds
- Bypassing the quorum, the timelock, or the epoch binding on attestations
- SSRF, memory exhaustion or key exposure in the checker
- Rendering unverified telemetry as if it were verified
- Contract logic that diverges from what the documentation claims

**Out of scope**

- The instance-per-address cap being bypassable with a second address. This is
  documented as a speed bump, not a Sybil defence.
- A quorum of colluding authorized checkers flagging a healthy endpoint. The
  design assumes checker independence and says so.
- Denial of service against a single checker or a single RPC endpoint. The
  directory is built to survive both.
- Anything requiring the admin key, which is a trusted role bounded by timelocks
  and constants rather than by cryptography.

The [threat model](docs/THREAT-MODEL.md) states these assumptions in full. A
report that shows one of them is weaker than documented is in scope even if the
consequence is listed above.

## Supported versions

| Version | Supported |
|---|---|
| 2.x | Yes |
| 1.x ("Static Edition") | No — see [docs/REVIEW.md](docs/REVIEW.md) |

Version 1 does not compile, and its checker cannot submit an attestation. If you
have a 1.x deployment, migrate rather than patch.

## Operator hardening

If you run a checker, `docs/THREAT-MODEL.md` and
`bot/genesisl1-api-checker.service.example` cover the important parts. The two
that matter most:

- Never pass the checker key as a command-line argument. Use the environment.
- Never place a key anywhere under `web/`. Everything there is served publicly.
