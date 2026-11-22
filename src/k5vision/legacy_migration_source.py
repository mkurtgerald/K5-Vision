"""Authenticated old-user migration controller for the native graphical broker.

The fixed native entry point takes only its creator's public process identity.
Credentials, service-protection requests and admitted copy instructions remain
inside the owned private pipe. It never activates a site itself.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from k5vision import identity_state
from k5vision import legacy_identity_snapshot as snapshot
from k5vision import legacy_media_stage as media
from k5vision.legacy_migration_authorization import (
    LegacyMigrationAuthorizationError,
    LegacyMigrationAuthorizer,
)
from k5vision.legacy_migration_wire import NativeParentChannel
from k5vision.operator_profile_lock import OperatorProfileLock


def _response(status, **fields):
    return {"schema_version": 1, "status": status, **fields}


def _known_legacy_identity() -> Path:
    if os.name != "nt":
        raise ValueError
    shell, ole = ctypes.WinDLL("shell32", use_last_error=True), ctypes.WinDLL("ole32")
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_int32
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    identifier = ctypes.create_string_buffer(UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091").bytes_le)
    pointer = ctypes.c_void_p()
    try:
        if (
            shell.SHGetKnownFolderPath(identifier, 0, None, ctypes.byref(pointer))
            or not pointer.value
        ):
            raise ValueError
        root = Path(ctypes.wstring_at(pointer))
        return identity_state._checked_path(root / "K5VisionOperatorState" / "identity")
    finally:
        if pointer.value:
            ole.CoTaskMemFree(pointer)


class _Broker:
    def __init__(self, channel, permit):
        self.channel, self.permit = channel, permit
        self.binding = None
        self.last_progress = -1.0
        self.phase = "checking_site"

    def authority(self):
        self.channel.assert_current()
        self.permit.assert_current(site_id=self.permit.site_id, transfer_id=self.permit.transfer_id)
        now = time.monotonic()
        if now - self.last_progress >= 1:
            reply = self.channel.exchange(
                {"schema_version": 1, "operation": "progress", "phase": self.phase}
            )
            if reply != _response("ok"):
                raise ValueError
            self.last_progress = time.monotonic()

    def service(self, operation, **fields):
        self.authority()
        reply = self.channel.exchange(
            {
                "schema_version": 1,
                "operation": "service",
                "request": {"schema_version": 1, "operation": operation, **fields},
            }
        )
        if type(reply) is not dict or reply.get("status") not in {
            "ok",
            "working",
            "prepared",
            "activated",
        }:
            raise ValueError
        self.permit.assert_current(site_id=self.permit.site_id, transfer_id=self.permit.transfer_id)
        return reply

    def assert_authenticated(self, binding):
        self.authority()
        if self.binding is None:
            if (
                binding.site_id != self.permit.site_id
                or binding.transfer_id != self.permit.transfer_id
            ):
                raise ValueError
            self.service(
                "begin",
                binding={
                    "site_id": binding.site_id,
                    "transfer_id": binding.transfer_id,
                    "source_digest": binding.source_digest,
                },
            )
            self.binding = binding
        elif self.binding != binding:
            raise ValueError

    def protect(self, binding, purpose, secret):
        self.assert_authenticated(binding)
        reply = self.service(
            "protect", purpose=purpose, clear=base64.b64encode(secret).decode("ascii")
        )
        protected = base64.b64decode(reply["protected"], validate=True)
        if not protected.startswith(b"dpapi-v1:"):
            raise ValueError
        return protected

    def verify(self, binding, purpose, secret, protected):
        self.assert_authenticated(binding)
        reply = self.service(
            "verify",
            purpose=purpose,
            clear=base64.b64encode(secret).decode("ascii"),
            protected=base64.b64encode(protected).decode("ascii"),
        )
        if reply.get("verified") is not True:
            raise ValueError

    def copy(self, source, entry):
        from k5vision.legacy_migration_copy import copy_to_service_stage

        self.authority()
        metadata = {
            "area": entry["section"],
            "relative": entry["relative"],
            "size": entry["bytes"],
            "sha256": entry["sha256"],
            "mtime_ns": entry["modified_ns"],
        }
        copy_to_service_stage(source, metadata, self.permit.transfer_id, checkpoint=self.authority)
        reply = self.service("entry", entry=metadata)
        if reply != _response("ok"):
            raise ValueError
        self.permit.assert_current(site_id=self.permit.site_id, transfer_id=self.permit.transfer_id)


def _source_digest(source):
    files, _ = snapshot._inventory(source)
    import json

    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def run_source(channel, source: Path) -> int:
    authorizer = lease = permit = broker = None
    reason, successful = "migration_unavailable", False
    try:
        request = channel.receive()
        if (
            type(request) is not dict
            or set(request) != {"schema_version", "operation", "username", "password", "code"}
            or type(request["schema_version"]) is not int
            or request["schema_version"] != 1
            or request["operation"] != "authorize"
        ):
            raise ValueError
        source = identity_state._checked_path(source)
        LegacyMigrationAuthorizer.preflight(source)
        lease = OperatorProfileLock(source)
        try:
            lease.acquire()
        except Exception:
            reason = "close_previous_app"
            raise
        transfer = str(uuid4())
        authorizer = LegacyMigrationAuthorizer(
            source, lease=lease, transfer_id=transfer, permit_seconds=86400
        )
        try:
            permit = authorizer.authorize(request["username"], request["password"], request["code"])
        except LegacyMigrationAuthorizationError:
            reason = "authentication_not_accepted"
            raise
        finally:
            request["password"] = request["code"] = None
        broker = _Broker(channel, permit)
        broker.authority()
        stage_parent = source.parent / "migration-prepared"
        if not stage_parent.exists():
            stage_parent.mkdir(mode=0o700)
        identity_state._same_directory(stage_parent, stage_parent.lstat())
        stage = stage_parent / transfer
        stage.mkdir(mode=0o700)
        prepared = stage / "identity"
        broker.phase = "copying_accounts"
        receipt = snapshot.prepare_identity_snapshot(
            source, prepared, lease=lease, sink=broker, transfer_id=transfer
        )
        broker.service("snapshot", receipt=receipt)
        broker.phase = "checking_recordings"
        inventory = media.inspect_media_source(
            source,
            lease=lease,
            transfer_id=transfer,
            source_digest=receipt["source_digest"],
            authority_check=broker.authority,
            max_seconds=86400,
        )
        broker.service(
            "capacity",
            bytes=inventory.total_bytes
            + 2 * sum(entry["bytes"] for entry in receipt["staged_files"].values())
            + 64 * 1024**2,
            reserve=inventory.minimum_free_bytes,
        )
        broker.phase = "copying_accounts"
        for index, (name, expected) in enumerate(sorted(receipt["staged_files"].items())):
            path = snapshot._staged_path(prepared, name)
            broker.copy(
                path,
                {
                    "index": index,
                    "section": "identity",
                    "relative": path.name,
                    "bytes": expected["bytes"],
                    "sha256": expected["sha256"],
                    "modified_ns": path.stat().st_mtime_ns,
                },
            )
        marker = prepared / "migration-snapshot.json"
        marker_digest = snapshot._digest(marker)
        broker.copy(
            marker,
            {
                "index": len(receipt["staged_files"]),
                "section": "identity",
                "relative": marker.name,
                "bytes": marker_digest["bytes"],
                "sha256": marker_digest["sha256"],
                "modified_ns": marker.stat().st_mtime_ns,
            },
        )
        broker.phase = "copying_recordings"
        media.copy_media_source(
            inventory, lease=lease, authority_check=broker.authority, copy_entry=broker.copy
        )
        broker.authority()
        if _source_digest(source) != receipt["source_digest"]:
            raise ValueError
        broker.phase = "verifying_copy"
        reply = broker.service("prepare")
        while reply["status"] == "working":
            time.sleep(0.25)
            reply = broker.service("status")
        if reply["status"] != "prepared":
            raise ValueError
        broker.authority()

        def final():
            return media._checkpoint(
                inventory.identity, inventory.source_root, lease, inventory.deadline
            )

        # No external progress or protection callback follows this final source
        # seal. The already prepared service copy requires an explicit commit.
        media._validate_inventory(inventory, lease, final)
        if _source_digest(source) != receipt["source_digest"]:
            raise ValueError
        permit.assert_current(site_id=permit.site_id, transfer_id=permit.transfer_id)
        channel.assert_current()
        reply = channel.exchange(
            {
                "schema_version": 1,
                "operation": "service",
                "request": {"schema_version": 1, "operation": "commit"},
            }
        )
        while type(reply) is dict and reply.get("status") == "working":
            time.sleep(0.25)
            channel.assert_current()
            reply = channel.exchange(
                {
                    "schema_version": 1,
                    "operation": "service",
                    "request": {"schema_version": 1, "operation": "status"},
                }
            )
        if type(reply) is not dict or reply.get("status") != "activated":
            raise ValueError
        successful = True
    except Exception:
        if broker is not None and broker.binding is not None:
            try:
                channel.exchange(
                    {
                        "schema_version": 1,
                        "operation": "service",
                        "request": {"schema_version": 1, "operation": "cancel"},
                    }
                )
            except Exception:
                pass
    finally:
        if permit is not None:
            permit.close()
        if authorizer is not None:
            authorizer.close()
        if lease is not None:
            lease.close()
    channel.send(
        {
            "schema_version": 1,
            "operation": "result",
            "status": "ready" if successful else "refused",
            "reason": "migration_complete" if successful else reason,
        }
    )
    return 0 if successful else 1


def main():
    channel = None
    try:
        if len(sys.argv) != 3 or any(
            not value.isascii() or not value.isdecimal() for value in sys.argv[1:]
        ):
            raise ValueError
        channel = NativeParentChannel(int(sys.argv[1]), int(sys.argv[2]))
        return run_source(channel, _known_legacy_identity())
    except Exception:
        return 1
    finally:
        if channel is not None:
            channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
