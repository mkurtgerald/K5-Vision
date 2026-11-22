#!/usr/bin/env python3
"""Admit and smoke-test the experimental derivative on a native Linux host."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "v1.21.2-k5-monotonic1"


class SmokeFailure(Exception):
    """A finite diagnostic which cannot contain credentials or process output."""

    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


def require(condition, kind):
    if not condition:
        raise SmokeFailure(kind)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stop_owned(process):
    """Reap only this controller's child, with bounded escalation."""
    if process is None:
        return True
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    except ProcessLookupError:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def request_info(port, authorization, deadline):
    def remaining_timeout():
        remaining = deadline - time.monotonic()
        require(remaining > 0, "readiness_timeout")
        return min(0.5, remaining)

    # HTTP/1.0 plus Connection: close provides a finite body without chunk framing.
    # Each receive checks the total monotonic budget, including slow responses.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(remaining_timeout())
        connection.connect(("127.0.0.1", port))
        lines = ["GET /v3/info HTTP/1.0", f"Host: 127.0.0.1:{port}", "Connection: close"]
        if authorization:
            lines.append("Authorization: " + authorization)
        connection.settimeout(remaining_timeout())
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        response = bytearray()
        while True:
            connection.settimeout(remaining_timeout())
            chunk = connection.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            require(len(response) <= 16384, "api_response_size")
    header, separator, body = bytes(response).partition(b"\r\n\r\n")
    status_line = header.split(b"\r\n", 1)[0]
    require(
        bool(separator) and bool(re.fullmatch(rb"HTTP/1\.[01] [0-9]{3}(?: .*)?", status_line)),
        "api_response_format",
    )
    require(len(body) <= 8192, "api_response_size")
    return int(status_line.split(b" ", 2)[1]), body


def native_probe(binary, version, receipt):
    # Reserve three seconds of the fifteen-second process budget for cleanup.
    deadline = time.monotonic() + 12
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("MTX_", "RTSP_"))
    }
    receipt["failure_phase"] = "version"
    version_process = subprocess.Popen(
        [str(binary), "--version"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    receipt["cleanup_complete"] = False
    try:
        output, _ = version_process.communicate(
            timeout=min(3, max(0.001, deadline - time.monotonic()))
        )
        require(
            version_process.returncode == 0 and output.decode().strip() == version,
            "version_identity",
        )
    finally:
        receipt["cleanup_complete"] = stop_owned(version_process)
        if version_process.stdout is not None:
            version_process.stdout.close()
    require(receipt["cleanup_complete"], "cleanup_timeout")
    receipt["failure_phase"] = "startup"
    process = None
    with tempfile.TemporaryDirectory(prefix="k5-mediamtx-smoke-") as private_directory:
        directory = Path(private_directory)
        os.chmod(directory, 0o700)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        username, password = secrets.token_hex(16), secrets.token_hex(32)
        config = {
            "logLevel": "error",
            "logDestinations": ["stdout"],
            "api": True,
            "apiAddress": f"127.0.0.1:{port}",
            "apiEncryption": False,
            "authMethod": "internal",
            "authInternalUsers": [
                {
                    "user": username,
                    "pass": password,
                    "ips": ["127.0.0.1"],
                    "permissions": [{"action": "api"}],
                }
            ],
            "rtsp": False,
            "rtmp": False,
            "hls": False,
            "webrtc": False,
            "srt": False,
            "moq": False,
            "playback": False,
            "metrics": False,
            "pprof": False,
            "paths": {},
        }
        config_path = directory / "mediamtx.json"
        with config_path.open("x", encoding="utf-8") as stream:
            os.chmod(config_path, 0o600)
            json.dump(config, stream)
        authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        try:
            process = subprocess.Popen(
                [str(binary), str(config_path)],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            receipt["cleanup_complete"] = False
            receipt["failure_phase"] = "anonymous_api"
            while True:
                require(process.poll() is None, "startup_exit")
                require(time.monotonic() < deadline, "readiness_timeout")
                try:
                    status, _ = request_info(port, None, deadline)
                except OSError:
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                    continue
                require(status == 401, "anonymous_authentication")
                break
            receipt["failure_phase"] = "authenticated_api"
            require(process.poll() is None, "startup_exit")
            status, body = request_info(port, authorization, deadline)
            require(status == 200, "authenticated_status")
            info = json.loads(body)
            require(isinstance(info, dict) and info.get("version") == version, "api_version")
            require(process.poll() is None, "startup_exit")
        finally:
            receipt["cleanup_complete"] = stop_owned(process)
        require(receipt["cleanup_complete"], "cleanup_timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = {
        "schema_version": 1,
        "status": "failed",
        "native_linux": False,
        "product_qualification": False,
        "cleanup_complete": True,
        "failure_phase": "admission",
    }
    try:
        require(sys.platform == "linux", "host_platform")
        directory = args.directory.resolve()
        provenance = json.loads((directory / "build-provenance.json").read_text(encoding="utf-8"))
        source_head = os.environ.get("GITHUB_SHA", "")
        require(
            bool(re.fullmatch(r"[0-9a-f]{40}", source_head))
            and provenance.get("source_head") == source_head
            and provenance.get("status") == "passed"
            and provenance.get("reproducible_binary") is True
            and provenance.get("product_qualification") is False
            and provenance.get("derivative_version") == VERSION,
            "provenance_identity",
        )
        receipt["source_head"] = source_head
        pin = provenance.get("linux_cross_build")
        require(isinstance(pin, dict), "linux_pin")
        require(
            pin.get("goos") == "linux"
            and pin.get("goarch") == "amd64"
            and pin.get("version") == VERSION
            and pin.get("native_execution_observed") is False
            and isinstance(pin.get("bytes"), int)
            and not isinstance(pin["bytes"], bool)
            and pin["bytes"] > 0
            and isinstance(pin.get("sha256"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])),
            "linux_pin",
        )
        binary = directory / "mediamtx"
        require(binary.is_file() and not binary.is_symlink(), "binary_identity")
        require(binary.stat().st_size == pin["bytes"], "binary_identity")
        require(digest(binary) == pin["sha256"], "binary_identity")
        receipt.update(
            executable_sha256=pin["sha256"], executable_bytes=pin["bytes"], version=VERSION
        )
        # Artifact extraction may omit executable permissions. Identity is admitted first.
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        native_probe(binary, VERSION, receipt)
        receipt.update(status="passed", native_linux=True)
        del receipt["failure_phase"]
    except SmokeFailure as error:
        receipt["failure_kind"] = error.kind
    except subprocess.TimeoutExpired:
        receipt["failure_kind"] = "process_timeout"
    except (OSError, ValueError, TypeError, KeyError):
        receipt["failure_kind"] = "invalid_input_or_io"
    except KeyboardInterrupt:
        receipt["failure_kind"] = "interrupted"
    except Exception:
        receipt["failure_kind"] = "unexpected_error"
    try:
        args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    except OSError:
        print('{"status":"failed","failure_phase":"receipt","failure_kind":"receipt_io"}')
        return 1
    print(json.dumps(receipt))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
