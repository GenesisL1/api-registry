#!/usr/bin/env python3
"""GenesisL1 public API checker.

One process probes every registered endpoint, keeps detailed history in local
SQLite, publishes a signed telemetry snapshot, optionally serves the static web
directory, and anchors compact daily attestations on-chain.

Design rules this file follows, and the reasons for them:

  * Nothing the registry contains is trusted. A registered URL is attacker
    controlled input: it is re-validated here, resolved under an allowlist, and
    never followed through a redirect.
  * Reads are bounded. A probe that would pull an unbounded body is aborted.
  * A measurement is attributed to the endpoint epoch it was taken in. If the
    operator changes the endpoint mid-flight, the measurement is discarded
    rather than applied to a record it does not describe.
  * The published snapshot is signed. A consumer that cannot verify the
    signature against an on-chain authorized checker should discard the feed.
  * Nothing is marked submitted until the transaction is mined successfully.

Usage:
    export CHECKER_PRIVATE_KEY=0x...        # only if this checker attests
    python checker.py --registry 0x... --lens 0x... --serve
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import random
import re
import signal
import socket
import sqlite3
import ssl
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3
from web3.exceptions import ContractLogicError

VERSION = "3.0.0"
SCHEMA = "genesisl1-api-telemetry/v3"
ROOT = Path(__file__).resolve().parents[1]

KIND_NAMES = [
    "EVM RPC",
    "EVM WebSocket",
    "Cosmos LCD",
    "CometBFT RPC",
    "Cosmos gRPC",
    "Cosmos gRPC-Web",
    "Indexer API",
    "Snapshot service",
    "Other API",
]
KIND_EVM_HTTP, KIND_EVM_WS, KIND_LCD, KIND_COMET, KIND_GRPC, KIND_GRPC_WEB, KIND_INDEXER, KIND_SNAPSHOT, KIND_OTHER = range(9)

HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
HTTP2_EMPTY_SETTINGS = b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"
WELL_KNOWN_PATH = "/.well-known/genesisl1-registry.txt"
DNS_LABEL = "_genesisl1"
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")

log = logging.getLogger("checker")


# --------------------------------------------------------------------- models

@dataclass(frozen=True)
class Endpoint:
    id: int
    instance_id: int
    kind: int
    url: str
    epoch: int
    added_at: int
    updated_at: int
    unreliable: bool
    verified: bool
    adverse_days: int
    owner: str

    @property
    def kind_name(self) -> str:
        return KIND_NAMES[self.kind] if self.kind < len(KIND_NAMES) else "Unknown"


@dataclass
class ProbeResult:
    ts: int
    ok: bool
    latency_ms: float
    bytes_read: int = 0
    height: int | None = None
    throttled: bool = False
    error: str = ""


@dataclass
class Progress:
    height: int | None = None
    since: float = field(default_factory=time.time)


# ------------------------------------------------------------------- security

class GuardedResolver(AbstractResolver):
    """Rejects every address that resolves into a non-public range.

    The check happens inside the connector, on the address the socket will
    actually be opened to. Validating a hostname before handing it to a
    different resolver leaves a rebinding window; this closes it.
    """

    def __init__(self, allow_private: bool) -> None:
        self._allow_private = allow_private
        self._inner = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        hosts = await self._inner.resolve(host, port, family)
        if self._allow_private:
            return hosts
        allowed = [entry for entry in hosts if is_public_address(entry["host"])]
        if not allowed:
            raise OSError(f"blocked target: {host} resolves outside the public address space")
        return allowed

    async def close(self) -> None:
        await self._inner.close()


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_private or address.is_loopback or address.is_link_local:
        return False
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    # IPv4-mapped and 6to4 addresses can smuggle a private v4 target through v6.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped and not is_public_address(str(address.ipv4_mapped)):
            return False
        if address.sixtofour and not is_public_address(str(address.sixtofour)):
            return False
    return True


def validate_url(url: str, allow_private: bool = False) -> tuple[str, str, int]:
    """Re-validate a registered URL locally. The contract enforces the same
    shape, but a checker must not depend on the contract having done so.

    `allow_private` must match the resolver's setting, or the two layers
    disagree about what this checker is permitted to reach.
    """
    if len(url) > 256 or any(char < "\x21" or char > "\x7e" for char in url):
        raise ValueError("malformed url")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "ws", "wss", "grpc", "grpcs"):
        raise ValueError(f"unsupported scheme {parts.scheme!r}")
    if "@" in parts.netloc:
        raise ValueError("credentials in url")
    host = parts.hostname
    if not host:
        raise ValueError("missing host")
    default = 443 if parts.scheme in ("https", "wss", "grpcs") else 80
    try:
        port = parts.port or default
    except ValueError as exc:
        raise ValueError("invalid port") from exc
    # A literal address never reaches the resolver, so check it here too.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not allow_private and not is_public_address(host):
            raise ValueError(f"blocked literal target {host}")
    return parts.scheme, host, port


async def read_capped(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read a response body with a hard ceiling, aborting the transfer rather
    than buffering an unbounded reply into memory."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            response.close()
            raise ValueError(f"response exceeded {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------- probes

class Prober:
    def __init__(self, session: aiohttp.ClientSession, args: argparse.Namespace) -> None:
        self.session = session
        self.args = args
        self.progress: dict[int, Progress] = {}

    async def json_rpc(self, url: str, method: str, params: list[Any]) -> tuple[Any, int]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self.session.post(url, json=payload, allow_redirects=False) as response:
            self._reject_redirect(response)
            raw = await read_capped(response, self.args.max_bytes)
            if response.status != 200:
                raise ThrottledError(response) if _is_throttle(response) else RuntimeError(f"HTTP {response.status}")
        data = json.loads(raw)
        if "error" in data:
            raise RuntimeError(f"{method}: {str(data['error'])[:120]}")
        if "result" not in data:
            raise RuntimeError(f"{method}: no result")
        return data["result"], len(raw)

    async def get_json(self, url: str) -> tuple[Any, int]:
        async with self.session.get(url, allow_redirects=False) as response:
            self._reject_redirect(response)
            raw = await read_capped(response, self.args.max_bytes)
            if response.status != 200:
                raise ThrottledError(response) if _is_throttle(response) else RuntimeError(f"HTTP {response.status}")
        return json.loads(raw), len(raw)

    @staticmethod
    def _reject_redirect(response: aiohttp.ClientResponse) -> None:
        # A public API that redirects is not a usable API, and following the
        # redirect would hand an attacker a request to a target of their choice.
        if 300 <= response.status < 400:
            raise RuntimeError(f"HTTP {response.status} redirect to an unvalidated target")

    async def probe(self, endpoint: Endpoint) -> ProbeResult:
        started = time.perf_counter()
        ts = int(time.time())
        try:
            scheme, host, port = validate_url(endpoint.url, self.args.allow_private)
            base = endpoint.url.rstrip("/")
            total = 0
            height: int | None = None

            if endpoint.kind == KIND_EVM_HTTP:
                height, total = await self._probe_evm_http(base)
            elif endpoint.kind == KIND_EVM_WS:
                height, total = await self._probe_evm_ws(base)
            elif endpoint.kind == KIND_LCD:
                height, total = await self._probe_lcd(base)
            elif endpoint.kind == KIND_COMET:
                height, total = await self._probe_comet(base)
            elif endpoint.kind in (KIND_GRPC, KIND_GRPC_WEB):
                await self._probe_grpc(scheme, host, port)
            elif endpoint.kind == KIND_SNAPSHOT:
                total = await self._probe_download(base)
            else:
                _, total = await self.get_json(base)

            latency = (time.perf_counter() - started) * 1000
            stall = self._check_progress(endpoint, height, ts)
            if stall:
                return ProbeResult(ts, False, latency, total, height, error=stall)
            return ProbeResult(ts, True, latency, total, height)
        except ThrottledError as exc:
            # Rate limiting is the operator's capacity signal, not an outage. It
            # is recorded and shown, but excluded from the reliability verdict.
            return ProbeResult(ts, False, (time.perf_counter() - started) * 1000, throttled=True, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a probe failure is data, not a crash
            latency = (time.perf_counter() - started) * 1000
            return ProbeResult(ts, False, latency, error=f"{type(exc).__name__}: {str(exc)[:160]}")

    async def _probe_evm_http(self, base: str) -> tuple[int | None, int]:
        total = 0
        chain_id, size = await self.json_rpc(base, "eth_chainId", [])
        total += size
        if int(chain_id, 16) != self.args.chain_id:
            raise RuntimeError(f"wrong chain: reports {int(chain_id, 16)}, expected {self.args.chain_id}")
        block, size = await self.json_rpc(base, "eth_blockNumber", [])
        total += size
        height = int(block, 16)
        _, size = await self.json_rpc(base, "net_version", [])
        return height, total + size

    async def _probe_evm_ws(self, base: str) -> tuple[int | None, int]:
        # A TCP connect proves only that something is listening. Complete the
        # WebSocket handshake and make a real call over it.
        async with self.session.ws_connect(base, timeout=self.args.timeout, max_msg_size=self.args.max_bytes) as socket_:
            await socket_.send_json({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
            message = await asyncio.wait_for(socket_.receive(), timeout=self.args.timeout)
            if message.type is not aiohttp.WSMsgType.TEXT:
                raise RuntimeError(f"unexpected websocket frame {message.type.name}")
            data = json.loads(message.data)
            if "result" not in data:
                raise RuntimeError("no eth_chainId result over websocket")
            if int(data["result"], 16) != self.args.chain_id:
                raise RuntimeError(f"wrong chain: reports {int(data['result'], 16)}")
            await socket_.send_json({"jsonrpc": "2.0", "id": 2, "method": "eth_blockNumber", "params": []})
            message = await asyncio.wait_for(socket_.receive(), timeout=self.args.timeout)
            height = int(json.loads(message.data)["result"], 16)
            return height, len(message.data)

    async def _probe_lcd(self, base: str) -> tuple[int | None, int]:
        total = 0
        info, size = await self.get_json(base + "/cosmos/base/tendermint/v1beta1/node_info")
        total += size
        network = info.get("default_node_info", {}).get("network") or info.get("node_info", {}).get("network")
        if self.args.cosmos_chain_id and network and network != self.args.cosmos_chain_id:
            raise RuntimeError(f"wrong network: reports {network!r}")
        block, size = await self.get_json(base + "/cosmos/base/tendermint/v1beta1/blocks/latest")
        total += size
        height = int(block.get("block", {}).get("header", {}).get("height", 0) or 0)
        # One cheap paginated read proves the store is queryable without asking
        # the node for a full validator set every minute.
        _, size = await self.get_json(base + "/cosmos/staking/v1beta1/validators?pagination.limit=1")
        return height, total + size

    async def _probe_comet(self, base: str) -> tuple[int | None, int]:
        total = 0
        status, size = await self.get_json(base + "/status")
        total += size
        sync = status.get("result", status).get("sync_info", {})
        if sync.get("catching_up") is True:
            raise RuntimeError("node is catching up")
        height = int(sync.get("latest_block_height", 0) or 0)
        _, size = await self.get_json(base + "/abci_info")
        return height, total + size

    async def _probe_grpc(self, scheme: str, host: str, port: int) -> None:
        # Exchange the HTTP/2 connection preface. A plain TCP accept proves
        # nothing; a SETTINGS frame in reply proves an HTTP/2 server is there.
        context = ssl.create_default_context() if scheme in ("grpcs", "https", "wss") else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context), timeout=self.args.timeout
        )
        try:
            writer.write(HTTP2_PREFACE + HTTP2_EMPTY_SETTINGS)
            await writer.drain()
            header = await asyncio.wait_for(reader.readexactly(9), timeout=self.args.timeout)
            frame_type = header[3]
            if frame_type != 0x04:
                raise RuntimeError(f"not an HTTP/2 server (first frame type {frame_type})")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _probe_download(self, base: str) -> int:
        # Snapshot services serve very large files. Ask for the first kilobyte.
        headers = {"Range": "bytes=0-1023"}
        async with self.session.get(base, headers=headers, allow_redirects=False) as response:
            self._reject_redirect(response)
            if response.status not in (200, 206):
                raise ThrottledError(response) if _is_throttle(response) else RuntimeError(f"HTTP {response.status}")
            chunk = await read_capped(response, 1 << 20)
        return len(chunk)

    def _check_progress(self, endpoint: Endpoint, height: int | None, ts: int) -> str:
        """A node that answers instantly but stopped importing blocks is worse
        than one that is down, because it silently serves stale state."""
        if height is None or height <= 0:
            return ""
        state = self.progress.setdefault(endpoint.id, Progress())
        if state.height is None or height > state.height:
            state.height = height
            state.since = ts
            return ""
        stalled_for = ts - state.since
        if stalled_for >= self.args.stall_seconds:
            return f"height stalled at {height} for {int(stalled_for)}s"
        return ""


class ThrottledError(Exception):
    def __init__(self, response: aiohttp.ClientResponse) -> None:
        retry = response.headers.get("Retry-After", "")
        super().__init__(f"HTTP {response.status} rate limited{f' (retry after {retry})' if retry else ''}")


def _is_throttle(response: aiohttp.ClientResponse) -> bool:
    return response.status in (429, 503) or response.status == 509


# ---------------------------------------------------------------- persistence

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS probes (
    endpoint_id INTEGER NOT NULL,
    epoch       INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    throttled   INTEGER NOT NULL DEFAULT 0,
    latency     REAL,
    bytes       INTEGER DEFAULT 0,
    height      INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS probe_window ON probes(endpoint_id, epoch, ts);
CREATE TABLE IF NOT EXISTS attested (
    endpoint_id INTEGER NOT NULL,
    epoch       INTEGER NOT NULL,
    day         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    tx          TEXT,
    PRIMARY KEY (endpoint_id, epoch, day, kind)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA_SQL)
        self.db.commit()
        self._lock = threading.Lock()

    def record(self, endpoint: Endpoint, result: ProbeResult) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO probes(endpoint_id,epoch,ts,ok,throttled,latency,bytes,height,error)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    endpoint.id,
                    endpoint.epoch,
                    result.ts,
                    int(result.ok),
                    int(result.throttled),
                    result.latency_ms,
                    result.bytes_read,
                    result.height,
                    result.error[:200],
                ),
            )
            self.db.commit()

    def window(self, endpoint: Endpoint, since: int) -> list[tuple]:
        with self._lock:
            return self.db.execute(
                "SELECT ts,ok,throttled,latency,bytes,height,error FROM probes"
                " WHERE endpoint_id=? AND epoch=? AND ts>=? ORDER BY ts",
                (endpoint.id, endpoint.epoch, since),
            ).fetchall()

    def already_attested(self, endpoint: Endpoint, day: int, kind: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT 1 FROM attested WHERE endpoint_id=? AND epoch=? AND day=? AND kind=?",
                (endpoint.id, endpoint.epoch, day, kind),
            ).fetchone()
        return row is not None

    def mark_attested(self, records: Iterable[tuple[int, int, int, str]], tx: str) -> None:
        with self._lock:
            self.db.executemany(
                "INSERT OR IGNORE INTO attested(endpoint_id,epoch,day,kind,tx) VALUES(?,?,?,?,?)",
                [(*record, tx) for record in records],
            )
            self.db.commit()

    def prune(self, days: int) -> None:
        cutoff = int(time.time()) - days * 86_400
        with self._lock:
            self.db.execute("DELETE FROM probes WHERE ts<?", (cutoff,))
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()


def summarize(rows: Sequence[tuple]) -> dict[str, Any]:
    """Uptime excludes throttled probes from the denominator. Being rate limited
    is a capacity signal from the operator, and counting it as downtime would
    let a busy checker fleet manufacture the outage it then reports."""
    if not rows:
        return {
            "samples": 0, "counted": 0, "throttled": 0, "uptime_bps": None, "error_bps": None,
            "latency_avg_ms": None, "latency_p50_ms": None, "latency_p95_ms": None, "latency_p99_ms": None,
            "first_checked": None, "last_checked": None, "last_success": None,
            "bytes_total": 0, "latest_height": None, "last_error": "",
        }
    counted = [row for row in rows if not row[2]]
    successes = [row for row in counted if row[1]]
    latencies = [row[3] for row in successes if row[3] is not None]
    denominator = len(counted) or 1
    failures = [row for row in counted if not row[1]]
    return {
        "samples": len(rows),
        "counted": len(counted),
        "throttled": len(rows) - len(counted),
        "uptime_bps": round(len(successes) * 10_000 / denominator) if counted else None,
        "error_bps": round(len(failures) * 10_000 / denominator) if counted else None,
        "latency_avg_ms": round(statistics.fmean(latencies), 2) if latencies else None,
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_p99_ms": percentile(latencies, 0.99),
        "first_checked": rows[0][0],
        "last_checked": rows[-1][0],
        "last_success": max((row[0] for row in successes), default=None),
        "bytes_total": sum(row[4] or 0 for row in rows),
        "latest_height": max((row[5] for row in rows if row[5] is not None), default=None),
        "last_error": next((row[6] for row in reversed(rows) if row[6]), ""),
    }


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[index], 2)


def hourly_buckets(rows: Sequence[tuple], now: int, hours: int = 24) -> list[dict[str, Any]]:
    """Per-hour uptime for the last day. The published series the directory
    renders as a strip, so a reader sees when an endpoint failed, not just how
    often."""
    start = (now // 3600 - hours + 1) * 3600
    buckets: list[dict[str, Any]] = [
        {"t": start + index * 3600, "samples": 0, "uptime_bps": None, "latency_p95_ms": None}
        for index in range(hours)
    ]
    grouped: dict[int, list[tuple]] = {}
    for row in rows:
        index = (row[0] - start) // 3600
        if 0 <= index < hours:
            grouped.setdefault(index, []).append(row)
    for index, group in grouped.items():
        counted = [row for row in group if not row[2]]
        if not counted:
            buckets[index]["samples"] = len(group)
            continue
        successes = [row for row in counted if row[1]]
        latencies = [row[3] for row in successes if row[3] is not None]
        buckets[index] = {
            "t": start + index * 3600,
            "samples": len(counted),
            "uptime_bps": round(len(successes) * 10_000 / len(counted)),
            "latency_p95_ms": percentile(latencies, 0.95),
        }
    return buckets


# ------------------------------------------------------------ proof of control

class ControlVerifier:
    """Confirms the instance owner controls the endpoint's host.

    Without this, anyone can list somebody else's infrastructure under their own
    name. Either proof is sufficient:

        DNS   _genesisl1.<host>  TXT  "gl1-owner=0x<owner>"
        HTTP  <origin>/.well-known/genesisl1-registry.txt  containing 0x<owner>
    """

    def __init__(self, session: aiohttp.ClientSession, args: argparse.Namespace) -> None:
        self.session = session
        self.args = args
        self._resolver = None
        if not args.no_dns_verification:
            try:
                import dns.asyncresolver  # noqa: PLC0415

                self._resolver = dns.asyncresolver.Resolver()
                self._resolver.lifetime = args.timeout
            except ImportError:
                log.warning("dnspython is not installed; falling back to HTTP proof of control only")

    async def verify(self, endpoint: Endpoint) -> bool:
        try:
            scheme, host, port = validate_url(endpoint.url, self.args.allow_private)
        except ValueError:
            return False
        owner = endpoint.owner.lower()
        if await self._verify_dns(host, owner):
            return True
        if scheme in ("http", "https"):
            return await self._verify_well_known(scheme, host, port, owner)
        return False

    async def _verify_dns(self, host: str, owner: str) -> bool:
        if self._resolver is None:
            return False
        try:
            answer = await self._resolver.resolve(f"{DNS_LABEL}.{host}", "TXT")
        except Exception:  # noqa: BLE001 - absence of a record is the common case
            return False
        for record in answer:
            text = b"".join(record.strings).decode("utf-8", "ignore").lower()
            if owner in text:
                return True
        return False

    async def _verify_well_known(self, scheme: str, host: str, port: int, owner: str) -> bool:
        default = 443 if scheme == "https" else 80
        netloc = host if port == default else f"{host}:{port}"
        url = f"{scheme}://{netloc}{WELL_KNOWN_PATH}"
        try:
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    return False
                body = await read_capped(response, 4096)
        except Exception:  # noqa: BLE001
            return False
        found = ADDRESS_RE.findall(body.decode("utf-8", "ignore"))
        return any(match.lower() == owner for match in found)


# -------------------------------------------------------------- chain access

class Chain:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": args.timeout}))
        abi_dir = Path(args.abi_dir)
        self.registry = self.w3.eth.contract(
            address=Web3.to_checksum_address(args.registry),
            abi=json.loads((abi_dir / "GenesisL1APIRegistry.json").read_text()),
        )
        self.lens = self.w3.eth.contract(
            address=Web3.to_checksum_address(args.lens),
            abi=json.loads((abi_dir / "GenesisL1RegistryLens.json").read_text()),
        )
        self.account = Account.from_key(args.private_key) if args.private_key else None
        self._revision: int | None = None
        self._cache: list[Endpoint] = []

    @property
    def address(self) -> str | None:
        return self.account.address if self.account else None

    def threshold_bps(self) -> int:
        return int(self.registry.functions.reliabilityThresholdBps().call())

    def endpoints(self) -> list[Endpoint]:
        """Re-read the registry only when its revision counter moved. One word
        of state replaces a full paginated re-read on every cycle."""
        revision = int(self.registry.functions.revision().call())
        if revision == self._revision and self._cache:
            return self._cache

        collected: list[Endpoint] = []
        cursor = 0
        while True:
            endpoints, owners, next_cursor, at_revision = self.lens.functions.getAllEndpoints(
                cursor, self.args.page_size
            ).call()
            for record, owner in zip(endpoints, owners):
                if not record[8]:
                    continue
                collected.append(
                    Endpoint(
                        id=int(record[0]), instance_id=int(record[1]), added_at=int(record[2]),
                        updated_at=int(record[3]), epoch=int(record[4]), adverse_days=int(record[5]),
                        kind=int(record[7]), unreliable=bool(record[9]), verified=bool(record[10]),
                        url=record[12], owner=owner,
                    )
                )
            if next_cursor <= cursor or not endpoints and next_cursor == cursor:
                break
            cursor = next_cursor
            if at_revision != revision:  # a write landed mid-read; start over
                return self.endpoints()
            if cursor >= self.args.max_instances:
                log.warning("stopped reading the registry at %d instances", cursor)
                break

        self._revision = revision
        self._cache = collected
        log.info("registry revision %d: %d active endpoints", revision, len(collected))
        return collected

    def attest(self, items: list[tuple]) -> str:
        """Submit and wait for the receipt. A transaction hash means accepted
        into a mempool, not mined, and certainly not successful."""
        if not self.account:
            raise RuntimeError("no checker key configured")
        function = self.registry.functions.attest(items)
        try:
            gas = function.estimate_gas({"from": self.account.address})
        except ContractLogicError as exc:
            raise RuntimeError(f"attestation would revert: {exc}") from exc
        gas = min(int(gas * 1.25), self.args.max_gas)

        tx: dict[str, Any] = {
            "from": self.account.address,
            "nonce": self.w3.eth.get_transaction_count(self.account.address, "pending"),
            "chainId": self.w3.eth.chain_id,
            "gas": gas,
        }
        latest = self.w3.eth.get_block("latest")
        if latest.get("baseFeePerGas") is not None:
            tip = self.w3.eth.max_priority_fee
            tx["maxPriorityFeePerGas"] = tip
            tx["maxFeePerGas"] = latest["baseFeePerGas"] * 2 + tip
        else:
            tx["gasPrice"] = self.w3.eth.gas_price

        signed = self.account.sign_transaction(function.build_transaction(tx))
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.args.tx_timeout)
        if receipt.status != 1:
            raise RuntimeError(f"attestation reverted on chain ({tx_hash.hex()})")
        return tx_hash.hex()


# ------------------------------------------------------------------ publishing

def publish(path: Path, payload: dict[str, Any], account: Account | None) -> None:
    """Write the snapshot and its detached signature atomically.

    The signature covers the exact bytes of status.json. A detached file avoids
    every canonicalization trap that comes with signing a document that has to
    contain its own signature.
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(body)
    temp.replace(path)

    signature_path = path.with_suffix(path.suffix + ".sig")
    if account is None:
        signature_path.unlink(missing_ok=True)
        return
    signature = Account.sign_message(encode_defunct(primitive=body), private_key=account.key).signature.hex()
    document = {
        "address": account.address,
        "signature": signature if signature.startswith("0x") else "0x" + signature,
        "signed_at": payload["generated_at"],
        "algorithm": "eip191-personal-sign/status.json-bytes",
    }
    temp = signature_path.with_suffix(signature_path.suffix + ".tmp")
    temp.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    temp.replace(signature_path)


