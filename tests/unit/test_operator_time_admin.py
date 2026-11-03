from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from k5vision.domain.devices import DeviceProtocol
from k5vision.operator_onvif import OnvifError
from k5vision.operator_time_admin import (
    TimeAdministrationError,
    TimeAdministrationService,
    WindowsTimeAdministrator,
    _camera_operation,
    _current_windows_issuer,
    measure_reference,
    normalize_servers,
    unavailable_reference,
)
from k5vision.operator_time_probe import _timestamp, measure


@pytest.fixture
def private_profile(tmp_path, monkeypatch):
    if os.name == "posix":
        original = Path.lstat

        def admitted_system_ancestors(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                fields = list(info)
                fields[4] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", admitted_system_ancestors)
    profile = tmp_path / "time-profile"
    profile.mkdir(mode=0o700)
    return profile


@pytest.mark.parametrize(
    "values",
    [
        [],
        "time.example",
        ["time.example", "TIME.EXAMPLE."],
        ["a"] * 5,
        ["https://time.example"],
        ["time.example /resync"],
        ["time.example,0x9"],
        ["user@time.example"],
        ["-time.example"],
        ["bad\nserver"],
        ["224.0.0.1"],
        ["0.0.0.0"],
        ["::"],
        ["ff02::1"],
        ["fe80::1%3"],
    ],
)
def test_ntp_sources_refuse_commands_duplicates_and_invalid_addresses(values):
    with pytest.raises(ValueError):
        normalize_servers(values)


def test_ntp_sources_admit_only_normalized_bounded_host_values():
    assert normalize_servers(["TIME.Example.", "192.0.2.1", "2001:0db8::1"]) == [
        "time.example",
        "192.0.2.1",
        "2001:db8::1",
    ]
    assert normalize_servers([], allow_empty=True) == []


def _stamp(epoch):
    value = epoch + 2_208_988_800
    return struct.pack("!II", int(value) % 2**32, int(value % 1 * 2**32))


def test_ntp_era_rollover_is_resolved_near_current_utc():
    now = datetime(2040, 1, 1, tzinfo=UTC).timestamp()
    assert _timestamp(_stamp(now + 0.125), now) == now + 0.125


def _ntp_wire(monkeypatch, *, mutate=lambda value: None, wall_step=0, near=1_800_000_000):
    import k5vision.operator_time_probe as probe

    class Wire:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert 0 < value <= 2

        def connect(self, address):
            assert address == ("127.0.0.1", 123)

        def send(self, request):
            assert len(request) == 48 and request[0] == 0x23
            self.nonce = request[40:48]

        def recv(self, maximum):
            assert maximum == 513
            result = bytearray(48)
            result[0], result[1] = (4 << 3) | 4, 2
            result[24:32] = self.nonce
            result[32:40], result[40:48] = _stamp(near + 0.504), _stamp(near + 0.506)
            mutate(result)
            return bytes(result)

    monkeypatch.setattr(
        probe.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", 123))],
    )
    monkeypatch.setattr(probe.socket, "socket", lambda *args: Wire())
    elapsed = iter([5.0, 5.0, 6.0, 6.01])
    wall = iter([near, near + 0.01 + wall_step])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(elapsed))
    monkeypatch.setattr(probe.time, "time", lambda: next(wall))


def test_ntp_measurement_requires_matching_server_nonce_and_fresh_timestamps(monkeypatch):
    _ntp_wire(monkeypatch)
    result = measure("time.example")
    assert result["offset_seconds"] == pytest.approx(0.5, abs=0.000002)
    assert result["uncertainty_seconds"] == pytest.approx(0.004, abs=0.000002)
    assert result["sync_verified"] is True
    assert result["authenticated"] is False
    assert result["stratum"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda packet: packet.__setitem__(slice(24, 32), b"replayed"),
        lambda packet: packet.__setitem__(0, 0xE4),
        lambda packet: packet.__setitem__(0, 0x23),
        lambda packet: packet.__setitem__(1, 0),
        lambda packet: packet.__setitem__(1, 16),
        lambda packet: packet.__setitem__(slice(40, 48), bytes(8)),
        lambda packet: packet.__setitem__(slice(32, 40), _stamp(1_800_000_001)),
        lambda packet: packet.__setitem__(slice(8, 12), struct.pack("!I", 20 * 65536)),
    ],
)
def test_ntp_rejects_replay_unsynchronized_invalid_or_uncertain_servers(monkeypatch, mutation):
    _ntp_wire(monkeypatch, mutate=mutation)
    with pytest.raises(ValueError):
        measure("time.example")


