# Operations

## Public deployment

Run the checker as a dedicated unprivileged user, bound to localhost, behind
Caddy or nginx. `bot/genesisl1-api-checker.service.example` is a hardened unit;
`deploy/` has both proxy configurations.

```bash
useradd --system --home /opt/genesis-api --shell /usr/sbin/nologin genesis-api
install -d -o genesis-api -g genesis-api /opt/genesis-api
# copy the bundle to /opt/genesis-api, then:
python3 -m venv /opt/genesis-api/.venv
/opt/genesis-api/.venv/bin/pip install -r /opt/genesis-api/bot/requirements.txt

install -m 600 -o root -g root bot/checker.env.example /etc/genesis-api-checker.env
$EDITOR /etc/genesis-api-checker.env
install -m 644 bot/genesisl1-api-checker.service.example \
    /etc/systemd/system/genesisl1-api-checker.service
systemctl daemon-reload && systemctl enable --now genesisl1-api-checker
```

The default bind is `127.0.0.1`. Change it only if you have deliberately decided
to expose Python's `http.server` directly, which you should not.

## Probe load

Per endpoint, per checker, per cycle: `--burst` rounds, each a handful of
protocol calls. At the defaults — 60s interval, burst 2 — that is roughly 8,600
requests per endpoint per day per checker.

Each cycle waits `--interval` plus a random `--jitter` (0–20s by default). Leave
the jitter on. Without it, a fleet of checkers converges on the top of each
minute, finds every operator's rate limiter at once, and then reports the
throttling it caused.

Tune `--burst`, `--concurrency` and `--timeout` to your capacity. `--burst 1` is
reasonable for a large register.

Flagged endpoints are still probed, at one cycle in `--unreliable-divisor`
(default 10). This is deliberate: an endpoint that is never measured can never
accumulate the evidence that clears its own flag.

## Rate limiting

HTTP 429 and 503 are recorded as `throttled` and excluded from the uptime
denominator. They appear in the directory as a separate count. If an operator is
shedding your load, that is capacity information, not an outage, and it must not
become a reliability penalty.

If your own infrastructure is the bottleneck — a shared gateway in front of
several registered endpoints, say — raise the interval before you raise the
concurrency.

## Files to keep

| Path | Why |
|---|---|
| `bot/checker.sqlite3` | Full local measurement history. ~35 days by default. |
| `web/data/status.json` | Current public snapshot. Regenerated each cycle. |
| `web/data/status.json.sig` | Its detached signature. Serve both or neither. |

The `attested` table in SQLite is what stops a checker re-submitting a day it has
already anchored. Restoring an old backup causes duplicate attempts; the contract
skips them rather than reverting, so the cost is a little gas, not a stuck
checker.

## Running several checkers

The quorum is only as independent as the checkers are. Separate operators,
separate networks, separate administration. Each needs its own authorized address.

Publish each `status.json` and `status.json.sig` over HTTPS with
`Access-Control-Allow-Origin: *`, then list the URLs in every directory instance:

```js
telemetryUrls: [
  "data/status.json",
  "https://checker-2.example/data/status.json",
  "https://checker-3.example/data/status.json"
]
```

The browser verifies each signature against the on-chain roster and combines what
survives by median. When checkers disagree by more than two percentage points the
directory says so instead of hiding it in an average.

## Reading the logs

```
cycle complete: 41 endpoints probed, 41 in snapshot, 2 attestations pending
attested 2 endpoints in 0x…
holding 3 attestations away from the UTC day boundary
attestation batch failed, will retry next cycle: …
```

An attestation that fails is retried on the next cycle; nothing is marked
submitted until its receipt confirms success. `holding …` near midnight is normal.

## Upgrading the lens

The lens holds no state and no funds. To change the shape of the data clients
read, deploy a new one against the same registry and update `lensAddress` in
`config.js`. Nothing migrates and nothing is at risk.

The registry itself is not upgradeable. That is intentional: it holds the records
and the fees, and an upgrade path would be a larger risk than the flexibility is
worth. The lens exists so that the read API can evolve without one.

## Changing parameters

Every parameter change is a two-step, two-day operation:

```solidity
proposeConfiguration(writeFee, thresholdBps, adverseQuorum, verificationQuorum)
// two days
commitConfiguration()
```

`cancelConfiguration()` withdraws a pending change. The same pattern governs
`proposeFeeRecipient` / `commitFeeRecipient` and checker authorization. Only
`revokeChecker` is immediate.

Announce parameter changes when you propose them, not when you commit them — the
delay exists so operators can react.

## Troubleshooting

**"No verified telemetry feed."** The directory found no feed it would trust.
Check that `status.json.sig` is being served next to `status.json`, that the
checker has a key, and that its address is authorized on-chain. The notice above
the register names the specific reason for each rejected feed.

**Every endpoint shows "Awaiting measurement".** The checker is running but its
snapshot is not reaching the page, or it is more than thirty minutes old.

**An endpoint you know is healthy reads as failing.** Check the last error shown
under it. `wrong chain` means the endpoint answers for a different chain ID.
`height stalled` means it responds but has stopped importing blocks.
`redirect to an unvalidated target` means it returns a 3xx.

**`attestation would revert`.** Usually a checker whose authorization was revoked,
or a clock far enough off that its day number disagrees with the chain's.
