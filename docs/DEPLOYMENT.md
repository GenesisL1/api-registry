# Deployment

Start to finish: two contracts, a static site, and at least two checkers. Budget
about an hour of work plus a two-day timelock before anything can be flagged.

Nothing here needs a mainnet transaction until step 3. Steps 1 and 2 run entirely
on your machine.

---

## Before you start

Decide these first. Two of them are awkward to change later.

| Decision | Guidance |
|---|---|
| **Admin address** | A multisig. It controls the checker set. An EOA here means one key can eventually flag any endpoint. |
| **Fee recipient** | A multisig, or any address that can receive a plain EVM value transfer. **Not** a Cosmos module account — see below. |
| **Write fee** | 1 L1 coin is the default. High enough to deter noise, low enough that a legitimate operator does not think twice. |
| **Checkers** | At least two, ideally three, under genuinely separate administration. |

**About the fee recipient.** Fees accrue inside the registry and are pulled out by
a separate call, so a recipient that cannot receive value can never block a
registration — but it can strand the fees until you change it, which takes two
days. The trap worth naming: routing fees straight into the Cosmos community pool
does not work. The bank module's blocked-address list rejects the transfer, so
`withdrawFees()` reverts every time. Use a multisig and fund the community pool
from it with an explicit `MsgFundCommunityPool` when you choose to.
`scripts/deploy.py` runs a dry-run transfer against your recipient before it
spends anything.

---

## 1. Build

```bash
git clone https://github.com/GenesisL1/api-registry.git
cd api-registry

solc --version          # must be exactly 0.8.24
./scripts/build.sh
```

```
  GenesisL1APIRegistry        20991 bytes runtime  (3585 under EIP-170)
  GenesisL1RegistryLens        9000 bytes runtime  (15576 under EIP-170)
  web/vendor/abi.js           30907 bytes
```

If you do not have solc:

```bash
curl -sSL -o solc https://github.com/ethereum/solidity/releases/download/v0.8.24/solc-static-linux
echo "fb03a29a517452b9f12bcf459ef37d0a543765bb3bbc911e70a87d6a37c30d5f  solc" | sha256sum -c -
chmod +x solc && sudo mv solc /usr/local/bin/
```

The build settings are pinned by the script and are what you will need again for
source verification:

| | |
|---|---|
| Compiler | solc 0.8.24 |
| Optimizer | enabled, 200 runs |
| Codegen | viaIR |
| EVM version | paris |

`contract/build/standard-input.json` is the exact standard-JSON input, for
one-click verification later.

`web/vendor/abi.js` is generated here from the compiler output. It is what the
browser loads, which is why the interface cannot drift from the deployed
contract. Commit it alongside any contract change.

---

## 2. Run the tests

Do this before you deploy, not after.

```bash
pip install -r requirements-dev.txt -r bot/requirements.txt
npm ci
./test/run-all.sh
```

```
=== contracts ===     91 passed, 0 failed
=== checker ===       50 passed, 0 failed
=== end to end ===    36 passed, 0 failed
```

The contract suite deploys to an in-process EVM — no node, no network, no funded
key. The end-to-end suite renders the real page against real contracts in a
headless DOM.

To see a fully populated register before you commit to anything:

```bash
python test/e2e_fixture.py --hold
# open http://127.0.0.1:8646/
```

That gives you four instances, ten endpoints, a quorum-flagged endpoint, a
verified one, and two signed telemetry feeds. Nothing touches a real chain.

---

## 3. Deploy the contracts

```bash
export DEPLOYER_PRIVATE_KEY=0x...

python scripts/deploy.py \
    --rpc https://rpc.genesisl1.org \
    --admin 0xADMIN_MULTISIG \
    --fee-recipient 0xTREASURY_MULTISIG \
    --fee 1
```

The script prints what it is about to do, tests that the fee recipient can receive
value, asks for confirmation, deploys both contracts, and prints the config lines
to paste.

<details>
<summary>Deploying from Remix instead</summary>

Under **Advanced Configurations**, set the compiler to **0.8.24**, enable the
optimizer at **200** runs, and turn on **viaIR**. Without viaIR the registry
compiles but lands at roughly 24.1 KB, which is inside the limit with almost no
margin — use viaIR.