def test_ntp_does_not_confuse_host_clock_step_with_server_offset(monkeypatch):
    _ntp_wire(monkeypatch, wall_step=172800)
    with pytest.raises(ValueError):
        measure("time.example")


def _native_administrator(profile, *, proof=True, accept=True, nonce=True, apply=True):
    executable = profile / "K5Vision.exe"
    executable.write_bytes(b"test-native-supervisor")
    executable.chmod(0o600)
    configuration = {
        "platform": "windows",
        "status": "available",
        "service_running": True,
        "mode": "manual",
        "ntp_servers": ["old.example"],
    }
    called = []

    def invoke(executable, request, response):
        value = json.loads(request.read_text())
        called.append(value)
        assert set(value) == {
            "schema_version",
            "nonce",
            "operation",
            "mode",
            "ntp_servers",
            "owner_pid",
            "owner_created_100ns",
        }
        assert value["owner_pid"] == os.getpid()
        assert value["owner_created_100ns"] == 134065190123456789
        assert type(value["owner_created_100ns"]) is int
        assert len(value["nonce"]) == 32
        assert request.parent == response.parent == profile / "time-admin"
        assert not response.exists()
        if apply:
            configuration.update(mode=value["mode"], ntp_servers=value["ntp_servers"])
        response.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "nonce": value["nonce"] if nonce else "0" * 32,
                    "succeeded": accept,
                    "code": "completed" if accept else "access_denied",
                }
            )
        )
        response.chmod(0o600)

    def measured(server):
        assert server in {"old.example", "new.example"}
        result = unavailable_reference()
        if proof:
            result.update(
                status="measured",
                offset_seconds=0.001,
                uncertainty_seconds=0.005,
                measured_at="2026-10-31T12:00:00.000Z",
                stratum=2,
                sync_verified=True,
            )
        return result

    value = WindowsTimeAdministrator(
        profile,
        executable=executable,
        read_configuration=lambda: configuration,
        measure=measured,
        invoke=invoke,
        owner_identity=lambda: {
            "owner_pid": os.getpid(),
            "owner_created_100ns": 134065190123456789,
        },
    )
    return value, called, configuration


def test_native_configuration_requires_actual_readback_and_reference(private_profile):
    value, calls, _configuration = _native_administrator(private_profile)
    result = value.operate("configure", ntp_servers=["new.example"])
    assert calls[0]["operation"] == "configure"
    assert result["host"]["ntp_servers"] == ["new.example"]
    assert result["operation"] == {"action": "configure", "status": "verified", "verified": True}
    assert not list((private_profile / "time-admin").iterdir())


def test_issuer_birth_uses_exact_native_filetime_bits_without_float_rounding():
    class Kernel:
        def GetCurrentProcess(self):
            return 123

        def GetProcessTimes(self, process, created, exited, system, user):
            assert process == 123
            created._obj.high, created._obj.low = 0x01DDC0DE, 0x12345679
            return True

    owner = _current_windows_issuer(Kernel())
    assert owner == {
        "owner_pid": os.getpid(),
        "owner_created_100ns": (0x01DDC0DE << 32) | 0x12345679,
    }
    assert type(owner["owner_created_100ns"]) is int
    assert owner["owner_created_100ns"] != int(float(owner["owner_created_100ns"]))


