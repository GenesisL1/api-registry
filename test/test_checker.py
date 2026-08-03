#!/usr/bin/env python3
"""Checker test suite.

Stands up adversarial local servers and points the real probe code at them, so
the SSRF, redirect, bounded-read and signing behaviour is verified against live
sockets rather than asserted in a comment.

    pip install -r bot/requirements.txt
    python test/test_checker.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from argparse import Namespace
from pathlib import Path

import aiohttp
from aiohttp import web
from eth_account import Account
from eth_account.messages import encode_defunct

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

import checker as mod  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  \033[32mpass\033[0m  {name}")
    else:
        FAILED.append((name, detail))
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def make_args(**overrides) -> Namespace:
    base = dict(
        chain_id=29, cosmos_chain_id="", timeout=5.0, max_bytes=256 * 1024,
        stall_seconds=600, allow_private=True, no_dns_verification=True, burst=1,
    )
    base.update(overrides)
    return Namespace(**base)


def endpoint(kind: int, url: str, **overrides) -> mod.Endpoint:
    base = dict(
        id=1, instance_id=1, kind=kind, url=url, epoch=1, added_at=0, updated_at=0,
        unreliable=False, verified=False, adverse_days=0,
        owner="0x1111111111111111111111111111111111111111",
    )
    base.update(overrides)
    return mod.Endpoint(**base)


# ----------------------------------------------------------- adversarial hosts

async def build_app() -> web.Application:
    app = web.Application()

    async def evm(request: web.Request) -> web.Response:
        body = await request.json()
        results = {"eth_chainId": "0x1d", "eth_blockNumber": "0x2710", "net_version": "29"}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": results[body["method"]]})

    async def wrong_chain(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": "0x1"})

    async def frozen(request: web.Request) -> web.Response:
        body = await request.json()
        results = {"eth_chainId": "0x1d", "eth_blockNumber": "0x64", "net_version": "29"}
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": results[body["method"]]})

    async def rpc_error(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "boom"}})

    async def redirect_to_metadata(request: web.Request) -> web.Response:
        raise web.HTTPFound(location="http://169.254.169.254/latest/meta-data/")

    async def flood(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/json"})
        await response.prepare(request)
        chunk = b"x" * 65536
        for _ in range(200):  # 13 MB, far past the cap
            await response.write(chunk)
        return response

    async def throttled(request: web.Request) -> web.Response:
        return web.json_response({"error": "slow down"}, status=429, headers={"Retry-After": "30"})

    async def well_known(request: web.Request) -> web.Response:
        return web.Response(text="gl1-owner=0x1111111111111111111111111111111111111111\n")

    async def well_known_wrong(request: web.Request) -> web.Response:
        return web.Response(text="gl1-owner=0x2222222222222222222222222222222222222222\n")

    async def websocket(request: web.Request) -> web.WebSocketResponse:
        socket_ = web.WebSocketResponse()
        await socket_.prepare(request)
        async for message in socket_:
            body = json.loads(message.data)
            results = {"eth_chainId": "0x1d", "eth_blockNumber": "0x2710"}
            await socket_.send_json({"jsonrpc": "2.0", "id": body["id"], "result": results[body["method"]]})
        return socket_

    async def dead_socket(request: web.Request) -> web.Response:
        return web.Response(text="hello, i am not http/2")

    app.router.add_post("/evm", evm)
    app.router.add_post("/wrong-chain", wrong_chain)
    app.router.add_post("/frozen", frozen)
    app.router.add_post("/rpc-error", rpc_error)
    app.router.add_route("*", "/redirect", redirect_to_metadata)
    app.router.add_route("*", "/flood", flood)
    app.router.add_route("*", "/throttled", throttled)
    app.router.add_get("/.well-known/genesisl1-registry.txt", well_known)
    app.router.add_get("/wrong/.well-known/genesisl1-registry.txt", well_known_wrong)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/plain", dead_socket)
    return app


# ------------------------------------------------------------------- the tests

async def run() -> None:
    app = await build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    args = make_args()
    connector = aiohttp.TCPConnector(resolver=mod.GuardedResolver(allow_private=True))
    session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=args.timeout))
    prober = mod.Prober(session, args)

    print("url validation")
    for label, url in [
        ("loopback literal", "http://127.0.0.1:8545"),
        ("link-local metadata", "http://169.254.169.254/"),
        ("private range", "https://10.0.0.5:26657"),
        ("ipv6 loopback", "http://[::1]:8545"),
        ("ipv4-mapped ipv6", "http://[::ffff:127.0.0.1]:8545"),
        ("credentials", "https://user:pass@rpc.example.org"),
        ("bad scheme", "file:///etc/passwd"),
        ("control character", "https://rpc.example.org/\n"),
        ("overlong url", "https://rpc.example.org/" + "a" * 300),
    ]:
        try:
            mod.validate_url(url)
            check(f"rejects {label}", False, "accepted")
        except ValueError:
            check(f"rejects {label}", True)

    for label, url in [("public host", "https://rpc.genesisl1.org"), ("ws with port", "wss://rpc.example.org:8546")]:
        try:
            mod.validate_url(url)
            check(f"accepts {label}", True)
        except ValueError as exc:
            check(f"accepts {label}", False, str(exc))

    # The escape hatch has to work, and only when it is asked for.
    try:
        mod.validate_url("http://127.0.0.1:8545", allow_private=True)
        check("--allow-private permits a private literal", True)
    except ValueError as exc:
        check("--allow-private permits a private literal", False, str(exc))

    print("\nresolver guard")
    guard = mod.GuardedResolver(allow_private=False)
    for label, host in [("localhost", "localhost"), ("loopback ip", "127.0.0.1")]:
        try:
            await guard.resolve(host, 80)
            check(f"resolver blocks {label}", False, "resolved")
        except OSError:
            check(f"resolver blocks {label}", True)
    await guard.close()

    check("ipv4-mapped ipv6 is not public", mod.is_public_address("::ffff:127.0.0.1") is False)
    check("6to4 wrapping a private v4 is not public", mod.is_public_address("2002:0a00:0001::") is False)
    check("a real public address is public", mod.is_public_address("8.8.8.8") is True)

    print("\nprobes against live servers")
    result = await prober.probe(endpoint(mod.KIND_EVM_HTTP, f"{base}/evm"))
    check("healthy EVM endpoint passes", result.ok, result.error)
    check("block height is captured", result.height == 10000, str(result.height))

    result = await prober.probe(endpoint(mod.KIND_EVM_HTTP, f"{base}/wrong-chain", id=2))
    check("endpoint on the wrong chain fails", not result.ok and "wrong chain" in result.error, result.error)

    result = await prober.probe(endpoint(mod.KIND_EVM_HTTP, f"{base}/rpc-error", id=3))
    check("JSON-RPC error response fails", not result.ok, result.error)

    result = await prober.probe(endpoint(mod.KIND_EVM_HTTP, f"{base}/redirect", id=4))
    check("redirect is refused, not followed", not result.ok and "redirect" in result.error, result.error)

    result = await prober.probe(endpoint(mod.KIND_OTHER, f"{base}/flood", id=5))
    check("oversized body is aborted", not result.ok and "exceeded" in result.error, result.error)

    result = await prober.probe(endpoint(mod.KIND_OTHER, f"{base}/throttled", id=6))
    check("rate limiting is recorded as throttled", result.throttled and not result.ok, result.error)

    result = await prober.probe(endpoint(mod.KIND_EVM_WS, f"ws://127.0.0.1:{port}/ws", id=7))
    check("websocket handshake and call succeed", result.ok, result.error)
    check("websocket height captured", result.height == 10000, str(result.height))

    result = await prober.probe(endpoint(mod.KIND_GRPC, f"grpc://127.0.0.1:{port}", id=8))
    check("a listening non-HTTP/2 port fails the gRPC probe", not result.ok, result.error)

    print("\nstall detection")
    frozen_endpoint = endpoint(mod.KIND_EVM_HTTP, f"{base}/frozen", id=9)
    prober.args.stall_seconds = 0
    first = await prober.probe(frozen_endpoint)
    second = await prober.probe(frozen_endpoint)
    check("first sight of a height is fine", first.ok, first.error)
    check("a frozen height is reported as a failure", not second.ok and "stalled" in second.error, second.error)
    prober.args.stall_seconds = 600

    print("\nproof of control")
    verifier = mod.ControlVerifier(session, args)
    check("matching well-known proof verifies", await verifier.verify(endpoint(mod.KIND_EVM_HTTP, base)) is True)
    wrong = endpoint(mod.KIND_EVM_HTTP, base, owner="0x9999999999999999999999999999999999999999")
    check("a different owner does not verify", await verifier.verify(wrong) is False)

    print("\nstatistics")
    now = 1_700_000_000
    rows = [(now - 100 + i, 1, 0, 100.0 + i, 10, 500 + i, "") for i in range(90)]
    rows += [(now - 10 + i, 0, 0, 0.0, 0, None, "boom") for i in range(10)]
    rows += [(now, 0, 1, 0.0, 0, None, "429") for _ in range(50)]  # throttled
    stats = mod.summarize(rows)
    check("throttled probes leave the denominator", stats["counted"] == 100, str(stats["counted"]))
    check("uptime computed on counted probes", stats["uptime_bps"] == 9000, str(stats["uptime_bps"]))
    check("throttle count reported separately", stats["throttled"] == 50, str(stats["throttled"]))
    check("last error surfaced", stats["last_error"] == "429")
    check("empty window yields no verdict", mod.summarize([])["uptime_bps"] is None)

    buckets = mod.hourly_buckets(rows, now)
    check("24 hourly buckets are published", len(buckets) == 24)
    check("the final bucket holds the recent probes", buckets[-1]["samples"] == 100, str(buckets[-1]["samples"]))
    check("empty hours are present but unscored", buckets[0]["uptime_bps"] is None)

    print("\nsigned snapshot")
    account = Account.create()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "status.json"
        payload = {"schema": mod.SCHEMA, "generated_at": now, "endpoints": []}
        mod.publish(path, payload, account)
        raw = path.read_bytes()
        document = json.loads(path.with_suffix(".json.sig").read_text())
        recovered = Account.recover_message(encode_defunct(primitive=raw), signature=document["signature"])
        check("signature recovers the checker address", recovered == account.address)
        check("signature document names the signer", document["address"] == account.address)

        tampered = raw.replace(b'"endpoints":[]', b'"endpoints":[1]')
        recovered = Account.recover_message(encode_defunct(primitive=tampered), signature=document["signature"])
        check("a tampered snapshot recovers a different address", recovered != account.address)

        mod.publish(path, payload, None)
        check("read-only checkers publish no signature", not path.with_suffix(".json.sig").exists())

    print("\nattestation gating")
    store_path = Path(tempfile.mkdtemp()) / "test.sqlite3"
    store = mod.Store(str(store_path))
    gate_args = make_args(min_samples=1000, min_span_seconds=20 * 3600)
    gate = mod.Checker.__new__(mod.Checker)
    gate.args = gate_args
    gate.store = store
    gate.chain = Namespace(account=object())

    thin = {"uptime_bps": 5000, "counted": 100, "first_checked": now - 600, "last_checked": now,
            "latency_p95_ms": 10, "error_bps": 5000}
    check("a thin sample is not attested", gate._metrics_attestation(endpoint(0, "https://x.example"), thin, 9900, 1) is False)

    short_span = dict(thin, counted=5000, first_checked=now - 3600)
    check("a short measurement span is not attested",
          gate._metrics_attestation(endpoint(0, "https://x.example"), short_span, 9900, 1) is False)

    good = dict(thin, counted=5000, first_checked=now - 22 * 3600)
    check("a well-covered adverse day is attested",
          gate._metrics_attestation(endpoint(0, "https://x.example"), good, 9900, 1) is True)

    healthy = dict(good, uptime_bps=9990)
    check("a healthy unflagged endpoint is never attested",
          gate._metrics_attestation(endpoint(0, "https://x.example"), healthy, 9900, 1) is False)
    check("a healthy flagged endpoint is attested as recovery evidence",
          gate._metrics_attestation(endpoint(0, "https://x.example", unreliable=True), healthy, 9900, 1) is True)

    store.mark_attested([(1, 1, 1, "metrics")], "0xdead")
    check("the same day is not attested twice",
          gate._metrics_attestation(endpoint(0, "https://x.example"), good, 9900, 1) is False)
    check("a new epoch resets the daily gate",
          gate._metrics_attestation(endpoint(0, "https://x.example", epoch=2), good, 9900, 1) is True)
    store.close()

    await session.close()
    await runner.cleanup()


def main() -> int:
    asyncio.run(run())
    print(f"\n{'-' * 62}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for name, detail in FAILED:
        print(f"  FAILED: {name} — {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