# ---------------------------------------------------------------- static serve

def serve(directory: Path, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        server_version = f"GenesisL1Checker/{VERSION}"
        sys_version = ""

        def __init__(self, *args_: Any, **kwargs: Any) -> None:
            super().__init__(*args_, directory=str(directory), **kwargs)

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
                "img-src 'self' data:; connect-src *; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            super().end_headers()

        def list_directory(self, path: str):  # no directory listings
            self.send_error(404, "Not found")
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, fmt: str, *args_: Any) -> None:
            log.debug("http %s", fmt % args_)

    httpd = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="static-server").start()
    log.info("serving %s on http://%s:%d/", directory, host, port)
    return httpd


# -------------------------------------------------------------------- the loop

class Checker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.store = Store(args.database)
        self.chain = Chain(args)
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=self.args.concurrency * 2,
            resolver=GuardedResolver(self.args.allow_private),
            ttl_dns_cache=30,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=self.args.timeout)
        headers = {"User-Agent": f"GenesisL1-API-Checker/{VERSION}", "Accept": "application/json"}
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
            prober = Prober(session, self.args)
            verifier = ControlVerifier(session, self.args)
            while not self.stopping.is_set():
                started = time.monotonic()
                try:
                    await self.cycle(prober, verifier)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("cycle failed")
                elapsed = time.monotonic() - started
                delay = max(1.0, self.args.interval - elapsed) + random.uniform(0, self.args.jitter)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=delay)

    async def cycle(self, prober: Prober, verifier: ControlVerifier) -> None:
        endpoints = await asyncio.to_thread(self.chain.endpoints)
        threshold = await asyncio.to_thread(self.chain.threshold_bps)
        semaphore = asyncio.Semaphore(self.args.concurrency)

        async def measure(endpoint: Endpoint) -> None:
            async with semaphore:
                for index in range(self.args.burst):
                    if index:
                        await asyncio.sleep(random.uniform(0.2, 1.0))
                    result = await prober.probe(endpoint)
                    await asyncio.to_thread(self.store.record, endpoint, result)

        # A flagged endpoint is still probed, at a reduced rate. Without this it
        # can never accumulate the evidence needed to clear its own flag.
        due = [
            endpoint for endpoint in endpoints
            if not endpoint.unreliable or int(time.time()) // 60 % self.args.unreliable_divisor == 0
        ]
        await asyncio.gather(*(measure(endpoint) for endpoint in due))

        now = int(time.time())
        day = now // 86_400
        snapshot: list[dict[str, Any]] = []
        attestations: list[tuple] = []
        records: list[tuple[int, int, int, str]] = []

        for endpoint in endpoints:
            rows_24h = self.store.window(endpoint, now - 86_400)
            stats_24h = summarize(rows_24h)
            entry = {
                "id": endpoint.id,
                "instance_id": endpoint.instance_id,
                "epoch": endpoint.epoch,
                "kind": endpoint.kind,
                "kind_name": endpoint.kind_name,
                "url": endpoint.url,
                "unreliable": endpoint.unreliable,
                "verified": endpoint.verified,
                "adverse_days": endpoint.adverse_days,
                "stats_1h": summarize(self.store.window(endpoint, now - 3_600)),
                "stats_24h": stats_24h,
                "stats_30d": summarize(self.store.window(endpoint, now - 30 * 86_400)),
                "buckets_24h": hourly_buckets(rows_24h, now),
            }
            snapshot.append(entry)

            metrics = self._metrics_attestation(endpoint, stats_24h, threshold, day)
            control = await self._control_attestation(endpoint, verifier, day)
            if metrics or control:
                attestations.append(
                    (
                        endpoint.id,
                        endpoint.epoch,
                        stats_24h["uptime_bps"] or 0,
                        int(stats_24h["latency_p95_ms"] or 0),
                        stats_24h["error_bps"] or 0,
                        metrics,
                        control,
                    )
                )
                if metrics:
                    records.append((endpoint.id, endpoint.epoch, day, "metrics"))
                if control:
                    records.append((endpoint.id, endpoint.epoch, day, "control"))

        payload = {
            "schema": SCHEMA,
            "generated_at": now,
            "checker": {
                "address": self.chain.address,
                "name": self.args.name,
                "version": VERSION,
            },
            "registry": Web3.to_checksum_address(self.args.registry),
            "lens": Web3.to_checksum_address(self.args.lens),
            "chain_id": self.args.chain_id,
            "reliability_threshold_bps": threshold,
            "window": {
                "interval_seconds": self.args.interval,
                "probe_burst": self.args.burst,
                "retention_days": self.args.retention_days,
            },
            "endpoints": sorted(snapshot, key=lambda item: item["id"]),
        }
        await asyncio.to_thread(publish, Path(self.args.output), payload, self.chain.account)

        if attestations:
            await self._submit(attestations, records)
        await asyncio.to_thread(self.store.prune, self.args.retention_days)
        log.info(
            "cycle complete: %d endpoints probed, %d in snapshot, %d attestations pending",
            len(due), len(snapshot), len(attestations),
        )

    def _metrics_attestation(self, endpoint: Endpoint, stats: dict[str, Any], threshold: int, day: int) -> bool:
        if not self.chain.account:
            return False
        if stats["uptime_bps"] is None:
            return False
        # Require real coverage before making a claim about a whole day.
        if stats["counted"] < self.args.min_samples:
            return False
        span = (stats["last_checked"] or 0) - (stats["first_checked"] or 0)
        if span < self.args.min_span_seconds:
            return False
        adverse = stats["uptime_bps"] < threshold
        if not adverse and not endpoint.unreliable:
            return False  # healthy and unflagged: nothing worth a storage write
        if self.store.already_attested(endpoint, day, "metrics"):
            return False
        return True

    async def _control_attestation(self, endpoint: Endpoint, verifier: ControlVerifier, day: int) -> bool:
        if not self.chain.account or endpoint.verified:
            return False
        if self.store.already_attested(endpoint, endpoint.epoch, "control"):
            return False
        return await verifier.verify(endpoint)

    async def _submit(self, attestations: list[tuple], records: list[tuple[int, int, int, str]]) -> None:
        # Near a UTC boundary the chain may land the transaction on the next day
        # from the one the measurement describes. Wait it out.
        seconds_into_day = int(time.time()) % 86_400
        if seconds_into_day > 86_400 - self.args.day_edge_seconds or seconds_into_day < self.args.day_edge_seconds:
            log.info("holding %d attestations away from the UTC day boundary", len(attestations))
            return
        for start in range(0, len(attestations), self.args.batch_size):
            batch = attestations[start : start + self.args.batch_size]
            batch_records = records[start : start + self.args.batch_size]
            try:
                tx = await asyncio.to_thread(self.chain.attest, batch)
            except Exception as exc:  # noqa: BLE001
                log.error("attestation batch failed, will retry next cycle: %s", exc)
                return
            await asyncio.to_thread(self.store.mark_attested, batch_records, tx)
            log.info("attested %d endpoints in %s", len(batch), tx)

    def stop(self) -> None:
        self.stopping.set()


