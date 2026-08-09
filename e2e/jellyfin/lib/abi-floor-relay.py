#!/usr/bin/env python3
"""Bounded host-loopback TCP relay for the internal ABI-floor container."""

from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import select
import signal
import socket
import sys
import tempfile
import threading
import time
from typing import Any


IMPLEMENTATION_PATH = "e2e/jellyfin/lib/abi-floor-relay.py"
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
NETWORK_NAME = re.compile(r"^rk-jellyfin-[a-z0-9_-]+_abi-floor-internal$")
MAX_ACTIVE_CONNECTIONS = 32
MAX_BUFFERED_BYTES_PER_DIRECTION = 256 * 1024


def utc_now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def implementation_sha256() -> str:
    return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


def write_json_atomic(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"refusing unsafe relay receipt path: {path}")
    partial = path.with_name(path.name + ".part")
    if partial.exists() or partial.is_symlink():
        raise ValueError(f"refusing stale relay receipt partial: {partial}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(partial, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, path)


class LoopbackRelay:
    def __init__(
        self,
        *,
        bind_port: int,
        target_host: str,
        target_port: int,
        target_container_id: str,
        target_network: str,
        receipt_path: pathlib.Path,
    ) -> None:
        self.bind_host = "127.0.0.1"
        self.bind_port = bind_port
        self.target_host = target_host
        self.target_port = target_port
        self.target_container_id = target_container_id
        self.target_network = target_network
        self.receipt_path = receipt_path
        self.started_utc = utc_now()
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.workers: list[threading.Thread] = []
        self.lock = threading.Lock()
        self.active_connections = 0
        self.counters = {
            "connectionsAccepted": 0,
            "connectionsRejected": 0,
            "targetConnectionsSucceeded": 0,
            "targetConnectionsFailed": 0,
            "connectionsCompleted": 0,
            "bytesClientToTarget": 0,
            "bytesTargetToClient": 0,
            "peakBufferedBytes": 0,
        }

    def receipt(self, *, completed: bool) -> dict[str, Any]:
        with self.lock:
            counters = dict(self.counters)
        return {
            "schemaVersion": 1,
            "transport": "host-loopback-tcp-relay",
            "implementation": {
                "path": IMPLEMENTATION_PATH,
                "sha256": implementation_sha256(),
            },
            "limits": {
                "maxActiveConnections": MAX_ACTIVE_CONNECTIONS,
                "maxBufferedBytesPerDirection": MAX_BUFFERED_BYTES_PER_DIRECTION,
            },
            "processId": os.getpid(),
            "bind": {"host": self.bind_host, "port": self.bind_port},
            "target": {
                "host": self.target_host,
                "port": self.target_port,
                "containerId": self.target_container_id,
                "network": self.target_network,
            },
            "ready": True,
            "completed": completed,
            "startedUtc": self.started_utc,
            "finishedUtc": utc_now() if completed else None,
            "counters": counters,
        }

    def _increment(self, name: str, amount: int = 1) -> None:
        with self.lock:
            self.counters[name] += amount

    def _record_buffer_size(self, size: int) -> None:
        with self.lock:
            self.counters["peakBufferedBytes"] = max(
                self.counters["peakBufferedBytes"], size
            )

    def _complete_connection(self) -> None:
        with self.lock:
            self.active_connections -= 1
            self.counters["connectionsCompleted"] += 1

    def _relay_connection(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            try:
                upstream = socket.create_connection(
                    (self.target_host, self.target_port), timeout=5.0
                )
            except OSError:
                self._increment("targetConnectionsFailed")
                return
            self._increment("targetConnectionsSucceeded")
            client.setblocking(False)
            upstream.setblocking(False)
            peers = {client: upstream, upstream: client}
            counters = {
                upstream: "bytesClientToTarget",
                client: "bytesTargetToClient",
            }
            readable_peers = {client, upstream}
            pending = {client: bytearray(), upstream: bytearray()}
            shutdown_after_drain: set[socket.socket] = set()
            while (readable_peers or any(pending.values())) \
                    and not self.stop_event.is_set():
                readers = tuple(
                    source for source in readable_peers
                    if len(pending[peers[source]]) < MAX_BUFFERED_BYTES_PER_DIRECTION
                )
                writers = tuple(destination for destination, data in pending.items() if data)
                exceptional = tuple({*readable_peers, *writers})
                readable, writable, failed = select.select(
                    readers, writers, exceptional, 0.25
                )
                if failed:
                    break
                for source in readable:
                    destination = peers[source]
                    capacity = MAX_BUFFERED_BYTES_PER_DIRECTION - len(pending[destination])
                    try:
                        chunk = source.recv(min(64 * 1024, capacity))
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        readable_peers.discard(source)
                        if pending[destination]:
                            shutdown_after_drain.add(destination)
                        else:
                            try:
                                destination.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass
                        continue
                    pending[destination].extend(chunk)
                    self._record_buffer_size(len(pending[destination]))
                write_failed = False
                for destination in writable:
                    try:
                        sent = destination.send(pending[destination])
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        write_failed = True
                        break
                    if sent <= 0:
                        write_failed = True
                        break
                    del pending[destination][:sent]
                    self._increment(counters[destination], sent)
                    if not pending[destination] and destination in shutdown_after_drain:
                        shutdown_after_drain.remove(destination)
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                if write_failed:
                    break
        finally:
            try:
                client.close()
            finally:
                if upstream is not None:
                    upstream.close()
                self._complete_connection()

    def serve(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener = listener
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.bind_host, self.bind_port))
        self.bind_port = int(listener.getsockname()[1])
        listener.listen(64)
        listener.settimeout(0.25)
        write_json_atomic(self.receipt_path, self.receipt(completed=False))
        try:
            while not self.stop_event.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                self.workers = [worker for worker in self.workers if worker.is_alive()]
                with self.lock:
                    self.counters["connectionsAccepted"] += 1
                    admitted = self.active_connections < MAX_ACTIVE_CONNECTIONS
                    if admitted:
                        self.active_connections += 1
                    else:
                        self.counters["connectionsRejected"] += 1
                        self.counters["connectionsCompleted"] += 1
                if not admitted:
                    client.close()
                    continue
                worker = threading.Thread(
                    target=self._relay_connection,
                    args=(client,),
                    name="abi-floor-relay-connection",
                    daemon=True,
                )
                self.workers.append(worker)
                worker.start()
        finally:
            self.stop_event.set()
            listener.close()
            for worker in self.workers:
                worker.join(timeout=6.0)
            if any(worker.is_alive() for worker in self.workers):
                raise RuntimeError("relay workers did not stop cleanly")
            write_json_atomic(self.receipt_path, self.receipt(completed=True))

    def stop(self) -> None:
        self.stop_event.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass


def install_parent_death_signal() -> None:
    """Make an abruptly terminated shell unable to orphan the host listener."""
    if sys.platform != "linux":
        raise RuntimeError("the ABI-floor host relay requires Linux")
    parent = os.getppid()
    if parent <= 1:
        raise RuntimeError("refusing to start the ABI-floor relay without a live parent")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent:
        raise RuntimeError("ABI-floor relay parent exited during startup")


def validate_runtime_args(args: argparse.Namespace) -> None:
    if not 1024 <= args.bind_port <= 65535:
        raise ValueError("bind port must be 1024..65535")
    if args.target_port != 8096:
        raise ValueError("target port must be exactly 8096")
    address = ipaddress.ip_address(args.target_host)
    if not isinstance(address, ipaddress.IPv4Address) \
            or str(address) != args.target_host \
            or not address.is_private \
            or address.is_reserved:
        raise ValueError("target must be an inspected private IPv4 endpoint")
    if address.is_loopback or address.is_link_local or address.is_multicast \
            or address.is_unspecified:
        raise ValueError("target must be a routable private container endpoint")
    if CONTAINER_ID.fullmatch(args.target_container_id) is None:
        raise ValueError("target container ID is invalid")
    if NETWORK_NAME.fullmatch(args.target_network) is None:
        raise ValueError("target network identity is invalid")
    if args.receipt.exists() or args.receipt.is_symlink():
        raise ValueError("relay receipt path must be fresh")


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="rk-abi-floor-relay-") as temporary:
        root = pathlib.Path(temporary)
        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target_port = int(target.getsockname()[1])

        response_body = bytes(range(256)) * (4 * 1024 * 1024 // 256)

        def serve_target() -> None:
            connection, _ = target.accept()
            with connection:
                request_parts = []
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    request_parts.append(chunk)
                request = b"".join(request_parts)
                if request != b"relay-self-test":
                    raise AssertionError(f"unexpected relay self-test request: {request!r}")
                connection.sendall(response_body)
            target.close()

        target_thread = threading.Thread(target=serve_target, daemon=True)
        target_thread.start()
        receipt = root / "relay.json"
        relay = LoopbackRelay(
            bind_port=0,
            target_host="127.0.0.1",
            target_port=target_port,
            target_container_id="a" * 64,
            target_network="rk-jellyfin-fixture_abi-floor-internal",
            receipt_path=receipt,
        )
        relay_thread = threading.Thread(target=relay.serve, daemon=True)
        relay_thread.start()
        for _ in range(100):
            if receipt.is_file():
                break
            relay_thread.join(timeout=0.01)
        if not receipt.is_file() or not relay_thread.is_alive():
            raise AssertionError("relay did not become ready")
        with socket.create_connection(("127.0.0.1", relay.bind_port), timeout=2.0) as client:
            client.sendall(b"relay-self-test")
            client.shutdown(socket.SHUT_WR)
            # Let a multi-MiB response exceed kernel/relay buffers before the
            # client starts draining it. A nonblocking sendall implementation
            # truncates here; the writable-select pump must retain every byte.
            time.sleep(0.15)
            response_parts = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response_parts.append(chunk)
        response = b"".join(response_parts)
        if response != response_body:
            raise AssertionError(
                f"relay truncated slow-reader response: {len(response)} != {len(response_body)}"
            )
        relay.stop()
        relay_thread.join(timeout=8.0)
        target_thread.join(timeout=2.0)
        if relay_thread.is_alive() or target_thread.is_alive():
            raise AssertionError("relay self-test threads did not stop")
        final = json.loads(receipt.read_text(encoding="utf-8"))
        counters = final.get("counters", {})
        if final.get("completed") is not True \
                or counters.get("connectionsAccepted") != 1 \
                or counters.get("connectionsRejected") != 0 \
                or counters.get("targetConnectionsSucceeded") != 1 \
                or counters.get("targetConnectionsFailed") != 0 \
                or counters.get("connectionsCompleted") != 1 \
                or counters.get("bytesClientToTarget") != len(b"relay-self-test") \
                or counters.get("bytesTargetToClient") != len(response_body) \
                or not 0 < counters.get("peakBufferedBytes", 0) \
                <= MAX_BUFFERED_BYTES_PER_DIRECTION:
            raise AssertionError(f"relay self-test receipt differs: {final!r}")

        saturated_receipt = root / "saturated-relay.json"
        saturated = LoopbackRelay(
            bind_port=0,
            target_host="127.0.0.1",
            target_port=target_port,
            target_container_id="b" * 64,
            target_network="rk-jellyfin-fixture_abi-floor-internal",
            receipt_path=saturated_receipt,
        )
        saturated.active_connections = MAX_ACTIVE_CONNECTIONS
        saturated_thread = threading.Thread(target=saturated.serve, daemon=True)
        saturated_thread.start()
        for _ in range(100):
            if saturated_receipt.is_file():
                break
            saturated_thread.join(timeout=0.01)
        if not saturated_receipt.is_file() or not saturated_thread.is_alive():
            raise AssertionError("saturated relay did not become ready")
        with socket.create_connection(("127.0.0.1", saturated.bind_port), timeout=2.0) as client:
            if client.recv(1) != b"":
                raise AssertionError("saturated relay did not reject excess connection")
        saturated.stop()
        saturated_thread.join(timeout=2.0)
        saturated_final = json.loads(saturated_receipt.read_text(encoding="utf-8"))
        saturated_counters = saturated_final.get("counters", {})
        if saturated_thread.is_alive() \
                or saturated_counters.get("connectionsAccepted") != 1 \
                or saturated_counters.get("connectionsRejected") != 1 \
                or saturated_counters.get("connectionsCompleted") != 1 \
                or saturated_counters.get("targetConnectionsSucceeded") != 0 \
                or saturated_counters.get("targetConnectionsFailed") != 0:
            raise AssertionError(
                f"relay connection-ceiling receipt differs: {saturated_final!r}"
            )
    print("ABI-floor host-loopback relay self-test: PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--bind-port", type=int)
    parser.add_argument("--target-host")
    parser.add_argument("--target-port", type=int)
    parser.add_argument("--target-container-id")
    parser.add_argument("--target-network")
    parser.add_argument("--receipt", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_values = (
        args.bind_port,
        args.target_host,
        args.target_port,
        args.target_container_id,
        args.target_network,
        args.receipt,
    )
    if args.self_test:
        if any(value is not None for value in runtime_values):
            raise ValueError("--self-test cannot be combined with runtime arguments")
        self_test()
        return 0
    if any(value is None for value in runtime_values):
        raise ValueError("all runtime relay arguments are required")
    validate_runtime_args(args)
    install_parent_death_signal()
    relay = LoopbackRelay(
        bind_port=args.bind_port,
        target_host=args.target_host,
        target_port=args.target_port,
        target_container_id=args.target_container_id,
        target_network=args.target_network,
        receipt_path=args.receipt,
    )
    signal.signal(signal.SIGINT, lambda _signum, _frame: relay.stop())
    signal.signal(signal.SIGTERM, lambda _signum, _frame: relay.stop())
    relay.serve()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"FATAL: ABI-floor relay: {error}", file=sys.stderr)
        raise SystemExit(1)
