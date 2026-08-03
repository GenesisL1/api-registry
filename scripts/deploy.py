#!/usr/bin/env python3
"""Deploy the registry and its lens, then print the config.js block to paste.

    export DEPLOYER_PRIVATE_KEY=0x...
    python scripts/deploy.py \
        --rpc https://rpc.genesisl1.org \
        --admin 0xADMIN_MULTISIG \
        --fee-recipient 0xTREASURY_MULTISIG

Before it spends anything, this checks that the fee recipient can actually
receive a plain value transfer on this chain. Fees accrue inside the registry
and are pulled out later, so a recipient that cannot receive can never block a
registration — but it can strand the funds, and that is worth catching here
rather than after the first withdrawal attempt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]


def artifact(name: str) -> tuple[list, str]:
    abi = json.loads((ROOT / "contract" / "abi" / f"{name}.json").read_text())
    code = (ROOT / "contract" / "build" / f"{name}.bin").read_text().strip()
    if not code:
        sys.exit(f"{name} has no bytecode. Run scripts/build.sh first.")
    return abi, code


def deploy(w3: Web3, account, name: str, *args) -> str:
    abi, code = artifact(name)
    contract = w3.eth.contract(abi=abi, bytecode=code)
    tx = contract.constructor(*args).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
        "gasPrice": w3.eth.gas_price,
    })
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  {name}: {tx_hash.hex()} …")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=600)
    if receipt.status != 1:
        sys.exit(f"{name} deployment reverted")
    size = len(w3.eth.get_code(receipt.contractAddress))
    print(f"  {name}: {receipt.contractAddress}  ({size} bytes, gas {receipt.gasUsed})")
    return receipt.contractAddress


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the GenesisL1 API registry")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", "https://rpc.genesisl1.org"))
    parser.add_argument("--admin", required=True, help="registry administrator; use a multisig")
    parser.add_argument("--fee-recipient", required=True, help="fee destination; use a multisig")
    parser.add_argument("--fee", default="1", help="write fee in L1 coin")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    key = os.getenv("DEPLOYER_PRIVATE_KEY", "").strip()
    if not key:
        sys.exit("Set DEPLOYER_PRIVATE_KEY in the environment. It is never taken as an argument.")
    account = Account.from_key(key)

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        sys.exit(f"Cannot reach {args.rpc}")

    admin = Web3.to_checksum_address(args.admin)
    recipient = Web3.to_checksum_address(args.fee_recipient)
    fee = w3.to_wei(args.fee, "ether")

    print(f"chain {w3.eth.chain_id} via {args.rpc}")
    print(f"deployer      {account.address}  ({w3.from_wei(w3.eth.get_balance(account.address), 'ether')} L1)")
    print(f"admin         {admin}")
    print(f"fee recipient {recipient}  ({'contract' if w3.eth.get_code(recipient) else 'externally owned account'})")
    print(f"write fee     {args.fee} L1 coin per endpoint")

    # A Safe accepts plain transfers; a Cosmos module account does not, and the
    # bank module will reject the transfer when fees are eventually swept.
    try:
        w3.eth.call({"from": account.address, "to": recipient, "value": 1})
        print("fee recipient accepts a value transfer: yes")
    except Exception as exc:  # noqa: BLE001
        print(f"\n!! The fee recipient rejected a test value transfer: {exc}")
        print("!! Registrations will still work — fees accrue in the contract — but")
        print("!! withdrawFees() will revert until the recipient is changed.")
        if not args.yes and input("continue anyway? [y/N] ").strip().lower() != "y":
            return 1

    if not args.yes and input("\ndeploy? [y/N] ").strip().lower() != "y":
        return 1

    print("\ndeploying")
    registry = deploy(w3, account, "GenesisL1APIRegistry", admin, recipient, fee)
    lens = deploy(w3, account, "GenesisL1RegistryLens", registry)

    print("\nPaste into web/config.js:\n")
    print(f'  registryAddress: "{registry}",')
    print(f'  lensAddress: "{lens}",')
    print("\nAnd for the checker:\n")
    print(f"  --registry {registry} --lens {lens}")
    print("\nVerify the sources with contract/build/standard-input.json.")
    print("Next: authorize at least two checkers (proposeChecker, wait out the")
    print("timelock, then commitChecker). Until two exist, no endpoint can be flagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
