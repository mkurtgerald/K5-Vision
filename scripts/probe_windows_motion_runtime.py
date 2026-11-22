"""Diagnostic-only real recorder, durable policy polling and unchanged motion child."""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def _load(name):
    path = Path.cwd() / "tests/integration" / (name + ".py")
    spec = importlib.util.spec_from_file_location("motion_runtime_probe_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _port():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]


def _instrument(product, receipt, phase, began):
    runtime = product.service._motion
    status, new_worker, get = runtime.status, runtime._new_worker, runtime.store.get
    mutation = product.service._mutation
    workers = set()

    def stamp():
        return round(time.monotonic() - began, 4)

    def projection(binding):
        result = status(binding)
        path = binding.path_name
        sample = runtime.samples.get(path)
        age = None if sample is None else time.monotonic() - sample["captured_at"]
        if len(receipt["projections"]) < 1024:
            receipt["projections"].append(
                {
                    "elapsed_seconds": stamp(),
                    "phase": phase["name"],
                    "status": result["status"],
                    "service_ready": product.service._ready,
                    "needs_reconcile": product.service._needs_reconcile,
                    "health": runtime.health.get(path, "absent"),
                    "sample_present": sample is not None,
                    "sample_age_seconds": None if age is None else round(age, 4),
                    "sample_fresh": age is not None and 0 <= age <= 2,
                    "sample_inflight": path in runtime.inflight,
                    "gate": runtime.gates.get(path),
                    "pending_gate": runtime.pending_gates.get(path),
                    "moving": runtime.moving.get(path),
                    "hold_remaining_seconds": None
                    if path not in runtime.holds
                    else round(runtime.holds[path] - time.monotonic(), 4),
                }
            )
        return result

    def observe_worker(worker):
        if worker in workers:
            return worker
        workers.add(worker)
        sample = worker.sample

        def observed(policy):
            started = time.monotonic()
            note = {"phase": phase["name"], "started_seconds": stamp()}
            try:
                result = sample(policy)
                note.update(
                    outcome="ready",
                    sequence=result.sequence,
                    age_seconds=round(result.age_seconds, 4),
                )
                return result
            except BaseException as error:
                note.update(outcome="refused", kind=type(error).__name__)
                raise
            finally:
                note["duration_seconds"] = round(time.monotonic() - started, 4)
                if len(receipt["worker_samples"]) < 512:
                    receipt["worker_samples"].append(note)

        worker.sample = observed
        return worker

    def factory(service, source):
        return observe_worker(new_worker(service, source))

    def durable(source):
        started = time.monotonic()
        try:
            return get(source)
        finally:
            if len(receipt["durable_reads"]) < 1024:
                receipt["durable_reads"].append(
                    {
                        "phase": phase["name"],
                        "elapsed_seconds": stamp(),
                        "duration_seconds": round(time.monotonic() - started, 4),
                    }
                )

    def observed_mutation(method, route, payload=None, **kwargs):
        if method != "PATCH" or type(payload) is not dict or set(payload) != {"record"}:
            return mutation(method, route, payload, **kwargs)
        started = time.monotonic()
        note = {"phase": phase["name"], "record": payload["record"], "started_seconds": stamp()}
        try:
            result = mutation(method, route, payload, **kwargs)
            note["outcome"] = "applied"
            return result
        except BaseException as error:
            note.update(outcome="refused", kind=type(error).__name__)
            for field in ("winerror", "errno", "code"):
                value = getattr(error, field, None)
                if type(value) is int:
                    note[field] = value
            raise
        finally:
            note["duration_seconds"] = round(time.monotonic() - started, 4)
            if len(receipt["gate_mutations"]) < 256:
                receipt["gate_mutations"].append(note)

    runtime.status, runtime._new_worker, runtime.store.get = projection, factory, durable
    product.service._mutation = observed_mutation
    for worker in tuple(runtime.workers.values()):
        observe_worker(worker)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clock-steps", action="store_true")
    parser.add_argument("--baseline-source-head")
    args = parser.parse_args()
    if args.clock_steps and not args.baseline_source_head:
        parser.error("Clock diagnostics require the exact baseline source head")
    from k5vision import identity_state
    from k5vision import operator_product_service as product_module
    from k5vision.operator_native_process import OwnedNativeProcess
    from k5vision.operator_recording_policy import MotionPolicy

    native = _load("test_operator_product_runtime")
    fixture = _load("motion_camera_fixture")
    phase = {"name": "bootstrap"}
    receipt = {
        "schema_version": 1,
        "status": "infrastructure_failure",
        "scope": "actual_development_orchestrator_no_installer",
        "diagnostic_only": True,
        "cleanup_complete": False,
        "worker_samples": [],
        "durable_reads": [],
        "projections": [],
        "polls": [],
        "camera_observations": [],
        "motion_child_launches": 0,
        "motion_child_argv_unchanged": True,
        "configuration_writes": [],
        "baseline_source_head": args.baseline_source_head,
        "source_progress": [],
        "clock_steps": [],
        "gate_mutations": [],
    }
    product = upstream = publisher = None
    original_popen = subprocess.Popen
    original_configuration = product_module._private_configuration
    began = time.monotonic()

    def observed_popen(arguments, *positional, **kwargs):
        if (
            isinstance(arguments, (list, tuple))
            and "k5vision.operator_motion_process" in arguments
            and arguments[-1] == "--child"
        ):
            receipt["motion_child_launches"] += 1
            receipt["motion_child_argv_unchanged"] &= arguments[1:] == [
                "-I",
                "-B",
                "-m",
                "k5vision.operator_motion_process",
                "--child",
            ]
        return original_popen(arguments, *positional, **kwargs)

    def observed_configuration(path, content):
        started = time.monotonic()
        note = {
            "phase": phase["name"],
            "component": "media" if path.name == "operator-mediamtx.json" else "gateway",
        }
        try:
            result = original_configuration(path, content)
            note["outcome"] = "written"
            return result
        except BaseException as error:
            note.update(outcome="refused", kind=type(error).__name__)
            for field in ("winerror", "hresult", "errno"):
                value = getattr(error, field, None)
                if type(value) is int:
                    note[field] = value
            raise
        finally:
            note["duration_seconds"] = round(time.monotonic() - started, 4)
            if len(receipt["configuration_writes"]) < 128:
                receipt["configuration_writes"].append(note)

    with tempfile.TemporaryDirectory(prefix="k5-motion-runtime-") as temporary:
        root = Path(temporary)
        try:
            subprocess.Popen = observed_popen
            product_module._private_configuration = observed_configuration
            private = root / "private"
            private.mkdir(mode=0o700)
            media, caddy = private / "mediamtx.exe", private / "caddy.exe"
            for source, target in (
                (Path(os.environ["K5_MEDIAMTX_DONOR_EXE"]), media),
                (Path(os.environ["K5_CADDY_DONOR_EXE"]), caddy),
            ):
                shutil.copyfile(source, target)
                target.chmod(0o700)
            username, password = "motion-runtime-admin", secrets.token_urlsafe(32)
            identity = identity_state.initialize_identity_state(
                private / "identity", username=username, password=password
            )
            upstream_port, *ports = [_port() for _ in range(7)]
            stream_password = secrets.token_urlsafe(32)
            source_uri = f"rtsp://local:{stream_password}@127.0.0.1:{upstream_port}/camera"
            config = {
                "logLevel": "warn",
                "authInternalUsers": [
                    {
                        "user": "local",
                        "pass": stream_password,
                        "ips": ["127.0.0.1"],
                        "permissions": [
                            {"action": "read", "path": "camera"},
                            {"action": "publish", "path": "camera"},
                        ],
                    }
                ],
                "rtspAddress": f"127.0.0.1:{upstream_port}",
                "rtspTransports": ["tcp"],
                "rtmp": False,
                "hls": False,
                "webrtc": False,
                "srt": False,
                "moq": False,
                "paths": {"camera": {"source": "publisher"}},
            }
            config_path = private / "upstream.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            upstream = OwnedNativeProcess([str(media), str(config_path)], cwd=private)
            time.sleep(0.4)
            publisher = fixture.MotionPublisher(Path(os.environ["K5_DONOR_FFMPEG"]), source_uri)
            product = native._Product(identity, private / "recordings", media, caddy, ports)
            client = native._Client(ports[0])
            native._wait(
                lambda: product.server.started and client.get("/operator/")[0] == 200,
                label="Actual recorder startup",
                product=product,
                timeout=30,
            )
            _instrument(product, receipt, phase, began)
            native._login(client, username, password)
            camera = client.json(
                "/api/v1/operator/cameras",
                method="POST",
                expected=201,
                data={
                    "name": "Generated runtime camera",
                    "host": "127.0.0.1",
                    "source_uri": source_uri,
                },
            )
            route = f"/api/v1/operator/cameras/{camera['device_id']}/recording-policy"
            policy = MotionPolicy(
                mode="motion", post_capture_seconds=3, minimum_area_percent=0.1
            ).model_dump(mode="json")
            phase["name"] = "save_motion"
            saved = client.json(route, method="PUT", data={**policy, "expected_revision": 0})
            receipt["saved_policy"] = {key: saved[key] for key in ("mode", "revision", "status")}

            def source_snapshot():
                service = product.service
                token = base64.b64encode(
                    f"{service.credentials.username}:{service.credentials.password}".encode()
                ).decode()
                path = service._sources[0].binding.path_name
                request = Request(
                    f"http://127.0.0.1:{service.ports['api_port']}/v3/paths/get/{path}",
                    headers={"Authorization": "Basic " + token},
                )
                note = {
                    "phase": phase["name"],
                    "elapsed_seconds": round(time.monotonic() - began, 4),
                    "publisher_frames_written": publisher.frames_written,
                    "publisher_process_alive": publisher.process.poll() is None,
                    "publisher_writer_alive": publisher._writer.is_alive(),
                    "publisher_writer_failed": publisher._failure,
                }
                try:
                    with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
                        value = json.loads(response.read(65536))
                    note.update(
                        source_ready=value.get("ready") is True,
                        inbound_bytes=value.get("inboundBytes", value.get("bytesReceived")),
                        outbound_bytes=value.get("outboundBytes", value.get("bytesSent")),
                    )
                except (OSError, ValueError) as error:
                    note["failure_kind"] = type(error).__name__
                receipt["source_progress"].append(note)
                return note

            def poll(name, seconds):
                phase["name"] = name
                until, index = time.monotonic() + seconds, 0
                while time.monotonic() < until:
                    started = time.monotonic()
                    value = client.json(route)
                    receipt["polls"].append(
                        {
                            "phase": name,
                            "elapsed_seconds": round(time.monotonic() - began, 4),
                            "duration_seconds": round(time.monotonic() - started, 4),
                            "status": value["status"],
                            "publisher_frames_written": publisher.frames_written,
                        }
                    )
                    if index % 5 == 0:
                        source_snapshot()
                        cameras = client.json("/api/v1/operator/cameras")
                        receipt["camera_observations"].append(
                            {
                                "phase": name,
                                "elapsed_seconds": round(time.monotonic() - began, 4),
                                "worker_status": cameras["worker_status"],
                                "camera_status": cameras["cameras"][0]["status"],
                            }
                        )
                    index += 1
                    time.sleep(0.2)

            poll("initial_idle", 20)
            phase["name"] = "recorder_restart"
            product.stop()
            product = None
            product = native._Product(identity, private / "recordings", media, caddy, ports)
            native._wait(
                lambda: product.server.started and client.get("/operator/")[0] == 200,
                label="Actual recorder restart",
                product=product,
                timeout=30,
            )
            _instrument(product, receipt, phase, began)
            native._login(client, username, password)
            poll("restart_idle", 20)
            if args.clock_steps:
                desktop = _load("test_windows_desktop_app")

                def wait_state(name, wanted, timeout, note):
                    phase["name"] = name
                    wait = {"wanted": wanted, "timeout_seconds": timeout, "states": []}
                    note["policy_wait"] = wait

                    def ready():
                        started = time.monotonic()
                        try:
                            value = client.json(route)
                            wait["last_status"] = value["status"]
                            if len(wait["states"]) < 256:
                                wait["states"].append(value["status"])
                            receipt["polls"].append(
                                {
                                    "phase": name,
                                    "status": value["status"],
                                    "elapsed_seconds": round(time.monotonic() - began, 4),
                                    "duration_seconds": round(time.monotonic() - started, 4),
                                    "publisher_frames_written": publisher.frames_written,
                                }
                            )
                            return value["status"] == wanted
                        finally:
                            wait["last_http_status"] = client.last_http_status
                            wait["transport_failure"] = client.transport_failure

                    desktop._wait(ready, "Actual clock motion state", timeout=timeout)

                guard = _load("windows_vm_clock").WindowsVmClockGuard(
                    root / "runtime-clock-guard.json", watchdog_seconds=90
                )
                try:
                    with guard:
                        for offset, label in ((172800, "forward"), (-172800, "backward")):
                            phase["name"] = "clock_" + label + "_before"
                            note = {"offset_seconds": offset, "before": source_snapshot()}
                            receipt["clock_steps"].append(note)
                            try:
                                publisher.movement("outside")
                                wait_state(
                                    "clock_" + label + "_await_recording", "recording", 15, note
                                )
                                publisher.movement("idle")
                                wait_state(
                                    "clock_" + label + "_await_post_capture",
                                    "post_capture",
                                    10,
                                    note,
                                )
                                hold_began = time.monotonic()
                                note["shift"] = guard.shift(offset)
                                note["immediate_state"] = client.json(route)["status"]
                                if note["immediate_state"] != "post_capture":
                                    raise RuntimeError(
                                        "Clock step changed the live post-capture hold"
                                    )
                                note["source_after_step"] = source_snapshot()
                                wait_state("clock_" + label + "_await_armed", "armed", 8, note)
                                note["post_capture_elapsed_seconds"] = round(
                                    time.monotonic() - hold_began, 4
                                )
                                if not 0.5 <= note["post_capture_elapsed_seconds"] <= 8:
                                    raise RuntimeError(
                                        "Clock hold elapsed outside its existing bounds"
                                    )
                                note["after_shift"] = source_snapshot()
                                note["restoration"] = guard.restore()
                                poll("clock_" + label + "_restored", 5)
                                note["after_restore"] = source_snapshot()
                            finally:
                                note["after_wait"] = source_snapshot()
                finally:
                    try:
                        receipt["clock_guard"] = guard.receipt()
                    except BaseException as error:
                        receipt["clock_guard_failure_kind"] = type(error).__name__
            phase["name"] = "source_stopped"
            publisher.close()
            poll("source_stopped", 5)
            receipt["status"] = "diagnostic_complete"
        except BaseException as error:
            receipt["failure_kind"] = type(error).__name__
            receipt["failure_phase"] = phase["name"]
            if "client" in locals():
                receipt["failure_http_status"] = client.last_http_status
                receipt["transport_failure"] = client.transport_failure
        finally:
            cleanup = True
            for action in (
                lambda: product.stop() if product is not None else None,
                lambda: publisher.close() if publisher is not None else None,
                lambda: upstream.stop() if upstream is not None else None,
            ):
                try:
                    action()
                except BaseException as error:
                    cleanup = False
                    receipt.setdefault("cleanup_kinds", []).append(type(error).__name__)
            subprocess.Popen = original_popen
            product_module._private_configuration = original_configuration
            receipt["cleanup_complete"] = cleanup
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if receipt["status"] == "diagnostic_complete" and receipt["cleanup_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
