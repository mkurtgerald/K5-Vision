"""Fresh Windows QA: inspect real production relay/donor image identities.

Only generated loopback listeners and copied pinned development workers run.
Cleanup calls OwnedNativeProcess.stop(), never terminates enumerated processes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path, PureWindowsPath

from k5vision.operator_native_process import OwnedNativeProcess


def _module(path):
    spec = importlib.util.spec_from_file_location("k5_worker_image_native", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Native identity module unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not hasattr(module.NativeProcessTree, "details"):
        raise RuntimeError("Exact diagnostic identity API unavailable")
    return module


def _form(image):
    text = str(image)
    if text.startswith("\\\\?\\"):
        return "extended"
    if text.startswith(("\\Device\\", "\\??\\")):
        return "nt_device"
    if text.startswith("\\\\"):
        return "unc"
    return "drive" if PureWindowsPath(text).drive else "other"


def _row(tree, identity, role, expected):
    details = tree.details(identity)
    image = details["image"]
    result = {
        **details,
        "observed_role": role,
        "path_form": _form(image),
        "basename": PureWindowsPath(image).name,
        "expected_image": str(expected),
        "lexical_equal": str(PureWindowsPath(image)).casefold()
        == str(PureWindowsPath(expected)).casefold(),
    }
    if isinstance(result.get("sid"), bytes):
        result["sid"] = result["sid"].hex()
    try:
        actual, wanted = Path(image).stat(), expected.stat()
        result.update(
            same_file=Path(image).samefile(expected),
            actual_file_id=[actual.st_dev, actual.st_ino],
            expected_file_id=[wanted.st_dev, wanted.st_ino],
        )
    except (OSError, ValueError) as error:
        result.update(same_file=False, file_identity_error=type(error).__name__)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-module", type=Path, required=True)
    arguments = parser.parse_args()
    receipt = {
        "schema_version": 1,
        "status": "probe_failed",
        "observations": [],
        "actual_product_relay": True,
        "service_account_qualified": False,
        "actual_camera_data": False,
        "cleanup_completed": False,
    }
    owners, listeners = [], []
    workspace = None
    try:
        if (
            os.name != "nt"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            raise RuntimeError("Fresh hosted Windows QA required")
        module = _module(arguments.identity_module.resolve(strict=True))
        tree = module.NativeProcessTree()
        current = tree.details((os.getpid(), tree.birth(os.getpid())))
        receipt["caller"] = {key: current[key] for key in ("sid", "session", "elevated")}
        if isinstance(receipt["caller"]["sid"], bytes):
            receipt["caller"]["sid"] = receipt["caller"]["sid"].hex()
        receipt["identity_module_sha256"] = hashlib.sha256(
            arguments.identity_module.read_bytes()
        ).hexdigest()
        workspace = tempfile.TemporaryDirectory(prefix="K5 native worker images ")
        parent = Path(workspace.name)
        for _ in range(2):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        ports = [listener.getsockname()[1] for listener in listeners]
        media = Path(os.environ["K5_MEDIAMTX_DONOR_EXE"]).resolve(strict=True)
        caddy = Path(os.environ["K5_CADDY_DONOR_EXE"]).resolve(strict=True)
        media_target, caddy_target = parent / "mediamtx.exe", parent / "caddy.exe"
        for source, target in ((media, media_target), (caddy, caddy_target)):
            shutil.copyfile(source, target)
            if (
                hashlib.sha256(source.read_bytes()).digest()
                != hashlib.sha256(target.read_bytes()).digest()
            ):
                raise RuntimeError("Pinned development worker copy mismatch")
        media_config = parent / "media.json"
        media_config.write_text(
            json.dumps(
                {
                    "logLevel": "error",
                    "api": False,
                    "metrics": False,
                    "pprof": False,
                    "playback": False,
                    "rtsp": True,
                    "rtspAddress": f"127.0.0.1:{ports[0]}",
                    "rtspTransports": ["tcp"],
                    "rtmp": False,
                    "hls": False,
                    "webrtc": False,
                    "srt": False,
                    "paths": {"all_others": {}},
                }
            ),
            encoding="utf-8",
        )
        caddy_config = parent / "gateway.json"
        caddy_config.write_text(
            json.dumps(
                {
                    "admin": {"disabled": True},
                    "logging": {"logs": {"default": {"level": "ERROR"}}},
                    "apps": {
                        "http": {
                            "servers": {
                                "probe": {
                                    "listen": [f"127.0.0.1:{ports[1]}"],
                                    "routes": [
                                        {
                                            "handle": [
                                                {
                                                    "handler": "static_response",
                                                    "body": "native worker probe",
                                                    "status_code": 200,
                                                }
                                            ]
                                        }
                                    ],
                                }
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        for listener in listeners:
            listener.close()
        listeners.clear()
        for role, executable, command, port in (
            ("mediamtx", media_target, [str(media_target), str(media_config)], ports[0]),
            (
                "caddy",
                caddy_target,
                [str(caddy_target), "run", "--config", str(caddy_config)],
                ports[1],
            ),
        ):
            owner = OwnedNativeProcess(command, cwd=parent)
            owners.append(owner)
            root = (owner.pid, tree.birth(owner.pid))
            if root[1] is None:
                raise RuntimeError("Owned production relay exited early")
            deadline = time.monotonic() + 10
            ready = False
            while time.monotonic() < deadline and owner.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        ready = True
                    break
                except OSError:
                    time.sleep(0.05)
            if not ready:
                raise RuntimeError("Owned native fixture listener unavailable")
            rows = [_row(tree, root, role + "_relay", Path(sys.executable))]
            for identity in tree.descendants(root):
                if identity == root:
                    continue
                try:
                    rows.append(_row(tree, identity, role + "_child", executable))
                except (OSError, ProcessLookupError) as error:
                    rows.append(
                        {
                            "pid": identity[0],
                            "created_100ns": identity[1],
                            "observed_role": role + "_child",
                            "details_error": type(error).__name__,
                        }
                    )
                if len(rows) >= 16:
                    raise RuntimeError("Owned fixture exceeded its observation bound")
            receipt["observations"].append(
                {"worker": role, "listener_ready": ready, "processes": rows}
            )
        receipt["status"] = "diagnostic_complete"
    except Exception as error:
        receipt["failure_type"] = type(error).__name__
    finally:
        cleanup_errors = []
        for owner in reversed(owners):
            try:
                owner.stop()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        for listener in listeners:
            listener.close()
        if workspace is not None:
            try:
                workspace.cleanup()
            except Exception as error:
                cleanup_errors.append(type(error).__name__)
        receipt["cleanup_completed"] = not cleanup_errors
        if cleanup_errors:
            receipt["status"] = "probe_failed"
            receipt["cleanup_error_types"] = cleanup_errors
        Path("native-worker-images.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(receipt))
    return 0 if receipt["status"] == "diagnostic_complete" and receipt["cleanup_completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
