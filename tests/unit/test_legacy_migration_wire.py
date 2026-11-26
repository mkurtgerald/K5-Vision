import io
import json
import os
import struct

import pytest

from k5vision.legacy_migration_wire import (
    MAX_FRAME,
    MigrationWireError,
    NativeParentChannel,
    read_frame,
    write_frame,
)


class _Fragmented(io.BytesIO):
    def read(self, count=-1):
        return super().read(min(3, count))

    def write(self, value):
        return super().write(value[:3])


def _raw(payload):
    return io.BytesIO(struct.pack("<I", len(payload)) + payload)


def test_fragmented_private_frames_round_trip_without_dropping_or_replaying_bytes():
    stream = _Fragmented()
    first = {"schema_version": 1, "operation": "authorized", "display": "Imported camera\u2014one"}
    second = {"schema_version": 1, "operation": "progress", "copied_bytes": 2**40}
    write_frame(stream, first)
    write_frame(stream, second)
    stream.seek(0)
    assert read_frame(stream) == first
    assert read_frame(stream) == second
    with pytest.raises(MigrationWireError):
        read_frame(stream)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":1,"value":NaN}',
        b'{"schema_version":1,"value":Infinity}',
        b'{"schema_version":1,"value":-Infinity}',
        b'{"schema_version":1,"value":1e999}',
        b'{"schema_version":1,"value":-1e999}',
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1,"value":{"name":1,"name":2}}',
        b'{"schema_version":true}',
        b'{"schema_version":1.0}',
        b'{"schema_version":2}',
        b'[{"schema_version":1}]',
        b'{"schema_version":1,"value":"\xff"}',
        b'{"schema_version":1}{}',
    ],
)
def test_noncanonical_or_nonfinite_wire_documents_are_refused(payload):
    with pytest.raises(MigrationWireError, match="^Migration connection is unavailable$"):
        read_frame(_raw(payload))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x01",
        struct.pack("<I", 0),
        struct.pack("<I", MAX_FRAME + 1),
        struct.pack("<I", 5) + b"{}",
    ],
)
def test_truncated_and_oversized_frames_are_not_adopted(raw):
    with pytest.raises(MigrationWireError):
        read_frame(io.BytesIO(raw))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_writer_does_not_emit_nonfinite_json_or_a_partial_header(value):
    stream = io.BytesIO()
    with pytest.raises(MigrationWireError):
        write_frame(stream, {"schema_version": 1, "value": value})
    assert stream.getvalue() == b""


def test_writer_bounds_utf8_document_before_sending_any_private_data():
    stream = io.BytesIO()
    with pytest.raises(MigrationWireError):
        write_frame(stream, {"schema_version": 1, "value": "x" * MAX_FRAME})
    assert stream.getvalue() == b""


def test_nonprogressing_writer_fails_instead_of_spinning():
    class Blocked:
        def write(self, _value):
            return 0

    with pytest.raises(MigrationWireError):
        write_frame(Blocked(), {"schema_version": 1})


@pytest.mark.skipif(
    os.name == "nt", reason="Native parent/pipe admission requires its Windows fixture"
)
def test_a_portable_process_cannot_claim_a_native_migration_peer():
    with pytest.raises(MigrationWireError):
        NativeParentChannel(1, 1)


def test_large_integer_identifiers_are_not_rounded_through_json():
    birth = 134_397_788_621_806_095
    document = {"schema_version": 1, "created_100ns": birth}
    stream = io.BytesIO()
    write_frame(stream, document)
    stream.seek(0)
    assert read_frame(stream)["created_100ns"] == birth
    assert json.loads(stream.getvalue()[4:])["created_100ns"] == birth


def test_maximum_supported_oidc_clear_and_cipher_fit_the_bounded_migration_frame():
    import base64

    clear = ("\U0001f600" * 4096).encode()
    protected = b"dpapi-v1:" + b"x" * (65545 - 9)
    document = {
        "schema_version": 1,
        "operation": "service",
        "request": {
            "schema_version": 1,
            "operation": "verify",
            "purpose": "oidc-client-secret",
            "clear": base64.b64encode(clear).decode(),
            "protected": base64.b64encode(protected).decode(),
        },
    }
    stream = io.BytesIO()
    write_frame(stream, document)
    assert 32768 < len(stream.getvalue()) <= 131076
    stream.seek(0)
    assert read_frame(stream) == document
