<div align="center">

# GenesisL1 Public API Registry

**A register of community-operated GenesisL1 API endpoints, with a reliability record no single party can write.**

[![CI](https://github.com/GenesisL1/api-registry/actions/workflows/ci.yml/badge.svg)](https://github.com/GenesisL1/api-registry/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Solidity 0.8.24](https://img.shields.io/badge/solidity-0.8.24-363636.svg)](contract/GenesisL1APIRegistry.sol)
[![Tests](https://img.shields.io/badge/tests-177%20passing-1b7a5a.svg)](#tests)

[Deploy](docs/DEPLOYMENT.md) · [Operate](docs/OPERATIONS.md) · [Verify it yourself](docs/VERIFYING.md) · [Threat model](docs/THREAT-MODEL.md) · [Design review](docs/REVIEW.md)

</div>

---

Public RPC directories usually ask you to trust a list. This one asks you to check
it. Endpoints are registered on-chain by their operators; independent checkers
measure them and anchor compact daily attestations; the directory verifies every
measurement feed's signature before it renders a single number.

```
      operator                    checker (× N)                  you
         │                             │                          │
    registers endpoint            probes it every minute     open the directory
         │                             │                          │
         ▼                             ▼                          ▼
  ┌─────────────────┐          ┌──────────────┐          ┌─────────────────┐
  │    registry     │◀─────────│  attests to  │          │ reads records   │
  │  (on GenesisL1) │  adverse │  adverse days│          │ from the chain  │
  │                 │  days &  │  & recoveries│          │                 │
  │  records, fees, │ recovery │              │  signed  │ verifies every  │
  │  quorum verdict │          │  publishes ──┼─────────▶│ feed signature  │
  └─────────────────┘          │  status.json │ telemetry│ against the     │
                               └──────────────┘          │ on-chain roster │
                                      │                  └─────────────────┘
                               local SQLite:
                               minute-level history,
                               never on-chain
```

## What it commits to

**Only adverse days reach the chain.** Minute-level telemetry stays in each
checker's SQLite. A healthy endpoint in good standing never causes a storage
write. The chain holds registry records and compact daily attestations, written
only when an endpoint misses its target or recovers from a flag.

**No single party decides reliability.** Flagging needs a quorum of independently
authorized checkers. So does clearing a flag. The floor is a compile-time constant
of 2 that no administrator can lower.

**A reliability record cannot be bought off.** Editing an endpoint costs the fee
whether or not anything changed, and the measurement epoch advances only when the
URL or API type actually changed. The lifetime count of flagged days survives
every edit, epoch and recovery.

**The administrator is on a timelock.** Authorizing a checker, and changing the
fee, target, quorum or fee recipient, all wait two days. Revoking a misbehaving
checker is immediate — the delay guards against adding power, never removing it.

**Fees never touch an operator's write path.** They accrue in the contract and are
pulled out by a separate permissionless call. A fee recipient that cannot accept a
transfer can strand the funds but can never stop a registration.

**Telemetry is signed and checked.** Each snapshot ships with a detached signature
over its exact bytes. The directory discards any feed that does not recover to a
currently authorized checker, and says why. Feeds combine by median, and
disagreement between checkers is shown rather than averaged away.

**Operators have a right of reply.** A flagged operator can record a public
statement on-chain against the endpoint, for the cost of gas.

## Repository

| Path | What it is |
|---|---|
| [`contract/`](contract/) | `GenesisL1APIRegistry.sol` holds records, fees and verdicts. `GenesisL1RegistryLens.sol` is a stateless read aggregator that can be replaced without touching state. Solidity 0.8.24. |
| [`web/`](web/) | The directory. Plain HTML, CSS and JavaScript — no build step, no framework, and no third-party requests at runtime. ethers, fonts and the ABI are all vendored. |
| [`bot/`](bot/) | One Python process: probes endpoints, keeps local history, publishes a signed snapshot, anchors attestations. |
| [`test/`](test/) | 177 checks across three suites. All run without a node, a network or a funded key. |
| [`docs/`](docs/) | Deployment, operations, verification, threat model, and the review this version came out of. |

## Quick start

```bash
git clone https://github.com/GenesisL1/api-registry.git
cd api-registry

# See it working before you deploy anything: four instances, ten endpoints,
# a quorum-flagged endpoint, and two signed telemetry feeds — no chain, no keys.
pip install -r requirements-dev.txt -r bot/requirements.txt
python test/e2e_fixture.py --hold
# open http://127.0.0.1:8646/
```

Then read **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. The short version:

```bash
./scripts/build.sh                       # needs solc 0.8.24 exactly
./test/run-all.sh                        # 177 checks

export DEPLOYER_PRIVATE_KEY=0x...
python scripts/deploy.py \
    --rpc https://rpc.genesisl1.org \
    --admin 0xADMIN_MULTISIG \
    --fee-recipient 0xTREASURY_MULTISIG
```

Paste the printed addresses into `web/config.js`, publish `web/` anywhere static,
and run at least two checkers under separate administration. Nothing can be
flagged until two are authorized — that is the intended default.

> [!IMPORTANT]
> Point `--fee-recipient` at a multisig, not a Cosmos module account. Fees accrue
> in the contract and are pulled out later, so a recipient that cannot receive
> value can never block a registration — but it can strand the fees. The deploy
> script dry-runs a transfer before it spends anything.

## What the checker measures

Real protocol calls, not a TCP connect.

| Kind | Probe |
|---|---|
| EVM RPC | `eth_chainId` (must match), `eth_blockNumber`, `net_version` |
| EVM WebSocket | Full handshake, then a real call over the socket |
| Cosmos LCD | `node_info`, latest block, one paginated store read |
| CometBFT RPC | `/status` (fails if catching up), `/abci_info` |
| gRPC, gRPC-Web | HTTP/2 preface exchange; a SETTINGS frame must come back |
| Snapshot | Ranged `GET` for the first kilobyte, never the file |
| Indexer, other | Single bounded `GET` |

A node whose height has not advanced within `--stall-seconds` is recorded as
failing however fast it answers — silently serving stale state is worse than being
down. Rate limiting is recorded separately and excluded from the uptime
denominator: a checker fleet must not be able to manufacture the outage it then
reports.

Before attesting to a day, a checker requires at least 1,000 counted probes
spanning at least 20 hours, and stays quiet within ten minutes of the UTC boundary
so a transaction cannot land on the wrong day.

## Proving you control an endpoint

Otherwise anyone can list your infrastructure under their own name. Either proof
works; checkers verify it and a quorum records it on-chain.

```
DNS   _genesisl1.rpc.example.org.  IN  TXT  "gl1-owner=0xYOUR_OWNER_ADDRESS"
HTTP  GET https://rpc.example.org/.well-known/genesisl1-registry.txt
      gl1-owner=0xYOUR_OWNER_ADDRESS
```

Verification is per measurement epoch, so changing a URL revokes it until the new
host proves control in turn.

## Tests

```bash
pip install -r requirements-dev.txt -r bot/requirements.txt
npm ci
./test/run-all.sh
```

| Suite | Checks | What it proves |
|---|---|---|
| `test/test_registry.py` | 91 | Contracts on a real EVM: fees, quorum, epochs, timelocks, a fee recipient that reverts on receipt, URL validation, handovers, the lens. |
| `test/test_checker.py` | 50 | Probes against live adversarial servers: redirect refusal, oversized bodies, rebinding, stalled heights, signing, attestation gating. |
| `test/e2e.js` | 36 | The real page in a headless DOM against real deployed contracts — including a validly signed feed from an unauthorized key, which must never render. |

None of them needs a node, a network or a funded key. CI additionally fails if the
committed ABI drifts from a fresh compile, if the registry exceeds EIP-170, or if
anything under `web/` mentions a private key.

## Registry limits

| | |
|---|---|
| Instances per address | 4 |
| Endpoints per instance | 24 |
| URL | 256 bytes, printable ASCII, no credentials |
| Reliability target | 99.00% default; floor 50%, cannot exceed 100% |
| Quorum | 2 minimum, by constant |
| Write fee | 1 L1 coin default; hard ceiling 1,000 |
| Timelock | 2 days |

The instance cap is a speed bump, not a Sybil defence — a second address defeats
it. [THREAT-MODEL.md](docs/THREAT-MODEL.md) is explicit about that and the other
places this design is deliberately weak.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: a change to the contract needs a
test that would fail without it, and `./scripts/build.sh` re-run with the
generated files committed.

Security issues go through
[private advisories](https://github.com/GenesisL1/api-registry/security/advisories/new),
not public issues. See [SECURITY.md](SECURITY.md).

## License

MIT. Bundled third-party components keep their own licences — ethers.js under MIT,
Archivo and IBM Plex Mono under the SIL Open Font License. See [LICENSE](LICENSE).
