#!/usr/bin/env python3
"""Contract test suite for the GenesisL1 Public API Registry.

Runs against an in-process EVM. No node, no network, no funded keys.

    pip install "web3[tester]" py-solc-x
    python test/test_registry.py

Every hardening claim in docs/SECURITY.md has a test here. If you change the
contract, run this before you redeploy.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from eth_utils import to_wei
from web3 import EthereumTesterProvider, Web3
from web3.exceptions import ContractLogicError

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contract"
SOLC = Path(__file__).resolve().parents[1] / ".solc" / "solc"
DAY = 86_400
FEE = to_wei(1, "ether")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


# --------------------------------------------------------------------- harness

def solc_binary() -> str:
    for candidate in (SOLC, Path("solc")):
        try:
            out = subprocess.run([str(candidate), "--version"], capture_output=True, text=True)
            if out.returncode == 0 and "0.8.24" in out.stdout:
                return str(candidate)
        except FileNotFoundError:
            continue
    sys.exit("solc 0.8.24 not found. Run scripts/build.sh first.")


def compile_all() -> dict:
    solc = solc_binary()
    standard = {
        "language": "Solidity",
        "sources": {
            name: {"content": (CONTRACTS / name).read_text()}
            for name in ("GenesisL1APIRegistry.sol", "GenesisL1RegistryLens.sol")
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
            "evmVersion": "paris",
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object", "evm.deployedBytecode.object"]}},
        },
    }
    proc = subprocess.run([solc, "--standard-json"], input=json.dumps(standard), capture_output=True, text=True)
    result = json.loads(proc.stdout)
    for error in result.get("errors", []):
        if error["severity"] == "error":
            sys.exit(error["formattedMessage"])
    return result["contracts"]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  \033[32mpass\033[0m  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def reverts(name: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except (ContractLogicError, Exception) as exc:  # eth-tester wraps reverts
        if "revert" in str(exc).lower() or isinstance(exc, ContractLogicError):
            check(name, True)
            return
        check(name, False, f"unexpected error: {exc!r}")
        return
    check(name, False, "call succeeded but should have reverted")


class Chain:
    def __init__(self) -> None:
        self.w3 = Web3(EthereumTesterProvider())
        self.tester = self.w3.provider.ethereum_tester
        self.accounts = self.w3.eth.accounts

    def deploy(self, artifact: dict, *args, value: int = 0, sender: str | None = None):
        contract = self.w3.eth.contract(abi=artifact["abi"], bytecode=artifact["evm"]["bytecode"]["object"])
        tx = contract.constructor(*args).transact({"from": sender or self.accounts[0], "value": value})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        return self.w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])

    def send(self, fn, sender: str, value: int = 0):
        tx = fn.transact({"from": sender, "value": value})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        if receipt.status != 1:
            raise AssertionError("transaction reverted")
        return receipt

    def travel(self, seconds: int) -> None:
        self.tester.time_travel(self.w3.eth.get_block("latest").timestamp + seconds)
        self.tester.mine_block()


# ----------------------------------------------------------------------- tests

def main() -> int:
    print("compiling…")
    artifacts = compile_all()
    registry_art = artifacts["GenesisL1APIRegistry.sol"]["GenesisL1APIRegistry"]
    lens_art = artifacts["GenesisL1RegistryLens.sol"]["GenesisL1RegistryLens"]
    runtime = len(registry_art["evm"]["deployedBytecode"]["object"]) // 2
    print(f"registry runtime: {runtime} bytes ({24576 - runtime} under EIP-170)\n")

    chain = Chain()
    admin, operator, other, checker_a, checker_b, checker_c, treasury = chain.accounts[:7]

    print("deployment")
    registry = chain.deploy(registry_art, admin, treasury, FEE)
    lens = chain.deploy(lens_art, registry.address)
    check("registry deploys", registry.functions.admin().call() == admin)
    check("fee recipient set", registry.functions.feeRecipient().call() == treasury)
    check("write fee set", registry.functions.endpointWriteFee().call() == FEE)
    check("quorum defaults to the floor", registry.functions.adverseReportQuorum().call() == 2)
    check("lens bound to registry", lens.functions.registry().call() == registry.address)
    check("runtime under EIP-170", runtime < 24576, f"{runtime} bytes")

    print("\nregistration and fees")
    treasury_start = chain.w3.eth.get_balance(treasury)
    reverts(
        "zero-endpoint instance is rejected",
        chain.send,
        registry.functions.createInstance("Free ride", "EU", "", [], []),
        operator,
    )
    reverts(
        "wrong fee is rejected",
        chain.send,
        registry.functions.createInstance("Underpaid", "EU", "", [0], ["https://rpc.example.org"]),
        operator,
        FEE // 2,
    )
    chain.send(
        registry.functions.createInstance(
            "Northern Europe node",
            "Tallinn, EE",
            "https://operator.example",
            [0, 3],
            ["https://rpc.example.org", "https://rpc.example.org:26657"],
        ),
        operator,
        FEE * 2,
    )
    check("instance created", registry.functions.activeInstanceCount().call() == 1)
    check("fees accrue in the contract", chain.w3.eth.get_balance(registry.address) == FEE * 2)
    check("no value is pushed to the recipient on a write", chain.w3.eth.get_balance(treasury) == treasury_start)

    instance = registry.functions.getInstance(1).call()
    check("owner recorded", instance[1] == operator)
    check("endpoint count recorded", instance[4] == 2)

    reverts(
        "free metadata edits are rejected",
        chain.send,
        registry.functions.updateInstance(1, "Renamed", "EE", ""),
        operator,
    )
    chain.send(registry.functions.updateInstance(1, "Renamed node", "Tallinn, EE", ""), operator, FEE)
    check("paid metadata edit applies", registry.functions.getInstance(1).call()[6] == "Renamed node")

    reverts(
        "non-owner cannot edit",
        chain.send,
        registry.functions.updateInstance(1, "Hijacked", "", ""),
        other,
        FEE,
    )

    print("\nURL validation")
    bad_urls = [
        ("javascript scheme", "javascript:alert(1)"),
        ("no scheme", "rpc.example.org"),
        ("embedded credentials", "https://user:pass@rpc.example.org"),
        ("quote injection", 'https://rpc.example.org/"onload="x'),
        ("angle bracket", "https://rpc.example.org/<script>"),
        ("backtick", "https://rpc.example.org/`id`"),
        ("whitespace", "https://rpc.example.org /x"),
        ("empty authority", "https:///rpc.example.org"),
        ("non-ascii", "https://rpc.exämple.org"),
    ]
    for label, url in bad_urls:
        reverts(f"rejects {label}", chain.send, registry.functions.addEndpoints(1, [0], [url]), operator, FEE)
    for label, url in [("ws", "ws://rpc.example.org:8546"), ("grpc", "grpc://grpc.example.org:9090")]:
        chain.send(registry.functions.addEndpoints(1, [1], [url]), operator, FEE)
        check(f"accepts {label} scheme", True)

    print("\nsybil and capacity limits")
    for i in range(3):
        chain.send(
            registry.functions.createInstance(f"Node {i}", "EU", "", [0], [f"https://rpc{i}.example.org"]),
            operator,
            FEE,
        )
    reverts(
        "per-owner instance cap holds",
        chain.send,
        registry.functions.createInstance("Fifth", "EU", "", [0], ["https://rpc9.example.org"]),
        operator,
        FEE,
    )

    print("\nepoch integrity")
    endpoint = registry.functions.getEndpoint(1).call()
    epoch_before = endpoint[4]
    chain.send(registry.functions.updateEndpoint(1, 0, "https://rpc.example.org"), operator, FEE)
    check(
        "paid no-op does not advance the epoch",
        registry.functions.getEndpoint(1).call()[4] == epoch_before,
    )
    chain.send(registry.functions.updateEndpoint(1, 0, "https://rpc-new.example.org"), operator, FEE)
    check(
        "real URL change advances the epoch",
        registry.functions.getEndpoint(1).call()[4] == epoch_before + 1,
    )
    chain.send(registry.functions.updateEndpoint(1, 6, "https://rpc-new.example.org"), operator, FEE)
    check(
        "kind change advances the epoch",
        registry.functions.getEndpoint(1).call()[4] == epoch_before + 2,
    )
    chain.send(registry.functions.updateEndpoint(1, 0, "https://rpc-new.example.org"), operator, FEE)

    print("\nchecker authorization timelock")
    reverts("unauthorized checker cannot attest", chain.send, registry.functions.attest([(1, 3, 5000, 10, 5000, True, False)]), checker_a)
    chain.send(registry.functions.proposeChecker(checker_a, "checker-a", "https://a.example/status.json"), admin)
    reverts("checker cannot be committed early", chain.send, registry.functions.commitChecker(checker_a), admin)
    reverts(
        "non-admin cannot propose a checker",
        chain.send,
        registry.functions.proposeChecker(other, "rogue", ""),
        other,
    )
    chain.travel(2 * DAY + 60)
    chain.send(registry.functions.commitChecker(checker_a), admin)
    check("checker authorized after the delay", registry.functions.getChecker(checker_a).call()[0] is True)

    for who, name in ((checker_b, "checker-b"), (checker_c, "checker-c")):
        chain.send(registry.functions.proposeChecker(who, name, ""), admin)
    chain.travel(2 * DAY + 60)
    for who in (checker_b, checker_c):
        chain.send(registry.functions.commitChecker(who), admin)
    check("three checkers authorized", registry.functions.authorizedCheckerCount().call() == 3)

    print("\nattestation and quorum")
    endpoint_id, epoch = 1, registry.functions.getEndpoint(1).call()[4]
    chain.send(registry.functions.attest([(endpoint_id, epoch, 9000, 250, 1000, True, False)]), checker_a)
    check("one adverse report does not flag", registry.functions.getEndpoint(1).call()[9] is False)
    chain.send(registry.functions.attest([(endpoint_id, epoch, 9100, 260, 900, True, False)]), checker_a)
    aggregate = registry.functions.getLatestAggregate(endpoint_id).call()
    check("same checker cannot double-count in a day", aggregate[2] == 1, f"reportCount={aggregate[2]}")

    chain.send(registry.functions.attest([(endpoint_id, epoch, 9200, 300, 800, True, False)]), checker_b)
    check("quorum of two flags the endpoint", registry.functions.getEndpoint(1).call()[9] is True)
    check("lifetime adverse counter increments", registry.functions.getEndpoint(1).call()[5] == 1)

    chain.send(registry.functions.attest([(endpoint_id, epoch, 9500, 300, 500, True, False)]), checker_c)
    check("aggregate counts all three reports", registry.functions.getLatestAggregate(endpoint_id).call()[2] == 3)

    print("\nstale and irrelevant attestations are skipped, not reverted")
    chain.send(registry.functions.attest([(endpoint_id, epoch - 1, 100, 10, 9900, True, False)]), checker_a)
    check("stale epoch is ignored", registry.functions.getLatestAggregate(endpoint_id).call()[2] == 3)
    chain.send(registry.functions.attest([(999, epoch, 100, 10, 9900, True, False)]), checker_a)
    check("unknown endpoint is ignored", True)
    receipt = chain.send(
        registry.functions.attest(
            [
                (endpoint_id, epoch, 9300, 300, 700, True, False),  # duplicate for the day
                (2, registry.functions.getEndpoint(2).call()[4], 9000, 400, 1000, True, False),  # fresh, must land
            ]
        ),
        checker_b,
    )
    check("one stale entry does not discard the batch", registry.functions.getLatestAggregate(2).call()[2] == 1)

    print("\nrecovery clears the flag without a paid write")
    chain.travel(DAY + 60)
    chain.send(registry.functions.attest([(endpoint_id, epoch, 9990, 90, 10, True, False)]), checker_a)
    check("one healthy report is not enough to clear", registry.functions.getEndpoint(1).call()[9] is True)
    chain.send(registry.functions.attest([(endpoint_id, epoch, 9995, 85, 5, True, False)]), checker_b)
    check("healthy quorum clears the flag", registry.functions.getEndpoint(1).call()[9] is False)
    check("lifetime adverse history is preserved", registry.functions.getEndpoint(1).call()[5] == 1)

    print("\nreliability history survives a paid endpoint edit")
    chain.travel(DAY + 60)  # a checker gets one report per endpoint epoch per UTC day
    chain.send(registry.functions.attest([(endpoint_id, epoch, 5000, 900, 5000, True, False)]), checker_a)
    chain.send(registry.functions.attest([(endpoint_id, epoch, 5100, 950, 4900, True, False)]), checker_c)
    check("flagged again", registry.functions.getEndpoint(1).call()[9] is True)
    before = registry.functions.getEndpoint(1).call()[5]
    chain.send(registry.functions.updateEndpoint(1, 0, "https://rpc-fresh.example.org"), operator, FEE)
    after = registry.functions.getEndpoint(1).call()
    check("edit clears the live flag", after[9] is False)
    check("lifetime adverse count is not erased by an edit", after[5] == before, f"{after[5]} vs {before}")
    check("original registration date is preserved", after[2] == endpoint[2])

    print("\nproof of control")
    fresh_epoch = registry.functions.getEndpoint(1).call()[4]
    chain.send(registry.functions.attest([(1, fresh_epoch, 0, 0, 0, False, True)]), checker_a)
    check("one attestation does not verify", registry.functions.getEndpoint(1).call()[10] is False)
    chain.send(registry.functions.attest([(1, fresh_epoch, 0, 0, 0, False, True)]), checker_a)
    check("same checker cannot self-quorum", registry.functions.verificationCount(1, fresh_epoch).call() == 1)
    chain.send(registry.functions.attest([(1, fresh_epoch, 0, 0, 0, False, True)]), checker_b)
    check("verification quorum verifies", registry.functions.getEndpoint(1).call()[10] is True)
    chain.send(registry.functions.updateEndpoint(1, 0, "https://rpc-moved.example.org"), operator, FEE)
    check("a URL change revokes verification", registry.functions.getEndpoint(1).call()[10] is False)

    print("\nconfiguration timelock and floors")
    reverts("quorum below the floor is rejected", chain.send, registry.functions.proposeConfiguration(FEE, 9900, 1, 2), admin)
    reverts("unreachable threshold is rejected", chain.send, registry.functions.proposeConfiguration(FEE, 4000, 2, 2), admin)
    reverts(
        "fee above the ceiling is rejected",
        chain.send,
        registry.functions.proposeConfiguration(to_wei(2000, "ether"), 9900, 2, 2),
        admin,
    )
    chain.send(registry.functions.proposeConfiguration(to_wei(2, "ether"), 9500, 3, 2), admin)
    reverts("config cannot be committed early", chain.send, registry.functions.commitConfiguration(), admin)
    check("fee unchanged while pending", registry.functions.endpointWriteFee().call() == FEE)
    chain.travel(2 * DAY + 60)
    chain.send(registry.functions.commitConfiguration(), admin)
    check("config applies after the delay", registry.functions.endpointWriteFee().call() == to_wei(2, "ether"))
    chain.send(registry.functions.proposeConfiguration(FEE, 9900, 2, 2), admin)
    chain.travel(2 * DAY + 60)
    chain.send(registry.functions.commitConfiguration(), admin)

    print("\nchecker revocation is immediate")
    chain.send(registry.functions.revokeChecker(checker_c), admin)
    check("revoked immediately", registry.functions.getChecker(checker_c).call()[0] is False)
    reverts(
        "revoked checker cannot attest",
        chain.send,
        registry.functions.attest([(2, registry.functions.getEndpoint(2).call()[4], 100, 10, 9900, True, False)]),
        checker_c,
    )

    print("\ntreasury cannot block the registry")
    blocker_source = """
    // SPDX-License-Identifier: MIT
    pragma solidity 0.8.24;
    contract Blocker { receive() external payable { revert("no"); } }
    """
    (CONTRACTS.parent / "test" / "_Blocker.sol").write_text(blocker_source)
    proc = subprocess.run(
        [solc_binary(), "--optimize", "--bin", "--abi", "--evm-version", "paris",
         str(CONTRACTS.parent / "test" / "_Blocker.sol")],
        capture_output=True, text=True,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    blocker_bin = lines[lines.index("Binary:") + 1]
    blocker_abi = json.loads(lines[lines.index("Contract JSON ABI") + 1])
    blocker = chain.deploy({"abi": blocker_abi, "evm": {"bytecode": {"object": blocker_bin}}})

    chain.send(registry.functions.proposeFeeRecipient(blocker.address), admin)
    chain.travel(2 * DAY + 60)
    chain.send(registry.functions.commitFeeRecipient(), admin)
    chain.send(
        registry.functions.createInstance("Still working", "EU", "", [0], ["https://rpc-x.example.org"]),
        other,
        FEE,
    )
    check("registration still works with a hostile fee recipient", registry.functions.activeInstanceCount().call() == 5)
    reverts("withdraw to a hostile recipient reverts safely", chain.send, registry.functions.withdrawFees(), other)
    check("fees remain in the contract", chain.w3.eth.get_balance(registry.address) > 0)

    chain.send(registry.functions.proposeFeeRecipient(treasury), admin)
    chain.travel(2 * DAY + 60)
    chain.send(registry.functions.commitFeeRecipient(), admin)
    balance_before = chain.w3.eth.get_balance(treasury)
    accrued = chain.w3.eth.get_balance(registry.address)
    chain.send(registry.functions.withdrawFees(), other)  # permissionless sweep
    check("anyone may sweep to the configured recipient", chain.w3.eth.get_balance(treasury) == balance_before + accrued)
    check("contract balance is zero after sweep", chain.w3.eth.get_balance(registry.address) == 0)

    print("\ninstance handover")
    chain.send(registry.functions.offerInstance(1, other), operator)
    reverts("only the offeree can claim", chain.send, registry.functions.claimInstance(1), admin)
    chain.send(registry.functions.claimInstance(1), other)
    check("ownership transferred", registry.functions.getInstance(1).call()[1] == other)
    check("old owner's slot freed", registry.functions.ownerActiveInstanceCount(operator).call() == 3)

    print("\nadmin handover is two-step")
    chain.send(registry.functions.transferAdmin(other), admin)
    check("admin unchanged until accepted", registry.functions.admin().call() == admin)
    reverts("only the nominee can accept", chain.send, registry.functions.acceptAdmin(), checker_a)
    chain.send(registry.functions.acceptAdmin(), other)
    check("admin transferred on acceptance", registry.functions.admin().call() == other)
    chain.send(registry.functions.transferAdmin(admin), other)
    chain.send(registry.functions.acceptAdmin(), admin)

    print("\ndeletion and the active index")
    before_count = registry.functions.activeInstanceCount().call()
    chain.send(registry.functions.deleteInstance(2), operator)
    check("active count drops", registry.functions.activeInstanceCount().call() == before_count - 1)
    ids, _, _ = registry.functions.activeInstanceIds(0, 100).call()
    check("deleted instance leaves the active list", 2 not in ids)
    check("no gaps in the active list", len(ids) == before_count - 1)
    reverts("deleted instance cannot be edited", chain.send, registry.functions.updateInstance(2, "x", "", ""), operator, FEE)

    print("\nlens")
    page = lens.functions.getDirectory(0, 25).call()
    instances, counts, endpoints, aggregates, verifications, next_cursor, total, at_rev = page
    check("lens returns every live instance", len(instances) == registry.functions.activeInstanceCount().call())
    check("counts match the flattened endpoints", sum(counts) == len(endpoints))
    check("aggregates align with endpoints", len(aggregates) == len(endpoints))
    check("verification counts align", len(verifications) == len(endpoints))
    check("lens reports the registry revision", at_rev == registry.functions.revision().call())
    check("no deleted instance in the page", all(inst[5] for inst in instances))
    check("no deleted endpoint in the page", all(ep[8] for ep in endpoints))
    summary = lens.functions.getSummary().call()
    check("summary endpoint count matches", summary[2] == len(endpoints))
    checkers = lens.functions.getCheckers().call()
    check("lens lists checkers with live authorization", len(checkers) == 3 and [c[1] for c in checkers] == [True, True, False])
    empty = lens.functions.getDirectory(9999, 25).call()
    check("out-of-range page is empty, not a revert", len(empty[0]) == 0)

    print("\ncontest")
    owned = registry.functions.getInstanceEndpointIds(3).call()[0]
    chain.send(registry.functions.contestEndpoint(owned, "Regional transit incident, evidence at example.org/x"), operator)
    check("owner may contest on-chain", True)
    reverts("non-owner cannot contest", chain.send, registry.functions.contestEndpoint(owned, "not mine"), checker_a)
    reverts("empty contest reason is rejected", chain.send, registry.functions.contestEndpoint(owned, ""), operator)

    (CONTRACTS.parent / "test" / "_Blocker.sol").unlink(missing_ok=True)

    print(f"\n{'-' * 62}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for name, detail in FAILED:
        print(f"  FAILED: {name} — {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
