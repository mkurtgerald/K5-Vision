"""A historical request-size cap remains enforced while the fixture uses UTF-8."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from k5vision.legacy_migration_wire import MAX_FRAME, read_frame, write_frame


def _module(filename, name):
    path = Path(__file__).resolve().parents[1] / "integration" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_real_http_fixture_keeps_historical_body_guard_and_larger_private_verify_frame():
    harness = _module("test_windows_desktop_upgrade.py", "legacy_fixture_http_harness")
    runtime = _module("test_operator_product_runtime.py", "legacy_fixture_http_runtime")
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_PUT(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            assert self.path == "/api/v1/operator/admin/sso/provider"
            assert self.headers["Authorization"] == "Bearer generated-fixture-token"
            assert self.headers["Origin"] == f"http://127.0.0.1:{self.server.server_port}"
            accepted = length <= 16 * 1024
            observed.append({"bytes": length, "accepted": accepted, "body": body})
            payload = json.dumps({"has_client_secret": accepted}).encode()
            self.send_response(200 if accepted else 413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = runtime._Client(server.server_port)
        client.token = "generated-fixture-token"
        # This is the exact original fixture transport and character count.
        # Its expanded request must still be rejected by the historical guard.
        with pytest.raises(AssertionError, match="unexpected status"):
            client.json(
                "/api/v1/operator/admin/sso/provider",
                method="PUT",
                data={
                    "enabled": False,
                    "display_name": "Retained migration issuer",
                    "issuer": "https://id.example.test",
                    "client_id": "migration-fixture",
                    "client_secret": "\U0001f600" * 4096,
                    "groups_claim": "groups",
                },
            )
        diagnostic = {}
        harness._configure_unicode_provider(client, diagnostic)
        assert diagnostic == {"baseline_sso_http_status": 200}
        assert [row["accepted"] for row in observed] == [False, True]
        assert observed[0]["bytes"] > 16 * 1024
        assert observed[1]["bytes"] == len(observed[1]["body"]) == 14170
        assert observed[1]["bytes"] < 16 * 1024
        value = json.loads(observed[1]["body"])
        clear = value["client_secret"].encode("utf-8")
        assert clear.decode() == "\U0001f600" * 3500
        # A representative bounded cipher is only a framing fixture. Actual
        # DPAPI/token behavior remains an explicit native qualification gate.
        document = {
            "schema_version": 1,
            "operation": "service",
            "request": {
                "schema_version": 1,
                "operation": "verify",
                "purpose": "oidc-client-secret",
                "clear": base64.b64encode(clear).decode(),
                "protected": base64.b64encode(b"dpapi-v1:" + b"x" * len(clear)).decode(),
            },
        }
        stream = io.BytesIO()
        write_frame(stream, document)
        assert 32768 < len(stream.getvalue()) <= MAX_FRAME + 4
        stream.seek(0)
        assert read_frame(stream) == document
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