@pytest.mark.parametrize(
    "owner",
    [
        {"owner_pid": True, "owner_created_100ns": 134065190123456789},
        {"owner_pid": 42, "owner_created_100ns": 134065190123456789.0},
        {"owner_pid": 0, "owner_created_100ns": 134065190123456789},
        {"owner_pid": 42, "owner_created_100ns": 0},
    ],
)
def test_unreliable_issuer_identity_cannot_issue_native_capability(private_profile, owner):
    value, calls, _ = _native_administrator(private_profile)
    value._owner_identity = lambda: owner
    with pytest.raises(TimeAdministrationError, match="^time_unavailable$"):
        value.operate("resync")
    assert not calls


def test_successful_native_action_does_not_invent_synchronization(private_profile):
    value, _calls, _ = _native_administrator(private_profile, proof=False)
    assert value.operate("configure", ntp_servers=["new.example"])["operation"] == {
        "action": "configure",
        "status": "configured",
        "verified": False,
    }
    assert value.operate("resync")["operation"] == {
        "action": "resync",
        "status": "completed",
        "verified": False,
    }


@pytest.mark.parametrize("changes", [{"nonce": False}, {"accept": False}, {"apply": False}])
def test_native_refusal_wrong_nonce_or_unchanged_configuration_fail_closed(
    private_profile, changes
):
    value, _, _ = _native_administrator(private_profile, **changes)
    with pytest.raises(TimeAdministrationError, match="^time_operation_failed$"):
        value.operate("configure", ntp_servers=["new.example"])
    assert not list((private_profile / "time-admin").iterdir())


def test_domain_sources_are_not_certified_from_dormant_manual_peer(private_profile):
    value, _, configuration = _native_administrator(private_profile)
    configuration["mode"] = "domain"
    assert value.status()["host"]["reference"]["sync_verified"] is False


def test_closed_time_administrator_cannot_open_another_uac_prompt(private_profile):
    value, called, _ = _native_administrator(private_profile)
    value.close()
    with pytest.raises(TimeAdministrationError, match="^time_unavailable$"):
        value.operate("resync")
    assert called == []


def test_missing_native_helper_is_finite_without_an_elevation_attempt(private_profile):
    called = []
    value = WindowsTimeAdministrator(
        private_profile,
        executable=private_profile / "missing-supervisor.exe",
        invoke=lambda *args: called.append(args),
    )
    with pytest.raises(TimeAdministrationError, match="^time_unavailable$"):
        value.operate("resync")
    assert not called and not list((private_profile / "time-admin").iterdir())


def test_native_result_schema_requires_an_integer_and_network_error_remains_explicit(
    private_profile,
):
    value, _calls, _configuration = _native_administrator(private_profile)
    original = value._invoke

    def boolean_schema(executable, request, response):
        original(executable, request, response)
        data = json.loads(response.read_bytes())
        data["schema_version"] = True
        response.write_text(json.dumps(data))

    value._invoke = boolean_schema
    with pytest.raises(TimeAdministrationError, match="^time_operation_failed$"):
        value.operate("resync")

    def unavailable(executable, request, response):
        original(executable, request, response)
        data = json.loads(response.read_bytes())
        data.update(succeeded=False, code="ntp_unreachable")
        response.write_text(json.dumps(data))

    value._invoke = unavailable
    with pytest.raises(TimeAdministrationError, match="^time_ntp_unreachable$"):
        value.operate("resync")


