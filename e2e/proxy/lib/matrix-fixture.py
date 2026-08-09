#!/usr/bin/env python3
"""Loopback-only HTTP fixture for the proxy matrix's no-Docker regressions."""

from __future__ import annotations

import argparse
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    import brotli
except ImportError:  # The live matrix permits an identity fallback for Brotli.
    brotli = None


HTML = b'<html><script src="/RefreshKit/kit.js?v=fixture"></script></html>\n'
GZIP = gzip.compress(HTML, mtime=0)
BROTLI = brotli.compress(HTML) if brotli is not None else HTML
BASE_ETAGS = {
    "identity": '"rk-fixture-identity"',
    "gzip": '"rk-fixture-gzip"',
    "br": '"rk-fixture-br"' if brotli is not None else '"rk-fixture-identity"',
}


def etags_for(mode: str) -> dict[str, str]:
    etags = dict(BASE_ETAGS)
    if mode == "weak":
        return {name: f"W/{value}" for name, value in etags.items()}
    if mode == "gzip-equals-identity":
        etags["gzip"] = etags["identity"]
    elif mode == "br-equals-identity":
        etags["br"] = etags["identity"]
    elif mode == "br-equals-gzip":
        etags["br"] = etags["gzip"]
    return etags


class MatrixHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    suppress_conditionals = False
    etags = BASE_ETAGS

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def selected_representation(self) -> tuple[str, bytes, str]:
        accepted = self.headers.get("Accept-Encoding", "")
        tokens = {part.split(";", 1)[0].strip().lower() for part in accepted.split(",")}
        if "br" in tokens and brotli is not None:
            return "br", BROTLI, "br"
        if "gzip" in tokens:
            return "gzip", GZIP, "gzip"
        return "identity", HTML, ""

    def send_shell(self) -> None:
        name, body, encoding = self.selected_representation()
        etag = self.etags[name]

        if not self.suppress_conditionals:
            if_match = self.headers.get("If-Match")
            if if_match is not None and if_match != etag:
                self.send_response(412)
                self.send_header("Cache-Control", "no-store")
                # Go reverse proxies commonly serialize an otherwise bodyless
                # 412 this way; the matrix must accept absent or exactly zero.
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Vary", "Accept-Encoding")
                if name == "gzip":
                    # RFC 9110 permits these optional fields when they describe
                    # the selected response, including its hypothetical length.
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Encoding", encoding)
                    self.send_header("Content-Length", str(len(body)))
                elif name == "br":
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Encoding", encoding)
                self.end_headers()
                return

        headers = [
            ("ETag", etag),
            ("Cache-Control", "no-cache"),
            ("Vary", "Accept-Encoding"),
        ]
        if encoding:
            headers.append(("Content-Encoding", encoding))
        self.send_bytes(200, body, "text/html; charset=utf-8", tuple(headers))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path == "/web/":
            self.send_shell()
        elif path == "/RefreshKit/Generation":
            payload = json.dumps({"CacheKey": "fixture"}, separators=(",", ":")).encode()
            self.send_bytes(200, payload, "application/json")
        elif path == "/RefreshKit/Generation.txt":
            self.send_bytes(200, b"fixture\n", "text/plain")
        elif path == "/RefreshKit/kit.js":
            self.send_bytes(200, b"window.__rkFixture = true;\n", "text/javascript")
        else:
            self.send_bytes(404, b"not found\n", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--suppress-conditionals", action="store_true")
    parser.add_argument(
        "--etag-mode",
        choices=(
            "normal",
            "weak",
            "gzip-equals-identity",
            "br-equals-identity",
            "br-equals-gzip",
        ),
        default="normal",
    )
    args = parser.parse_args()

    handler = type(
        "ConfiguredMatrixHandler",
        (MatrixHandler,),
        {
            "suppress_conditionals": args.suppress_conditionals,
            "etags": etags_for(args.etag_mode),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    args.port_file.write_text(f"{server.server_port}\n", encoding="ascii")
    server.serve_forever()


if __name__ == "__main__":
    main()
