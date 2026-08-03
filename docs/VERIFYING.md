# Verifying the numbers yourself

The directory checks every feed before it displays anything. You do not have to
take its word for that. Everything below runs against a live deployment with no
special access.

## 1. Confirm the contracts are what they claim

`contract/build/standard-input.json` is the exact standard-JSON input the build
used. Paste it into Blockscout's standard-input verification for both addresses.
Compiler: solc 0.8.24, optimizer on, 200 runs, viaIR, evmVersion paris.

Reproduce the bytecode locally:

```bash
./scripts/build.sh
# compare contract/build/GenesisL1APIRegistry.bin with the deployed code
```

## 2. Check who can flag an endpoint

```solidity
getCheckerAddresses()          // every checker ever proposed
getChecker(address)            // .authorized is the live state
adverseReportQuorum()          // how many must agree
MIN_QUORUM()                   // the floor no admin can go below
```

If `authorizedCheckerCount()` is below the quorum, nothing can be flagged at all.

## 3. Verify a telemetry feed by hand

Each feed is published with a detached signature over the exact bytes of the JSON.

```bash
curl -sO https://checker.example/data/status.json
curl -sO https://checker.example/data/status.json.sig
```

```javascript
// node, with the bundled ethers
const fs = require("fs");
const { ethers } = require("./web/vendor/ethers-6.13.4.umd.min.js");

const raw = fs.readFileSync("status.json", "utf8");
const sig = JSON.parse(fs.readFileSync("status.json.sig", "utf8"));
console.log(ethers.verifyMessage(raw, sig.signature));   // the signing checker
```

Then confirm that address is authorized on-chain. If it is not, the feed means
nothing, however plausible its numbers look.

Change one byte of `status.json` and the recovered address changes. There is no
way to edit a feed without invalidating its signature.

## 4. Read the on-chain record directly

```solidity
getEndpoint(id)
//  .unreliable       flagged right now
//  .adverseDayCount  lifetime flagged days, never reset by an edit
//  .epoch            advances only on a real URL or type change
//  .verified         a quorum confirmed the operator controls the host

getLatestAggregate(id)
//  .adverseCount / .healthyCount   how many checkers said what, today
//  .uptimeBpsAverage               their average
```

An endpoint with `adverseDayCount` of zero has never been flagged. One with a high
count and `unreliable == false` has a history and has recovered — the directory
shows both, because only showing the current state would let a record be reset by
paying for an edit.

## 5. Confirm control of a host

```bash
dig +short TXT _genesisl1.rpc.example.org
curl -s https://rpc.example.org/.well-known/genesisl1-registry.txt
```

Either should contain the owning address from `getInstance(instanceId).owner`.

## 6. Watch it happen

Every meaningful action emits an event:

```
EndpointAdded  EndpointUpdated  EndpointDeleted
EndpointReliabilityChanged      EndpointVerified   EndpointContested
ReportSubmitted                 CheckerAuthorized  CheckerRevoked
ConfigurationProposed           ConfigurationUpdated
```

`ConfigurationProposed` fires two days before `ConfigurationUpdated` can. If a
parameter changed without a proposal two days earlier, something is wrong with
your copy of the contract, not with the chain.