class _Camera:
    def __init__(self, *, ignore_write=False):
        self.clock = datetime.now(UTC) + timedelta(hours=48)
        self.mode, self.servers, self.dhcp = "Manual", [], False
        self.operations = []
        self.ignore_write = ignore_write

    def synchronize_clock(self, endpoint):
        self.operations.append("sample_clock")
        return True

    def request(self, endpoint, xml, namespace, operation):
        self.operations.append((operation, xml))
        assert namespace == "http://www.onvif.org/ver10/device/wsdl"
        assert endpoint == "http://192.0.2.2:80/onvif/device_service"
        if operation == "GetSystemDateAndTime":
            value = self.clock
            return ET.fromstring(
                f"<Reply><DateTimeType>{self.mode}</DateTimeType><DaylightSavings>true</DaylightSavings><TimeZone><TZ>EST5EDT</TZ></TimeZone><UTCDateTime><Time><Hour>{value.hour}</Hour><Minute>{value.minute}</Minute><Second>{value.second}</Second></Time><Date><Year>{value.year}</Year><Month>{value.month}</Month><Day>{value.day}</Day></Date></UTCDateTime></Reply>"
            )
        if operation == "GetNTP":
            return ET.fromstring(
                "<Reply><FromDHCP>"
                + str(self.dhcp).lower()
                + "</FromDHCP>"
                + "".join(
                    "<NTPManual><DNSname>" + server + "</DNSname></NTPManual>"
                    for server in self.servers
                )
                + "</Reply>"
            )
        root = ET.fromstring(
            '<Root xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">'
            + xml
            + "</Root>"
        )
        if operation == "SetNTP":
            self.dhcp = next(i.text for i in root.iter() if i.tag.endswith("FromDHCP")) == "true"
            self.servers = [
                i.text
                for i in root.iter()
                if i.tag.endswith(("DNSname", "IPv4Address", "IPv6Address"))
            ]
        if operation == "SetSystemDateAndTime" and not self.ignore_write:
            self.mode = next(i.text for i in root.iter() if i.tag.endswith("DateTimeType"))
            assert "<tds:DaylightSavings>true</tds:DaylightSavings>" in xml
            assert "<tt:TZ>EST5EDT</tt:TZ>" in xml
            if self.mode == "Manual":
                fields = {i.tag.rsplit("}", 1)[-1]: i.text for i in root.iter()}
                self.clock = datetime(
                    *(
                        int(fields[name])
                        for name in ("Year", "Month", "Day", "Hour", "Minute", "Second")
                    ),
                    tzinfo=UTC,
                )
        return ET.fromstring("<Reply/>")


def _camera_source():
    return SimpleNamespace(
        binding=SimpleNamespace(device_id=uuid4()),
        device=SimpleNamespace(
            protocols={DeviceProtocol.ONVIF}, host="192.0.2.2", management_port=80, tags=set()
        ),
        source_uri="rtsp://private-user:private-password@192.0.2.2/main",
    )


def _connect_camera(monkeypatch, value):
    import k5vision.operator_time_admin as admin

    def client(host, username, password, *args, **kwargs):
        assert (host, username, password) == ("192.0.2.2", "private-user", "private-password")
        return value

    monkeypatch.setattr(admin, "_Client", client)


def test_camera_ntp_configuration_reads_back_without_claiming_ntp_accuracy(monkeypatch):
    camera = _Camera()
    _connect_camera(monkeypatch, camera)
    result = _camera_operation(
        _camera_source(), "configure", {"from_dhcp": False, "ntp_servers": ["time.example"]}, 15
    )
    assert result["ntp"]["mode"] == "ntp"
    assert result["ntp"]["servers"] == ["time.example"]
    assert result["operation"] == {"action": "configure", "status": "configured", "verified": False}
    assert result["absolute_sync_verified"] is False
    assert "private-password" not in json.dumps(result)


def test_camera_host_copy_preserves_timezone_and_verifies_read_back(monkeypatch):
    camera = _Camera()
    _connect_camera(monkeypatch, camera)
    result = _camera_operation(_camera_source(), "synchronize", {}, 15)
    assert result["operation"]["verified"] is True
    assert result["clock"]["reported_mode"] == "manual"
    assert result["absolute_sync_verified"] is False
    assert abs(result["clock"]["offset_seconds"]) <= 1


def test_camera_ignored_clock_write_cannot_be_marked_synchronized(monkeypatch):
    camera = _Camera(ignore_write=True)
    _connect_camera(monkeypatch, camera)
    result = _camera_operation(_camera_source(), "synchronize", {}, 15)
    assert result["operation"]["verified"] is False
    assert result["operation"]["status"] == "completed"


def test_direct_rtsp_stream_cannot_receive_onvif_clock_writes():
    source = _camera_source()
    source.device.protocols = {DeviceProtocol.RTSP}
    with pytest.raises(OnvifError) as error:
        _camera_operation(source, "synchronize", {}, 15)
    assert error.value.code == "unsupported"


