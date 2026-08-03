# Contributing

## Getting set up

```bash
git clone https://github.com/GenesisL1/api-registry.git
cd api-registry

# solc 0.8.24, exactly
curl -sSL -o solc https://github.com/ethereum/solidity/releases/download/v0.8.24/solc-static-linux
echo "fb03a29a517452b9f12bcf459ef37d0a543765bb3bbc911e70a87d6a37c30d5f  solc" | sha256sum -c -
chmod +x solc && sudo mv solc /usr/local/bin/

pip install -r requirements-dev.txt -r bot/requirements.txt
npm ci

./scripts/build.sh
./test/run-all.sh
```

To work on the interface with realistic data:

```bash
python test/e2e_fixture.py --hold      # http://127.0.0.1:8646/
```

That gives you a populated register with a flagged endpoint, a verified one, and
two signed telemetry feeds, backed by a real EVM running in-process. Edit files
under `web/` and reload.

## What a good change looks like

**A test that would fail without it.** Not a test that exercises the new code —
one that fails if the change is reverted. The three suites already cover the
security properties; add to whichever fits.

**Regenerated artifacts, committed.** Any contract change means re-running
`./scripts/build.sh` and committing `contract/abi/`, `contract/build/` and
`web/vendor/abi.js`. CI fails if they drift. This is what keeps the browser, the
checker and the deployed contract from disagreeing — the exact bug that made
version 1's on-chain reporting impossible.

**Docs updated when behaviour changes.** If you change how the thing is deployed
or operated, the relevant file under `docs/` changes in the same PR.

## Things to know before touching the contract

The registry is **not upgradeable**. It holds the records and the fees. A storage
layout change means a new deployment and a migration, so call it out explicitly.

The lens is stateless and replaceable. If you need a different shape of data for a
client, prefer changing the lens.

Deploy bytecode must stay under 24,576 bytes. There is about 3.5 KB of headroom.
CI checks it, but `./scripts/build.sh` prints it too, so you will know immediately.

These properties are load-bearing. If a change weakens one, it needs to be
argued for, not slipped in:

- No external call on an operator's write path
- No path by which one party changes another's reliability record
- No way for the admin to lower the quorum below `MIN_QUORUM`
- `adverseDayCount` is never reset
- The measurement epoch advances only on a real URL or kind change

## Things to know before touching the checker

It parses attacker-controlled responses from arbitrary hosts, continuously, while
holding a key. Treat it accordingly:

- Every outbound request goes through the guarded resolver and reads with a cap
- Redirects are never followed
- The key comes from the environment, never an argument
- Nothing is recorded as submitted before its receipt confirms success

## Things to know before touching the interface

No build step, no framework, no third-party requests. ethers, the fonts and the
ABI are vendored deliberately — a page that signs transactions should not load
executable code from someone else's CDN.

No inline event handlers. Everything is delegated from one listener keyed on
`data-act`, and every interpolated value passes through the escaper.

Bumping ethers is a manual step: re-vendor `web/vendor/ethers-<version>.umd.min.js`
from npm, update the tag in `index.html`, and note the version in the changelog.
Dependabot is configured to leave it alone.

## Commits and PRs

Present tense, imperative, explain the why in the body:

```
Reject redirects in the checker

Following a 302 hands an attacker a request to a target of their choosing,
which defeats the resolver guard entirely. A public API that redirects is
not usable anyway, so treat it as a failed probe.
```

Keep PRs to one concern. The PR template asks a few questions; they are the ones
a reviewer would ask anyway.

## Reporting a problem instead

- Security: [private advisory](https://github.com/GenesisL1/api-registry/security/advisories/new), not an issue
- A measurement that looks wrong: use the measurement dispute template, and record
  your account on-chain with `contestEndpoint` first
- Anything else: open an issue
