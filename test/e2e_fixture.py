#!/usr/bin/env python3
"""End-to-end fixture: a live registry on an in-process EVM, exposed over HTTP.

Deploys both contracts to an in-memory chain, fills the register with realistic
records, authorizes two checkers, anchors attestations, writes a signed
telemetry snapshot, and serves both a JSON-RPC endpoint and the web directory.

Used by test/e2e.js, which loads the real page against it. Run standalone to
click around a fully populated register with no node and no funded keys:

    python test/e2e_fixture.py --hold
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eth_account import Account
from eth_utils import to_wei
from web3 import EthereumTesterProvider, Web3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))
import checker as bot  # noqa: E402

FEE = to_wei(1, "ether")
DAY = 86_400

SEED = [
    ("Northern Europe node", "Tallinn, EE", "https://ee-node.example", [
        (0, "https://rpc.ee.example.org"),
        (3, "https://rpc.ee.example.org:26657"),
        (2, "https://lcd.ee.example.org"),
    ]),
    ("Zurich archive", "Zug, CH", "https://ch-archive.example", [
        (0, "https://archive.ch.example.org"),
        (1, "wss://ws.ch.example.org"),
        (4, "grpc://grpc.ch.example.org:9090"),
    ]),
    ("Community mirror", "Frankfurt, DE", "", [
        (0, "https://rpc.de.example.org"),
        (7, "https://snapshots.de.example.org"),
    ]),
    ("Pacific edge", "Singapore, SG", "https://sg.example", [
        (0, "https://rpc.sg.example.org"),
        (6, "https://index.sg.example.org"),
    ]),
]


def load_artifacts(solc: str) -> dict:
    """Prefer the artifacts scripts/build.sh already produced, so the test
    exercises exactly the bytecode that will be deployed — and so a machine
    without solc can still run it. Falls back to compiling."""
    abi_dir = ROOT / "contract" / "abi"
    bin_dir = ROOT / "contract" / "build"
    names = ("GenesisL1APIRegistry", "GenesisL1RegistryLens")
    if all((abi_dir / f"{n}.json").exists() and (bin_dir / f"{n}.bin").exists() for n in names):
        return {
            f"{n}.sol": {
                n: {
                    "abi": json.loads((abi_dir / f"{n}.json").read_text()),
                    "evm": {"bytecode": {"object": (bin_dir / f"{n}.bin").read_text().strip()}},
                }
            }
            for n in names
        }
    return compile_contracts(solc)


def compile_contracts(solc: str) -> dict:
    standard = {
        "language": "Solidity",
        "sources": {
            name: {"content": (ROOT / "contract" / name).read_text()}
            for name in ("GenesisL1APIRegistry.sol", "GenesisL1RegistryLens.sol")
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
            "evmVersion": "paris",
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    proc = subprocess.run([solc, "--standard-json"], input=json.dumps(standard), capture_output=True, text=True)
    out = json.loads(proc.stdout)
    fatal = [e for e in out.get("errors", []) if e["severity"] == "error"]
    if fatal:
        sys.exit("\n".join(e["formattedMessage"] for e in fatal))
    return out["contracts"]


class RpcBridge(ThreadingHTTPServer):
    """Minimal JSON-RPC over HTTP in front of the in-process EVM, so a browser
    can talk to the same chain the fixture built."""

    allow_reuse_address = True


QUANTITY_FIELDS = ("gas", "gasPrice", "value", "nonce", "maxFeePerGas", "maxPriorityFeePerGas")


def from_hex(value):
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return value
    return value


def decode_params(method: str, params, default_sender: str):
    """Ethers speaks hex quantities; the in-process EVM wants Python integers.
    A browser also omits gas and from on eth_call and relies on the node's
    defaults, which this EVM does not supply."""
    if method not in ("eth_call", "eth_estimateGas") or not params or not isinstance(params[0], dict):
        return params
    params = list(params)
    call = {key: (from_hex(value) if key in QUANTITY_FIELDS else value) for key, value in params[0].items()}
    call.setdefault("gas", 50_000_000)
    call.setdefault("from", default_sender)
    call.setdefault("value", 0)
    params[0] = call
    return params


def jsonrpc(value):
    """The in-process EVM answers in Python types. Ethereum JSON-RPC is defined
    in hex quantities and hex data, and a browser client will reject anything
    else, so normalize on the way out."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return hex(value)
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, dict):
        return {key: jsonrpc(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonrpc(item) for item in value]
    return value


def make_rpc_handler(w3: Web3):
    class Handler(SimpleHTTPRequestHandler):
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            batch = request if isinstance(request, list) else [request]
            replies = []
            for item in batch:
                try:
                    params = decode_params(item["method"], item.get("params", []), w3.eth.accounts[0])
                    answer = w3.provider.make_request(item["method"], params)
                    reply = {"jsonrpc": "2.0", "id": item.get("id")}
                    if "error" in answer:
                        reply["error"] = answer["error"] if isinstance(answer["error"], dict) else {
                            "code": -32000, "message": str(answer["error"])
                        }
                    else:
                        reply["result"] = jsonrpc(answer.get("result"))
                    replies.append(reply)
                except Exception as exc:  # noqa: BLE001
                    replies.append({
                        "jsonrpc": "2.0", "id": item.get("id"),
                        "error": {"code": -32000, "message": str(exc)},
                    })
            body = json.dumps(replies if isinstance(request, list) else replies[0]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    return Handler


def make_static_handler(directory: Path):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, *args) -> None:
            pass

    return Handler


def build(solc: str, out_dir: Path) -> dict:
    artifacts = load_artifacts(solc)
    registry_art = artifacts["GenesisL1APIRegistry.sol"]["GenesisL1APIRegistry"]
    lens_art = artifacts["GenesisL1RegistryLens.sol"]["GenesisL1RegistryLens"]

    w3 = Web3(EthereumTesterProvider())
    tester = w3.provider.ethereum_tester
    admin, treasury = w3.eth.accounts[0], w3.eth.accounts[9]
    operators = w3.eth.accounts[1:5]

    def deploy(artifact, *args):
        contract = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["evm"]["bytecode"]["object"])
        tx = contract.constructor(*args).transact({"from": admin})
        receipt = w3.eth.wait_for_transaction_receipt(tx)
        return w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])

    registry = deploy(registry_art, admin, treasury, FEE)
    lens = deploy(lens_art, registry.address)

    def send(fn, sender, value=0):
        receipt = w3.eth.wait_for_transaction_receipt(fn.transact({"from": sender, "value": value}))
        assert receipt.status == 1, "fixture transaction reverted"
        return receipt

    for index, (name, region, info, endpoints) in enumerate(SEED):
        kinds = [kind for kind, _ in endpoints]
        urls = [url for _, url in endpoints]
        send(registry.functions.createInstance(name, region, info, kinds, urls),
             operators[index % len(operators)], FEE * len(kinds))

    # Two independent checkers, authorized through the timelock.
    checkers = [Account.from_key("0x" + str(n) * 64) for n in (1, 2)]
    for n, account in enumerate(checkers):
        send(registry.functions.proposeChecker(account.address, f"checker-{n + 1}",
                                               f"https://checker{n + 1}.example/status.json"), admin)
    tester.time_travel(w3.eth.get_block("latest").timestamp + 2 * DAY + 60)
    tester.mine_block()
    for account in checkers:
        send(registry.functions.commitChecker(account.address), admin)
        w3.eth.send_transaction({"from": admin, "to": account.address, "value": to_wei(5, "ether")})

    endpoints = []
    cursor = 0
    while True:
        records, owners, next_cursor, _ = lens.functions.getAllEndpoints(cursor, 25).call()
        endpoints.extend(zip(records, owners))
        if next_cursor <= cursor or not records:
            break
        cursor = next_cursor

    # Endpoint 5 gets flagged by quorum; endpoint 2 has proven host control.
    flagged = endpoints[4][0]
    for account in checkers:
        signed = registry.functions.attest(
            [(int(flagged[0]), int(flagged[4]), 9100, 880, 900, True, True)]
        ).build_transaction({
            "from": account.address, "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": w3.eth.chain_id, "gas": 900_000, "gasPrice": w3.eth.gas_price,
        })
        w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(Account.sign_transaction(signed, account.key).raw_transaction))

    verified = endpoints[1][0]
    for account in checkers:
        signed = registry.functions.attest(
            [(int(verified[0]), int(verified[4]), 0, 0, 0, False, True)]
        ).build_transaction({
            "from": account.address, "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": w3.eth.chain_id, "gas": 900_000, "gasPrice": w3.eth.gas_price,
        })
        w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(Account.sign_transaction(signed, account.key).raw_transaction))

    # Two signed telemetry feeds with plausible, slightly divergent measurements.
    random.seed(29)
    now = int(time.time())
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, account in enumerate(checkers):
        payload = {
            "schema": bot.SCHEMA,
            "generated_at": now,
            "checker": {"address": account.address, "name": f"checker-{index + 1}", "version": bot.VERSION},
            "registry": registry.address,
            "lens": lens.address,
            "chain_id": w3.eth.chain_id,
            "reliability_threshold_bps": 9900,
            "window": {"interval_seconds": 60, "probe_burst": 2, "retention_days": 35},
            "endpoints": [synthesize(record, now, index) for record, _ in endpoints],
        }
        bot.publish(out_dir / f"status-{index + 1}.json", payload, account)

    return {
        "w3": w3,
        "registry": registry.address,
        "lens": lens.address,
        "chainId": w3.eth.chain_id,
        "operator": operators[0],
        "checkers": [account.address for account in checkers],
        "flagged": int(flagged[0]),
        "verified": int(verified[0]),
        "instances": len(SEED),
        "endpoints": len(endpoints),
    }