# ------------------------------------------------------------------------ main

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GenesisL1 public API checker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    chain = parser.add_argument_group("chain")
    chain.add_argument("--rpc", default=os.getenv("RPC_URL", "https://rpc.genesisl1.org"))
    chain.add_argument("--registry", default=os.getenv("REGISTRY_ADDRESS"), help="registry contract address")
    chain.add_argument("--lens", default=os.getenv("LENS_ADDRESS"), help="lens contract address")
    chain.add_argument("--chain-id", type=int, default=int(os.getenv("CHAIN_ID", "29")))
    chain.add_argument("--cosmos-chain-id", default=os.getenv("COSMOS_CHAIN_ID", ""))
    chain.add_argument("--abi-dir", default=str(ROOT / "contract" / "abi"))

    probe = parser.add_argument_group("probing")
    probe.add_argument("--interval", type=int, default=60, help="seconds between cycles")
    probe.add_argument("--jitter", type=float, default=20.0, help="random delay added per cycle")
    probe.add_argument("--burst", type=int, default=2, help="probes per endpoint per cycle")
    probe.add_argument("--concurrency", type=int, default=24)
    probe.add_argument("--timeout", type=float, default=12.0)
    probe.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024, help="hard cap on any single response")
    probe.add_argument("--stall-seconds", type=int, default=600, help="height must advance within this window")
    probe.add_argument("--unreliable-divisor", type=int, default=10, help="probe flagged endpoints 1 cycle in N")
    probe.add_argument("--allow-private", action="store_true", help="DANGEROUS: permit private network targets")
    probe.add_argument("--no-dns-verification", action="store_true")

    report = parser.add_argument_group("attestation")
    report.add_argument("--min-samples", type=int, default=1000, help="counted probes needed before a daily claim")
    report.add_argument("--min-span-seconds", type=int, default=20 * 3600, help="measurement span needed")
    report.add_argument("--day-edge-seconds", type=int, default=600, help="quiet period around the UTC boundary")
    report.add_argument("--batch-size", type=int, default=40)
    report.add_argument("--max-gas", type=int, default=8_000_000)
    report.add_argument("--tx-timeout", type=int, default=300)
    report.add_argument("--page-size", type=int, default=25)
    report.add_argument("--max-instances", type=int, default=5_000)

    local = parser.add_argument_group("local")
    local.add_argument("--name", default=os.getenv("CHECKER_NAME", "unnamed checker"))
    local.add_argument("--database", default=os.getenv("DATABASE_PATH", str(ROOT / "bot" / "checker.sqlite3")))
    local.add_argument("--output", default=str(ROOT / "web" / "data" / "status.json"))
    local.add_argument("--web-dir", default=str(ROOT / "web"))
    local.add_argument("--retention-days", type=int, default=35)
    local.add_argument("--serve", action="store_true")
    local.add_argument("--host", default="127.0.0.1", help="bind address; keep local and use a reverse proxy")
    local.add_argument("--port", type=int, default=8787)
    local.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))

    args = parser.parse_args(argv)
    # The key is read from the environment only. A command line argument would
    # appear in `ps`, in shell history, and in the logged systemd ExecStart.
    args.private_key = os.getenv("CHECKER_PRIVATE_KEY", "").strip()

    missing = [name for name in ("registry", "lens") if not getattr(args, name)]
    if missing:
        parser.error(f"missing required address: {', '.join('--' + name for name in missing)}")
    args.interval = max(30, args.interval)
    args.burst = max(1, min(12, args.burst))
    args.unreliable_divisor = max(1, args.unreliable_divisor)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    if args.allow_private:
        log.warning("--allow-private is set: this checker will probe private network targets")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    checker = Checker(args)
    if checker.chain.account:
        log.info("attesting as %s", checker.chain.address)
    else:
        log.info("read-only mode: no CHECKER_PRIVATE_KEY, nothing will be submitted on-chain")

    httpd = serve(Path(args.web_dir), args.host, args.port) if args.serve else None

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for signal_name in ("SIGINT", "SIGTERM"):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(getattr(signal, signal_name), checker.stop)
        await checker.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    finally:
        if httpd:
            httpd.shutdown()
        checker.store.close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