def test_explicit_management_credentials_replace_delegated_stream_account_only_for_call(
    monkeypatch,
):
    import k5vision.operator_time_admin as admin

    camera, source = _Camera(), _camera_source()
    seen = []

    def client(host, username, password, *args, **kwargs):
        seen.append((username, password))
        return camera

    monkeypatch.setattr(admin, "_Client", client)
    result = _camera_operation(
        source,
        "synchronize",
        {"username": "management-admin", "password": "private-management-secret"},
        15,
    )
    assert seen == [("management-admin", "private-management-secret")]
    assert source.source_uri == "rtsp://private-user:private-password@192.0.2.2/main"
    assert "private-management-secret" not in json.dumps(result)


def test_host_cancellation_retains_capacity_until_real_worker_finishes(private_profile):
    async def exercise():
        entered, finish = threading.Event(), threading.Event()

        class Host:
            def status(self):
                entered.set()
                assert finish.wait(5)
                return {"host": "complete"}

        service = TimeAdministrationService(private_profile, host=Host())
        first = asyncio.create_task(service.host_status())
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        with pytest.raises(TimeAdministrationError, match="^time_busy$"):
            await service.host_status()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await service.aclose()
        with pytest.raises(TimeAdministrationError, match="^time_unavailable$"):
            await service.host_status()

    asyncio.run(exercise())


def test_camera_cancellation_keeps_mutation_lease_until_physical_operation_finishes(
    private_profile,
):
    async def exercise():
        entered, finish = asyncio.Event(), asyncio.Event()
        mutations = asyncio.Lock()

        class Connector:
            async def _run(self, function, *args):
                entered.set()
                await finish.wait()
                return {"finished": True}

        service = TimeAdministrationService(private_profile, connector=Connector())

        async def operation():
            async with mutations:
                return await service.camera(_camera_source(), "synchronize")

        first = asyncio.create_task(operation())
        await entered.wait()
        first.cancel()
        await asyncio.sleep(0)
        assert mutations.locked() and not first.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not mutations.locked()
        await service.aclose()

    asyncio.run(exercise())


def test_ntp_parent_kills_and_reaps_a_stalled_dns_probe(monkeypatch):
    import subprocess

    import k5vision.operator_native_process as native
    import k5vision.operator_time_admin as admin

    events = []

    class Job:
        def assign(self, process):
            events.append("assigned")

        def close(self):
            events.append("job_closed")

    class Process:
        returncode = None

        def communicate(self, request, *, timeout):
            assert timeout == 5
            assert json.loads(request) == {"server": "time.example"}
            events.append("request_sent")
            raise subprocess.TimeoutExpired("owned probe", timeout)

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("killed")
            self.returncode = -1

        def wait(self, *, timeout):
            assert timeout == 3
            events.append("reaped")

    monkeypatch.setattr(native, "_WindowsJob", Job)
    monkeypatch.setattr(admin.subprocess, "Popen", lambda *args, **kwargs: Process())
    assert measure_reference("time.example") == unavailable_reference()
    assert (
        events[-2:] == ["killed", "reaped"]
        if os.name != "nt"
        else events[-3:] == ["killed", "reaped", "job_closed"]
    )
    if os.name == "nt":
        assert events.index("assigned") < events.index("request_sent")


def test_actual_sntp_datagrams_are_read_without_changing_any_clock():
    import time

    wire = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wire.bind(("127.0.0.1", 0))
    wire.settimeout(2)
    port = wire.getsockname()[1]
    failures = []

    def server():
        try:
            request, peer = wire.recvfrom(512)
            received = time.time()
            response = bytearray(48)
            response[0], response[1] = (4 << 3) | 4, 2
            response[24:32] = request[40:48]
            response[32:40], response[40:48] = _stamp(received), _stamp(time.time())
            wire.sendto(response, peer)
        except Exception as error:
            failures.append(type(error).__name__)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    try:
        result = measure("127.0.0.1", port=port)
        assert result["status"] == "measured" and result["sync_verified"] is True
        assert abs(result["offset_seconds"]) < 0.1
    finally:
        thread.join(timeout=3)
        wire.close()
    assert not thread.is_alive() and not failures
