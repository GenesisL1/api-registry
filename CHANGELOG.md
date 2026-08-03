# Changelog

Notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-08-02

A rewrite following a full design review. Version 1 does not compile and its
checker cannot submit an attestation; see [docs/REVIEW.md](docs/REVIEW.md) for
every finding, its fix, and the test that covers it.

**Not compatible with 1.x.** Different constructor, different ABI, different
storage. Redeploy rather than migrate.

### Added

- `GenesisL1RegistryLens`, a stateless read aggregator. Deploy a new one to change
  the shape of the data clients read without touching state or funds.
- Signed telemetry. Snapshots ship with a detached signature over their exact
  bytes; the directory discards feeds it cannot attribute to an authorized checker.
- Proof of endpoint control, via DNS TXT or `/.well-known/`, quorum-attested and
  revoked by any URL change.
- Recovery attestations. A quorum of healthy measurements clears a flag on-chain,
  free — previously the only exit was a paid write.
- `contestEndpoint`, an operator's free public right of reply to a flag.
- Two-day timelock on checker authorization, configuration, and fee-recipient
  changes. Revocation stays immediate.
- Lifetime `adverseDayCount` that no path resets.
- 24-hour hourly traces in the telemetry schema, rendered per endpoint.
- Two-step handover for both the admin role and instance ownership.
- 177 tests across three suites, plus CI that fails on ABI drift, EIP-170
  overflow, or a key committed under `web/`.

### Changed

- Fees accrue in the contract and are pulled by a separate permissionless call,
  instead of being pushed on every write. A fee recipient that cannot receive
  value can no longer block registrations.
- `submitReports` and `submitVerifications` merged into `attest(Attestation[])`.
- The measurement epoch advances only when the URL or API type actually changed.
  The fee is charged either way.
- WebSocket and gRPC probes now complete a real handshake instead of a TCP connect.
- Rate-limited probes are excluded from the uptime denominator.
- Flagged endpoints are still probed, at a reduced rate, so they can accumulate
  the evidence that clears them.
- The reliability target, fee and quorum are read from the contract, not config.
- RPC failover, then self-bootstrapping from healthy registered endpoints.
- Per-address instance cap raised from 2 to 4; documented as a speed bump rather
  than a Sybil defence.
- The interface was rebuilt: dark mode, an hourly availability trace per endpoint,
  visible per-checker consensus, and all owner actions reachable.
- Telemetry schema `v2` → `v3`.

### Fixed

- The contract did not compile under stock Remix settings.
- The checker's ABI did not match the contract, so no attestation could ever land.
- The documented constructor did not exist.
- SSRF: hostname validation could be defeated by DNS rebinding, and redirects were
  followed to arbitrary targets.
- Unbounded response reads could exhaust checker memory.
- ethers was loaded from a CDN with no integrity check, on a page that signs
  transactions.
- Paying for a no-op endpoint edit erased a reliability record from both the chain
  and local history.
- Attestations were attributed to the epoch at inclusion time, not measurement.
- One unusable entry reverted an entire attestation batch.
- Attestations were marked submitted on send rather than on a successful receipt.
- A "24 hour" claim could be filed on twenty minutes of data.
- The private key could be passed as a command-line argument.
- The default bind was `0.0.0.0`.
- `addEndpoints`, `updateInstance` and `reactivateEndpoint` were unreachable from
  the interface.
- On-chain attestations were written and never displayed.

### Security

Full model in [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md). The properties now
enforced by constants rather than by policy: quorum floor of 2, reliability target
floor of 50%, write fee ceiling of 1,000 L1 coin.

## [1.0.0] — 2026-08-02

Initial "Static Edition". Superseded; do not deploy.