1. Deploy `GenesisL1APIRegistry` with three arguments:
   - `admin_` — your admin multisig
   - `feeRecipient_` — your treasury multisig
   - `endpointWriteFee_` — `1000000000000000000` (1 L1 coin in wei)
2. Deploy `GenesisL1RegistryLens` with one argument: the registry address.

</details>

**Record both addresses.** The registry is not upgradeable; it holds the records
and the fees. The lens holds neither and can be replaced at any time.

### Verify the sources

On Blockscout, choose **Solidity (Standard JSON input)** and upload
`contract/build/standard-input.json` for each address. Do this immediately —
an unverified registry asks operators to trust bytecode they cannot read.

---

## 4. Point the directory at it

Edit `web/config.js`:

```js
registryAddress: "0xYOUR_REGISTRY",
lensAddress: "0xYOUR_LENS",
rpcUrls: ["https://rpc.genesisl1.org"],
telemetryUrls: ["data/status.json"]
```

Then publish `web/` anywhere static. It is a plain directory of files with no
build step and no runtime requests to any third party.

- **GitHub Pages** — enable Pages on the repository and the included
  `.github/workflows/pages.yml` publishes `web/` on every push to `main`. Set
  `REGISTRY_ADDRESS` and `LENS_ADDRESS` as repository *variables* to keep
  addresses out of the committed file.
- **Any object store or CDN** — copy `web/` and serve it.
- **The checker itself** — `--serve`, behind a reverse proxy. See step 5.

The `rpcUrls` entry only bootstraps the first load. Once the register has healthy
EVM endpoints, the directory prefers those, so it stops depending on any single
RPC — including yours.

---

## 5. Run a checker

```bash
sudo useradd --system --home /opt/genesis-api --shell /usr/sbin/nologin genesis-api
sudo install -d -o genesis-api -g genesis-api /opt/genesis-api
sudo cp -r . /opt/genesis-api/
sudo python3 -m venv /opt/genesis-api/.venv
sudo /opt/genesis-api/.venv/bin/pip install -r /opt/genesis-api/bot/requirements.txt
sudo chown -R genesis-api:genesis-api /opt/genesis-api
```

Configure it:

```bash
sudo install -m 600 -o root -g root bot/checker.env.example /etc/genesis-api-checker.env
sudo $EDITOR /etc/genesis-api-checker.env
```

```ini
REGISTRY_ADDRESS=0xYOUR_REGISTRY
LENS_ADDRESS=0xYOUR_LENS
RPC_URL=https://rpc.genesisl1.org
CHAIN_ID=29
COSMOS_CHAIN_ID=genesis_29-2
CHECKER_NAME=your-operator-name
# CHECKER_PRIVATE_KEY=0x...   # only once this checker is authorized
```

Install the service:

```bash
sudo install -m 644 bot/genesisl1-api-checker.service.example \
    /etc/systemd/system/genesisl1-api-checker.service
sudo systemctl daemon-reload
sudo systemctl enable --now genesisl1-api-checker
journalctl -u genesisl1-api-checker -f
```

You should see, within a minute:

```
read-only mode: no CHECKER_PRIVATE_KEY, nothing will be submitted on-chain
serving /opt/genesis-api/web on http://127.0.0.1:8787/
registry revision 3: 12 active endpoints
cycle complete: 12 endpoints probed, 12 in snapshot, 0 attestations pending
```

Put TLS in front of it. `deploy/Caddyfile.example` and
`deploy/nginx.conf.example` are ready to use and set the CORS header that other
directory instances need in order to read and verify this feed.

**The key is never a command-line argument.** It would appear in `ps`, in shell
history, and in the `ExecStart` line systemd logs. A checker with no key is fully
functional as a read-only measurement node — it just publishes nothing signed and
submits nothing.

---

## 6. Authorize the checkers

Until two are authorized, nothing can be flagged. That is the intended default:
a register with one checker has no quorum and therefore no verdict worth trusting.

From the admin multisig:

```solidity
proposeChecker(0xCHECKER_A, "operator-a", "https://a.example/data/status.json")
proposeChecker(0xCHECKER_B, "operator-b", "https://b.example/data/status.json")
```

Wait two days, then:

```solidity
commitChecker(0xCHECKER_A)
commitChecker(0xCHECKER_B)
```