def synthesize(record: tuple, now: int, feed_index: int) -> dict:
    endpoint_id = int(record[0])
    healthy = endpoint_id not in (5, 7)
    base = 9990 if healthy else 9100
    uptime = max(0, min(10_000, base - random.randint(0, 40) + feed_index * 5))
    buckets = []
    for hour in range(24):
        bad_hour = (not healthy) and hour in (9, 10, 11)
        buckets.append({
            "t": (now // 3600 - 23 + hour) * 3600,
            "samples": random.randint(90, 120),
            "uptime_bps": 4200 if bad_hour else max(9500, uptime - random.randint(0, 60)),
            "latency_p95_ms": random.randint(70, 260),
        })
    window = {
        "samples": 2880, "counted": 2860, "throttled": 20,
        "uptime_bps": uptime, "error_bps": 10_000 - uptime,
        "latency_avg_ms": random.randint(50, 140),
        "latency_p50_ms": random.randint(45, 120),
        "latency_p95_ms": random.randint(90, 320),
        "latency_p99_ms": random.randint(150, 600),
        "first_checked": now - 86_000, "last_checked": now - 30,
        "last_success": now - 30, "bytes_total": 91_000_000,
        "latest_height": 8_413_002 + endpoint_id,
        "last_error": "" if healthy else "RuntimeError: HTTP 502",
    }
    return {
        "id": endpoint_id, "instance_id": int(record[1]), "epoch": int(record[4]),
        "kind": int(record[7]), "kind_name": bot.KIND_NAMES[int(record[7])],
        "url": record[12], "unreliable": bool(record[9]), "verified": bool(record[10]),
        "adverse_days": int(record[5]),
        "stats_1h": dict(window, samples=120, counted=118),
        "stats_24h": window,
        "stats_30d": dict(window, samples=86_400, counted=86_000),
        "buckets_24h": buckets,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--solc", default="solc")
    parser.add_argument("--rpc-port", type=int, default=8645)
    parser.add_argument("--web-port", type=int, default=8646)
    parser.add_argument("--hold", action="store_true", help="keep serving until interrupted")
    args = parser.parse_args()

    web_dir = ROOT / "web"
    context = build(args.solc, web_dir / "data")

    rpc = RpcBridge(("127.0.0.1", args.rpc_port), make_rpc_handler(context["w3"]))
    threading.Thread(target=rpc.serve_forever, daemon=True).start()
    static = ThreadingHTTPServer(("127.0.0.1", args.web_port), make_static_handler(web_dir))
    threading.Thread(target=static.serve_forever, daemon=True).start()

    manifest = {
        "rpcUrl": f"http://127.0.0.1:{args.rpc_port}",
        "webUrl": f"http://127.0.0.1:{args.web_port}",
        "registryAddress": context["registry"],
        "lensAddress": context["lens"],
        "chainId": context["chainId"],
        "checkers": context["checkers"],
        "flaggedEndpoint": context["flagged"],
        "verifiedEndpoint": context["verified"],
        "instances": context["instances"],
        "endpoints": context["endpoints"],
    }
    (ROOT / "test" / "e2e-manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2), flush=True)

    if args.hold:
        print(f"\nOpen {manifest['webUrl']}/  (Ctrl-C to stop)", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        time.sleep(float(0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
