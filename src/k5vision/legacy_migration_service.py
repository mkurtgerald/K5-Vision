"""Private service-token migration actor launched only by the native host.

Long validation runs in a cancellable worker while the fixed native parent can
poll status. Publication always requires a separate, freshly authorized commit.
No source file path or application credential is accepted by this actor.
"""

from __future__ import annotations

import base64
import shutil
import sys
import threading

from k5vision.legacy_migration_target import TargetBinding, TargetMigration, recover_pending
from k5vision.legacy_migration_wire import NativeParentChannel
from k5vision.service_setup import _layout


def _response(status, **fields):
    return {"schema_version": 1, "status": status, **fields}


class _Admission:
    def __init__(self, channel, binding):
        self.channel, self.binding = channel, binding

    def assert_authenticated(self, binding):
        self.channel.assert_current()
        if binding != self.binding:
            raise ValueError


class MigrationService:
    """One process, one native parent, one transfer, one owned target lease."""

    def __init__(self, channel, root):
        self.channel, self.root = channel, root
        self.target = self.thread = None
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._result = None

    def _stop(self):
        self.channel.assert_current()
        if self.stopping.is_set():
            raise ValueError
        return False

    def _prepare(self):
        try:
            self.target.prepare_activation(stop=self._stop)
            reply = _response("prepared", progress=100)
        except Exception:
            reply = _response("refused", reason="migration_unavailable")
        with self._lock:
            self._result = reply

    def _commit(self):
        try:
            self.target.commit(stop=self._stop)
            reply = _response("activated", progress=100)
        except Exception:
            reply = _response("refused", reason="migration_unavailable")
        with self._lock:
            self._result = reply

    def _status(self):
        if self.target is None:
            raise ValueError
        with self._lock:
            if self._result is not None:
                return dict(self._result)
        if self.thread is not None:
            return _response("working", progress=self.target.status().get("progress", 0))
        status = self.target.status()
        return _response(
            "activated" if status["status"] == "committed" else "ok",
            progress=status.get("progress", 0),
        )

    def handle(self, request):
        self.channel.assert_current()
        if (
            type(request) is not dict
            or type(request.get("schema_version")) is not int
            or request["schema_version"] != 1
            or type(request.get("operation")) is not str
        ):
            raise ValueError
        operation = request["operation"]
        fields = {
            "begin": {"binding"},
            "protect": {"purpose", "clear"},
            "verify": {"purpose", "clear", "protected"},
            "snapshot": {"receipt"},
            "entry": {"entry"},
            "capacity": {"bytes", "reserve"},
            "prepare": set(),
            "status": set(),
            "commit": set(),
            "cancel": set(),
            "rollback": set(),
        }
        if (
            operation not in fields
            or set(request) != {"schema_version", "operation"} | fields[operation]
        ):
            raise ValueError
        if operation == "begin":
            if self.target is not None or self.stopping.is_set():
                raise ValueError
            binding = TargetBinding(**request["binding"])
            admission = _Admission(self.channel, binding)
            recover_pending(self.root, binding, admission)
            self.target = TargetMigration(self.root, binding, admission)
            self.target.begin()
            return _response("ok")
        if self.target is None:
            raise ValueError
        if operation == "status":
            return self._status()
        if operation == "cancel":
            self.stopping.set()
            self.target.cancel()
            return _response("ok")
        if self.stopping.is_set():
            raise ValueError
        if operation == "rollback":
            if self.thread is not None and self.thread.is_alive():
                raise ValueError
            self.target.rollback()
            return _response("ok")
        if operation == "commit":
            if (
                self.thread is None
                or self.thread.is_alive()
                or self._status()["status"] != "prepared"
            ):
                raise ValueError
            with self._lock:
                self._result = None
            self.thread = threading.Thread(
                target=self._commit, name="k5-migration-commit", daemon=True
            )
            self.thread.start()
            return _response("working", progress=100)
        if self.thread is not None:
            raise ValueError
        if operation == "capacity":
            needed, reserve = request["bytes"], request["reserve"]
            if (
                type(needed) is not int
                or not 0 <= needed <= 4100 * 1024**3
                or type(reserve) is not int
                or not 0 <= reserve <= 4096 * 1024**3
            ):
                raise ValueError
            free = shutil.disk_usage(self.root).free
            if type(free) is not int or free < needed + reserve:
                raise ValueError
            return _response("ok")
        if operation in {"protect", "verify"}:
            clear = base64.b64decode(request["clear"], validate=True)
            if len(clear) > 16384:
                raise ValueError
            if operation == "protect":
                protected = self.target.protect(request["purpose"], clear)
                return _response("ok", protected=base64.b64encode(protected).decode("ascii"))
            protected = base64.b64decode(request["protected"], validate=True)
            if len(protected) > 65545:
                raise ValueError
            self.target.verify(request["purpose"], clear, protected)
            return _response("ok", verified=True)
        if operation == "snapshot":
            self.target.set_snapshot(request["receipt"])
        elif operation == "entry":
            self.target.record_entry(request["entry"])
        elif operation == "prepare":
            self.thread = threading.Thread(
                target=self._prepare, name="k5-migration-prepare", daemon=True
            )
            self.thread.start()
            return _response("working", progress=0)
        return _response("ok")

    def close(self):
        self.stopping.set()
        if self.target is not None:
            self.target.cancel()
        if self.thread is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                # The native parent's Job is the final bound for disk I/O.
                return
        if self.target is not None:
            self.target.close()


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    channel = actor = None
    try:
        if len(arguments) != 2:
            raise ValueError
        channel = NativeParentChannel(int(arguments[0]), int(arguments[1]), service=True)
        actor = MigrationService(channel, _layout().root)
        while True:
            request = channel.receive()
            try:
                reply = actor.handle(request)
            except Exception:
                reply = _response("refused", reason="migration_unavailable")
            channel.send(reply)
            if reply["status"] in {"refused", "activated"} or request.get("operation") == "cancel":
                return 0 if reply["status"] == "activated" else 1
    except Exception:
        return 1
    finally:
        if actor is not None:
            actor.close()
        if channel is not None:
            channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