Now give each checker its key and restart it:

```bash
sudo $EDITOR /etc/genesis-api-checker.env      # uncomment CHECKER_PRIVATE_KEY
sudo systemctl restart genesisl1-api-checker
```

Each needs a small balance for gas. Attestations are rare — only adverse days,
recoveries, and one-off proofs of control — so a few L1 coin lasts a long time.

Finally, list every checker's feed in `web/config.js` so the directory can
cross-check them:

```js
telemetryUrls: [
  "data/status.json",
  "https://b.example/data/status.json",
  "https://c.example/data/status.json"
]
```

The browser verifies each signature against the on-chain roster, discards anything
it cannot attribute to an authorized checker, and combines what survives by
median.

---

## 7. Register the first endpoints

Open the directory, connect a wallet, and use **Register infrastructure**. Or from
the contract:

```solidity
createInstance(
  "Northern Europe node",
  "Tallinn, EE",
  "https://operator.example",
  [0, 3],                                                  // EVM RPC, CometBFT RPC
  ["https://rpc.example.org", "https://rpc.example.org:26657"]
)  // send 2 L1 coin
```

### Prove you control the host

Otherwise anyone can list your endpoints under their own name. Either proof works,
and checkers attest to it on-chain once a quorum agrees.

DNS, which covers every scheme:

```
_genesisl1.rpc.example.org.  IN  TXT  "gl1-owner=0xYOUR_OWNER_ADDRESS"
```

HTTP, for `http(s)` endpoints:

```
GET https://rpc.example.org/.well-known/genesisl1-registry.txt
gl1-owner=0xYOUR_OWNER_ADDRESS
```

Verification is per measurement epoch, so changing an endpoint's URL revokes it
until the new host proves control in turn.

---

## Post-deployment checklist

- [ ] Both contracts verified on the explorer
- [ ] `admin()` returns your multisig, not the deploying key
- [ ] `feeRecipient()` returns an address that can receive value
- [ ] At least two checkers authorized, on separate infrastructure
- [ ] Each checker's `status.json` **and** `status.json.sig` reachable over HTTPS with `Access-Control-Allow-Origin: *`
- [ ] The directory shows the expected number of verified feeds, and no rejection notice
- [ ] A test registration succeeded, and the fee landed in the registry
- [ ] `withdrawFees()` tested once, for a small amount, to confirm the recipient receives
- [ ] Proof of control published for the project's own endpoints

---

## Changing things later

Every parameter change is two steps, two days apart. Announce at the proposal,
not at the commit — the delay exists so operators can react.

```solidity
proposeConfiguration(writeFee, thresholdBps, adverseQuorum, verificationQuorum)
// two days
commitConfiguration()
```

The same pattern governs `proposeFeeRecipient` / `commitFeeRecipient` and checker
authorization. Only `revokeChecker` is immediate, because removing a misbehaving
checker must never wait.

Hard limits no administrator can cross: quorum below 2, reliability target below
50%, write fee above 1,000 L1 coin.

**Replacing the lens** is safe and cheap: deploy a new one against the same
registry, update `lensAddress`, done. Nothing migrates.

**Replacing the registry** is not possible. It holds the records and the fees, and
an upgrade path would be a larger risk than the flexibility is worth. The lens
exists so the read API can evolve without one.

---

## If something is wrong

| Symptom | Cause |
|---|---|
| "No verified telemetry feed" | The `.sig` file is not being served, the checker has no key, or its address is not authorized. The notice names the reason per feed. |
| Everything shows "Awaiting measurement" | The snapshot is not reaching the page, or is older than thirty minutes. |
| An endpoint reads as failing | Check the last error under it: `wrong chain`, `height stalled`, or `redirect to an unvalidated target` are each precise. |
| `attestation would revert` in the checker log | Authorization revoked, or the checker's clock disagrees with the chain about the UTC day. |
| `withdrawFees()` reverts | The fee recipient cannot receive value. Propose a new one; registrations are unaffected meanwhile. |
| Remix: "Stack too deep" | viaIR is off. Turn it on under Advanced Configurations. |

More in [OPERATIONS.md](OPERATIONS.md). To check any of the above yourself
without trusting this software, see [VERIFYING.md](VERIFYING.md).
