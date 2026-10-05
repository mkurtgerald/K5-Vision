"""Synthetic portable retention tests; no native queries or package commands.

Run independently: python -I -S -B tests/test_installer_wheel_storage.py
"""

from __future__ import annotations

import ast
import ctypes
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts/installer_wheel_storage.py"
SPEC = importlib.util.spec_from_file_location("wheel_storage", SOURCE)
assert SPEC is not None and SPEC.loader is not None
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)
SHA = "a" * 40
PRINCIPAL = "S-1-5-21-1000"


class FakePolicy:
    """Portable fault injection only, never a production admission fallback."""

    principal = PRINCIPAL

    def __init__(self):
        self.total = 100 * 1024**3
        self.free = 50 * 1024**3
        self.queries = 0
        self.capacity_hook = None
        self.publish_hook = None
        self.denied = None
        self.volume_denied = False
        self.publications = 0

    def volume(self, path):
        if self.volume_denied:
            raise storage.StorageError("storage_volume")

    def admit(self, path, *, owned=False, ancestor=False):
        if path == self.denied:
            raise storage.StorageError("storage_acl")

    def capacity(self, path):
        self.queries += 1
        if self.capacity_hook is not None:
            self.capacity_hook(self.queries)
        return self.total, self.free

    def publish(self, source, destination, *, expected_source, expected_parent):
        if self.publish_hook is not None:
            self.publish_hook(source, destination)
        if storage._identity(source.lstat()) != expected_source:
            raise storage.StorageError("storage_identity")
        if storage._identity(source.parent.lstat()) != expected_parent:
            raise storage.StorageError("storage_identity")
        if os.path.lexists(destination):
            raise storage.StorageError("storage_exists")
        # Test-only portable replacement: production uses Windows' no-replace
        # rename. This check is not presented as POSIX concurrency admission.
        source.rename(destination)
        self.publications += 1


def provenance(**changes):
    return {
        "schema_version": "k5-native-wheel-provenance-v1",
        "scope": "artifact-capture-only",
        "installer_accepted": False,
        **changes,
    }


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.work = self.base / "actual-work-root"
        self.runner = self.work / "K5-Vision"
        self.workspace = self.runner / "K5-Vision"
        self.runner_temp = self.work / "_temp"
        self.workspace.mkdir(parents=True)
        self.runner_temp.mkdir()
        self.policy = FakePolicy()
        self.root = self.work / "k5-qualification-artifacts" / "K5-Vision"
        self.records = self.make_records(3)

    def make_records(self, count):
        result = []
        for index in range(count):
            path = self.runner_temp / f"package_{index}-1.0-py3-none-any.whl"
            payload = f"generated synthetic archive {index}".encode()
            path.write_bytes(payload)
            group = ("wheelhouse", "built-wheels", "ensurepip")[index % 3]
            result.append(
                {
                    "source": path,
                    "relative_path": f"{group}/{path.name}",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        return result

    def retain(self, **changes):
        arguments = {
            "storage_root": self.root,
            "run_id": "100",
            "attempt": "2",
            "revision": SHA,
            "records": self.records,
            "provenance": provenance(),
            "policy": self.policy,
        }
        arguments.update(changes)
        return storage.retain_bundle(**arguments)

    def fails(self, code=None, **changes):
        with self.assertRaises(storage.StorageError) as caught:
            self.retain(**changes)
        error = caught.exception
        if code:
            self.assertEqual(code, error.code)
        self.assertEqual(error.code, str(error))
        self.assertEqual(error.code, error.reason_code)
        self.assertNotIn(str(self.base), str(error))
        self.assertEqual({"reason_code", "cleanup_pending"}, error.report.keys())
        return error

    def partial(self):
        return next(self.root.glob(".partial-*"))

    def test_derive_uses_actual_layout_without_mutation(self):
        derived = storage.derive_storage_root(
            self.runner, self.workspace, self.runner_temp, self.policy
        )
        self.assertEqual(self.root, derived)
        self.assertFalse(derived.parent.exists())
        self.assertEqual(0, self.policy.queries)

    def test_layout_rejects_mismatched_runner_workspace_temp_or_repo(self):
        other = self.work / "other"
        other.mkdir()
        for runner, workspace, temporary in (
            (self.runner, other, self.runner_temp),
            (self.runner, self.workspace, other),
            (other, self.workspace, self.runner_temp),
        ):
            with self.subTest(runner=runner, workspace=workspace, temporary=temporary):
                with self.assertRaises(storage.StorageError) as caught:
                    storage.derive_storage_root(runner, workspace, temporary, self.policy)
                self.assertEqual("storage_layout", caught.exception.code)

    def test_derivation_refuses_linked_ancestor(self):
        alias = self.base / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        with self.assertRaisesRegex(storage.StorageError, "storage_path"):
            storage.derive_storage_root(
                alias / "K5-Vision", alias / "K5-Vision/K5-Vision", alias / "_temp", self.policy
            )

    def test_policy_failure_is_closed_and_path_free(self):
        self.policy.denied = self.work
        self.fails("storage_acl")
        self.assertFalse(self.root.parent.exists())
        self.policy.denied = None
        self.policy.volume_denied = True
        self.fails("storage_volume")

    def test_storage_root_acl_path_context_is_fixed_and_read_only(self):
        for path, role, context, distance in (
            (self.runner, "runner_workspace", "runner_workspace", None),
            (self.workspace, "workspace", "workspace", None),
            (self.runner_temp, "runner_temp", "runner_temp", None),
            (self.work, "ancestor", "runner_workspace", 1),
        ):
            with self.subTest(role=role):
                self.policy.denied = path
                with self.assertRaises(storage.StorageError) as caught:
                    storage.derive_storage_root(
                        self.runner, self.workspace, self.runner_temp, self.policy
                    )
                diagnostic = caught.exception.acl_diagnostic
                self.assertEqual(role, diagnostic["path_role"])
                self.assertEqual(context, diagnostic["path_context"])
                self.assertEqual(distance, diagnostic["ancestor_distance"])
                self.assertNotIn(str(self.base), json.dumps(diagnostic))
                self.assertFalse(self.root.parent.exists())

    def test_retains_exact_36_once_and_subset_only_by_reference(self):
        self.records = self.make_records(36)
        subset = [record["relative_path"] for record in self.records[:30]]
        document = provenance(
            installer_subset=subset, qualification={"repository": "mkurtgerald/K5-Vision"}
        )
        result = self.retain(provenance=document)
        directory = self.root / result["bundle_key"]
        complete_path = self.root / result["complete_relative_path"]
        receipt_path = self.root / result["receipt_relative_path"]
        complete = json.loads(complete_path.read_bytes())
        self.assertEqual(36, len(list(directory.rglob("*.whl"))))
        self.assertEqual(36, result["archive_count"])
        self.assertEqual(sum(record["size"] for record in self.records), result["archive_bytes"])
        self.assertEqual(document, json.loads(receipt_path.read_bytes()))
        self.assertEqual(
            result["receipt_sha256"], hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        )
        self.assertEqual(
            result["complete_sha256"], hashlib.sha256(complete_path.read_bytes()).hexdigest()
        )
        self.assertTrue(complete["capture_only"])
        self.assertTrue(complete["requires_successful_final_run_and_receipts"])
        self.assertEqual(result["receipt_sha256"], complete["provenance_sha256"])
        self.assertNotIn(str(self.base), receipt_path.read_text() + complete_path.read_text())
        self.assertFalse(result["cleanup_pending"])
        for marker in self.root.parent.rglob("OWNER.json"):
            raw = marker.read_text()
            self.assertNotIn(PRINCIPAL, raw)
            self.assertEqual(
                hashlib.sha256(PRINCIPAL.encode()).hexdigest(),
                json.loads(raw)["principal_fingerprint"],
            )
        self.assertEqual(1, self.policy.publications)
        self.assertFalse(list(self.root.glob(".partial-*")))
        for record in self.records:
            captured = directory / record["relative_path"]
            self.assertEqual(record["source"].read_bytes(), captured.read_bytes())
            self.assertNotEqual(record["source"].stat().st_ino, captured.stat().st_ino)

    def test_complete_is_last_created_file_before_publish(self):
        events = []
        original = storage._Owned.file

        def observe(owner, path, payload=None):
            events.append(path.name)
            return original(owner, path, payload)

        self.policy.publish_hook = lambda source, destination: events.append("published")
        with mock.patch.object(storage._Owned, "file", observe):
            self.retain()
        self.assertEqual(["COMPLETE.json", "published"], events[-2:])

    def test_second_bundle_reuses_owned_namespace_and_preserves_first(self):
        first = self.retain()
        path = self.root / first["receipt_relative_path"]
        old = path.read_bytes()
        second = self.retain(attempt="3")
        self.assertNotEqual(first["bundle_key"], second["bundle_key"])
        self.assertEqual(old, path.read_bytes())

    def test_existing_run_key_is_never_replaced(self):
        result = self.retain()
        path = self.root / result["complete_relative_path"]
        original = path.read_bytes()
        self.fails("storage_exists")
        self.assertEqual(original, path.read_bytes())

    def test_publication_collision_preserves_foreign_destination(self):
        def collide(source, destination):
            destination.mkdir()
            (destination / "foreign.txt").write_text("do not alter")

        self.policy.publish_hook = collide
        error = self.fails("storage_exists")
        self.assertTrue(error.cleanup_pending)
        foreign = self.root / f"100-2-{SHA}" / "foreign.txt"
        self.assertEqual("do not alter", foreign.read_text())

    def test_unknown_namespace_is_never_adopted_or_cleaned(self):
        self.root.parent.mkdir()
        foreign = self.root.parent / "other-work"
        foreign.write_text("untouched")
        self.fails("storage_namespace")
        self.assertEqual("untouched", foreign.read_text())
        self.assertEqual([foreign], list(self.root.parent.iterdir()))

    def test_wrong_owner_marker_is_rejected_without_changes(self):
        self.retain()
        marker = self.root / "OWNER.json"
        marker.write_text('{"principal":"foreign"}')
        self.fails(attempt="3")
        self.assertEqual('{"principal":"foreign"}', marker.read_text())

    def test_missing_project_marker_is_not_recreated(self):
        self.retain()
        marker = self.root / "OWNER.json"
        marker.unlink()
        self.fails("storage_namespace", attempt="3")
        self.assertFalse(marker.exists())

    def test_storage_symlink_is_rejected_without_following(self):
        self.root.parent.mkdir()
        target = self.base / "foreign"
        target.mkdir()
        sentinel = target / "keep"
        sentinel.write_text("keep")
        self.root.symlink_to(target, target_is_directory=True)
        self.fails()
        self.assertEqual("keep", sentinel.read_text())

    def test_record_shape_limits_and_duplicate_relative_paths(self):
        invalid = [
            [],
            self.records * 13,
            [{**self.records[0], "extra": "secret"}],
            [{**self.records[0], "size": True}],
            [{**self.records[0], "size": 0}],
            [{**self.records[0], "sha256": "A" * 64}],
            [self.records[0], self.records[0]],
        ]
        for records in invalid:
            with self.subTest(records=str(records)[:80]):
                self.fails("storage_records", records=records)
        self.assertFalse(self.root.parent.exists())

    def test_total_size_is_bounded_before_reading(self):
        records = [{**record, "size": storage.MAX_ARCHIVE_BYTES} for record in self.records[:2]]
        self.fails("storage_limit", records=records)
        self.assertFalse(self.root.parent.exists())

    def test_run_and_revision_are_fixed_safe_identifiers(self):
        for field, value in [
            ("run_id", "../100"),
            ("run_id", 100),
            ("attempt", "0"),
            ("revision", "b" * 39),
            ("revision", "C" * 40),
            ("attempt", "2\nsecret"),
        ]:
            with self.subTest(field=field, value=value):
                self.fails("storage_records", **{field: value})

    def test_source_path_and_relative_path_rejections(self):
        for relative in (
            "../x.whl",
            "/wheelhouse/x.whl",
            "wheelhouse/../x.whl",
            "wheelhouse/nested/x.whl",
            "wheelhouse\\x.whl",
            "other/x.whl",
            "wheelhouse/CON.whl",
            "wheelhouse/x.whl:stream",
            "wheelhouse/x.whl ",
            "wheelhouse/x.txt",
            "wheelhouse/é.whl",
        ):
            with self.subTest(relative=relative):
                self.fails(
                    "storage_records", records=[{**self.records[0], "relative_path": relative}]
                )
        for source in (
            Path("relative.whl"),
            self.runner_temp / "../escape.whl",
            Path("//server/share/archive.whl"),
            self.runner_temp / "NUL",
        ):
            with self.subTest(source=source):
                self.fails("storage_path", records=[{**self.records[0], "source": source}])

    def test_case_alias_duplicate_relative_path_is_refused(self):
        first = self.records[0]
        records = [
            first,
            {
                **self.records[1],
                "relative_path": first["relative_path"].replace("package", "PACKAGE"),
            },
        ]
        self.fails("storage_records", records=records)

    def test_source_link_hardlink_and_nonregular_file_are_refused(self):
        record = self.records[0]
        alias_directory = self.runner_temp / "alias"
        alias_directory.mkdir()
        alternate = alias_directory / record["source"].name
        alternate.symlink_to(record["source"])
        self.fails("storage_path", records=[{**record, "source": alternate}])
        alternate.unlink()
        os.link(record["source"], alternate)
        self.fails("storage_path", records=[record])
        alternate.unlink()
        alternate.mkdir()
        self.fails("storage_path", records=[{**record, "source": alternate}])

    def test_nonwheel_source_cannot_be_disguised_as_archive(self):
        source = self.runner_temp / "private.txt"
        source.write_bytes(self.records[0]["source"].read_bytes())
        self.fails("storage_records", records=[{**self.records[0], "source": source}])
        self.assertFalse(self.root.parent.exists())

    def test_wrong_hash_or_size_refused_before_creating_namespace(self):
        for changes in ({"sha256": "0" * 64}, {"size": self.records[0]["size"] + 1}):
            self.fails("storage_hash", records=[{**self.records[0], **changes}])
            self.assertFalse(self.root.parent.exists())

    def test_provenance_schema_secrets_paths_urls_and_nonjson_are_refused(self):
        invalid = [
            {},
            provenance(scope="installer-ready"),
            provenance(installer_accepted=True),
            provenance(unexpected="value"),
            provenance(qualification={"source_path": "x"}),
            provenance(qualification={"revision": "/home/private"}),
            provenance(qualification={"revision": "at /home/private"}),
            provenance(qualification={"revision": "C:\\secret"}),
            provenance(qualification={"revision": "https://private.example"}),
            provenance(qualification={"revision": "../secret"}),
            provenance(qualification={"revision": Path("private")}),
            provenance(qualification={"password": "private"}),
            provenance(qualification={"count": 1.1}),
            provenance(qualification={"count": -1}),
        ]
        for document in invalid:
            with self.subTest(document=document):
                self.fails("storage_provenance", provenance=document)
        self.assertFalse(self.root.parent.exists())

    def test_quoted_bracketed_and_embedded_absolute_paths_are_refused(self):
        for text in (
            "missing; sys_platform == '/home/private-account/project'",
            'missing; sys_platform == "/home/private"',
            "[/home/private]",
            "(/home/private)",
            "{/home/private}",
            "path=/home/private",
            "prefix:'C:/private'",
            "['\\\\server\\share']",
        ):
            with self.subTest(text=text):
                self.fails(
                    "storage_provenance", provenance=provenance(qualification={"value": text})
                )

    def test_provenance_depth_size_and_cycles_are_bounded(self):
        deep = {}
        current = deep
        for _ in range(20):
            current["nested"] = {}
            current = current["nested"]
        self.fails("storage_limit", provenance=provenance(qualification=deep))
        cyclic = {}
        cyclic["cycle"] = cyclic
        self.fails("storage_limit", provenance=provenance(qualification=cyclic))
        self.fails("storage_limit", provenance=provenance(wheels=["x"] * 4097))
        self.fails("storage_limit", provenance=provenance(wheels=["x" * 4096] * 100))

    def test_subset_must_reference_retained_unique_paths(self):
        for subset in (
            ["wheelhouse/missing.whl"],
            [self.records[0]["relative_path"]] * 2,
            [{"path": "wheelhouse/x.whl"}],
            "wheelhouse/x.whl",
        ):
            self.fails("storage_provenance", provenance=provenance(installer_subset=subset))

    def test_capacity_requires_exact_bytes_and_five_percent_headroom(self):
        required = sum(record["size"] for record in self.records)
        reserve = (self.policy.total + 19) // 20
        self.policy.free = required + reserve - 1
        self.fails("storage_capacity")
        self.assertFalse(self.root.parent.exists())
        self.policy.free += 1
        self.retain()
        self.assertGreater(self.policy.queries, len(self.records))

    def test_capacity_requires_two_gib_minimum(self):
        self.policy.total = 10 * 1024**3
        self.policy.free = storage.MIN_FREE_BYTES + sum(r["size"] for r in self.records) - 1
        self.fails("storage_capacity")
        self.assertFalse(self.root.parent.exists())

    def test_invalid_capacity_or_volume_change_fails_closed(self):
        self.policy.free = self.policy.total + 1
        self.fails("storage_capacity")
        self.policy.free = 50 * 1024**3
        self.policy.queries = 0

        def change_volume(number):
            if number == 2:
                self.policy.total += 1

        self.policy.capacity_hook = change_volume
        error = self.fails("storage_capacity")
        self.assertTrue(error.cleanup_pending)
        self.assertTrue(self.root.parent.exists())

    def test_disk_filling_retains_partial_and_preserves_prior_bundle(self):
        first = self.retain()
        keep = self.root / first["complete_relative_path"]
        expected = keep.read_bytes()
        self.policy.queries = 0

        def exhaust(number):
            if number == 2:
                self.policy.free = 0

        self.policy.capacity_hook = exhaust
        error = self.fails("storage_capacity", attempt="3")
        self.assertTrue(error.cleanup_pending)
        self.assertEqual(expected, keep.read_bytes())
        self.assertTrue(list(self.root.glob(".partial-*")))

    def test_source_replacement_after_preflight_is_refused(self):
        source = self.records[0]["source"]

        def replace_source(number):
            if number == 2:
                original = source.read_bytes()
                source.rename(source.with_suffix(".old"))
                source.write_bytes(original)

        self.policy.capacity_hook = replace_source
        error = self.fails("storage_identity")
        self.assertTrue(error.cleanup_pending)
        self.assertTrue(self.root.parent.exists())

    def test_source_mutation_after_preflight_is_refused(self):
        source = self.records[0]["source"]

        def change_source(number):
            if number == 2:
                source.write_bytes(b"x" * self.records[0]["size"])

        self.policy.capacity_hook = change_source
        self.fails("storage_hash")
        self.assertTrue(self.root.parent.exists())

    def test_unknown_insert_during_failure_is_preserved_with_owner(self):
        def inject(number):
            if number == 2:
                (self.partial() / "foreign.txt").write_text("other job")
                raise OSError("sensitive raw path /not-for-output")

        self.policy.capacity_hook = inject
        error = self.fails("storage_io")
        self.assertTrue(error.cleanup_pending)
        partial = self.partial()
        self.assertEqual("other job", (partial / "foreign.txt").read_text())
        self.assertTrue((partial / "OWNER.json").is_file())
        self.assertFalse((partial / "COMPLETE.json").exists())

    @contextmanager
    def tracked_output_streams(self):
        streams = {}
        original = storage._Owned.file

        def capture(owned, path, payload=None):
            result = original(owned, path, payload)
            if payload is None:
                streams[path] = result
            return result

        with mock.patch.object(storage._Owned, "file", new=capture):
            yield streams

    def test_replaced_partial_file_is_not_deleted(self):
        sentinel = self.base / "sentinel"
        sentinel.write_text("foreign")
        injected = []
        original_seal = storage._Owned.seal

        with self.tracked_output_streams() as streams:

            def inject(owned, destination):
                if destination in streams:
                    # Real production stream closure, not a mocked close flag.
                    self.assertTrue(streams[destination].closed)
                    injected.append(destination)
                    destination.unlink()
                    destination.symlink_to(sentinel)
                return original_seal(owned, destination)

            with mock.patch.object(storage._Owned, "seal", new=inject):
                error = self.fails("storage_path")
        self.assertEqual(1, len(injected))
        self.assertTrue(error.cleanup_pending)
        self.assertEqual("foreign", sentinel.read_text())
        self.assertTrue(injected[0].is_symlink())
        self.assertFalse((self.partial() / "COMPLETE.json").exists())
        self.assertEqual(0, self.policy.publications)

    def test_closed_destination_identity_replacement_preserves_both_files(self):
        injected = []
        original_seal = storage._Owned.seal

        with self.tracked_output_streams() as streams:

            def inject(owned, destination):
                if destination in streams:
                    self.assertTrue(streams[destination].closed)
                    original = destination.read_bytes()
                    prior = destination.with_suffix(".prior")
                    destination.rename(prior)
                    destination.write_bytes(b"foreign replacement")
                    injected.append((destination, prior, original))
                return original_seal(owned, destination)

            with mock.patch.object(storage._Owned, "seal", new=inject):
                error = self.fails("storage_identity")
        self.assertEqual(1, len(injected))
        destination, prior, original = injected[0]
        self.assertTrue(error.cleanup_pending)
        self.assertEqual(original, prior.read_bytes())
        self.assertEqual(b"foreign replacement", destination.read_bytes())
        self.assertNotEqual(storage._identity(prior.stat()), storage._identity(destination.stat()))
        self.assertFalse((self.partial() / "COMPLETE.json").exists())
        self.assertEqual(0, self.policy.publications)

    def test_open_destination_delete_boundary_is_fail_closed(self):
        sentinel = self.base / "sentinel"
        sentinel.write_text("foreign")
        attempts, blocked = [], []

        with self.tracked_output_streams() as streams:

            def inject(number):
                if number == 2:
                    destination = next(self.partial().rglob("*.whl"))
                    self.assertFalse(streams[destination].closed)
                    attempts.append(
                        (
                            destination,
                            storage._identity(destination.stat()),
                            destination.read_bytes(),
                        )
                    )
                    try:
                        destination.unlink()
                    except PermissionError as error:
                        # Only the exact Windows sharing refusal is expected.
                        # Another I/O error must not satisfy this regression.
                        self.assertEqual("nt", os.name)
                        self.assertEqual(32, error.winerror)
                        blocked.append("unlink-sharing-violation")
                        raise
                    destination.symlink_to(sentinel)

            self.policy.capacity_hook = inject
            error = self.fails("storage_io" if os.name == "nt" else "storage_path")
        self.assertEqual(1, len(attempts))
        destination, identity, original = attempts[0]
        self.assertTrue(streams[destination].closed)
        self.assertTrue(error.cleanup_pending)
        self.assertEqual("foreign", sentinel.read_text())
        if os.name == "nt":
            self.assertEqual(["unlink-sharing-violation"], blocked)
            self.assertFalse(destination.is_symlink())
            self.assertEqual(identity, storage._identity(destination.stat()))
            self.assertEqual(original, destination.read_bytes())
        else:
            self.assertEqual([], blocked)
            self.assertTrue(destination.is_symlink())
        self.assertFalse((self.partial() / "COMPLETE.json").exists())
        self.assertEqual(0, self.policy.publications)

    def test_post_hash_destination_change_prevents_publication(self):
        self.records = self.records[:1]

        def change(number):
            if number == 3:
                destination = self.partial() / self.records[0]["relative_path"]
                destination.write_bytes(b"z" * self.records[0]["size"])

        self.policy.capacity_hook = change
        error = self.fails("storage_identity")
        self.assertTrue(error.cleanup_pending)
        self.assertEqual(0, self.policy.publications)

    def test_locked_cleanup_reports_pending_and_preserves_owner_marker(self):
        def fail(number):
            if number == 2:
                raise OSError("copy error")

        self.policy.capacity_hook = fail
        original = Path.unlink

        def locked(path, *args, **kwargs):
            if path.suffix == ".whl":
                raise PermissionError("locked")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", locked):
            error = self.fails("storage_io")
        self.assertTrue(error.cleanup_pending)
        self.assertTrue((self.partial() / "OWNER.json").exists())

    def test_cleanup_never_uses_pathname_deletion(self):
        def fail(number):
            if number == 2:
                raise OSError("copy error")

        self.policy.capacity_hook = fail
        with mock.patch.object(Path, "unlink") as unlink, mock.patch.object(Path, "rmdir") as rmdir:
            error = self.fails("storage_io")
        self.assertTrue(error.cleanup_pending)
        unlink.assert_not_called()
        rmdir.assert_not_called()
        self.assertTrue((self.partial() / "OWNER.json").is_file())

    def test_cleanup_replacement_after_check_is_preserved(self):
        owned = storage._Owned(self.policy)
        directory = self.base / "owned"
        owned.directory(directory)
        target = directory / "file.whl"
        owned.file(target, b"ours")
        owned.check(target)
        target.rename(directory / "moved-original.whl")
        target.write_bytes(b"foreign replacement")
        self.assertTrue(owned.cleanup())
        self.assertEqual(b"foreign replacement", target.read_bytes())
        self.assertEqual(b"ours", (directory / "moved-original.whl").read_bytes())

    def test_mkdir_then_identity_failure_reports_uncertain_retention(self):
        original = Path.lstat
        observed = []

        def fail_after_creation(path, *args, **kwargs):
            if path == self.root.parent and path.is_dir():
                observed.append(path)
                raise OSError("identity lookup failed")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", fail_after_creation):
            error = self.fails("storage_io")
        self.assertTrue(error.cleanup_pending)
        self.assertTrue(self.root.parent.is_dir())
        self.assertFalse((self.root.parent / "OWNER.json").exists())
        self.assertEqual(1, len(observed))

    def test_complete_allocation_cannot_consume_reserved_headroom(self):
        original = storage._Owned.file

        def allocate(owner, path, payload=None):
            result = original(owner, path, payload)
            if path.name == "COMPLETE.json":
                self.policy.free = (self.policy.total + 19) // 20 - 1
            return result

        with mock.patch.object(storage._Owned, "file", allocate):
            error = self.fails("storage_capacity")
        self.assertTrue(error.cleanup_pending)
        self.assertEqual(0, self.policy.publications)
        self.assertTrue((self.partial() / "COMPLETE.json").exists())

    def test_partial_replacement_at_publication_is_not_blessed(self):
        def replace(source, destination):
            source.rename(source.with_name(source.name + "-original"))
            source.mkdir()
            (source / "foreign.txt").write_text("other job")

        self.policy.publish_hook = replace
        error = self.fails("storage_identity")
        self.assertTrue(error.cleanup_pending)
        self.assertEqual(0, self.policy.publications)
        foreign = list(self.root.glob(".partial-*/foreign.txt"))
        self.assertEqual(1, len(foreign))
        self.assertEqual("other job", foreign[0].read_text())

    def test_false_policy_success_is_rejected_by_final_identity(self):
        def false_publish(source, destination, **identities):
            destination.mkdir()
            (destination / "foreign.txt").write_text("keep")

        self.policy.publish = false_publish
        error = self.fails("storage_identity")
        self.assertTrue(error.cleanup_pending)
        self.assertEqual("keep", (self.root / f"100-2-{SHA}" / "foreign.txt").read_text())

    def test_foreign_insertion_after_rename_prevents_success_and_is_preserved(self):
        original = self.policy.publish

        def publish_then_insert(source, destination, **identities):
            original(source, destination, **identities)
            (destination / "foreign.txt").write_text("preserve")

        self.policy.publish = publish_then_insert
        error = self.fails("storage_inventory")
        self.assertTrue(error.cleanup_pending)
        self.assertEqual("preserve", (self.root / f"100-2-{SHA}" / "foreign.txt").read_text())

    def test_reparse_and_zero_identity_are_refused(self):
        with self.assertRaisesRegex(storage.StorageError, "storage_path"):
            storage._ordinary(
                types.SimpleNamespace(st_mode=0o040700, st_file_attributes=0x400), directory=True
            )
        with self.assertRaisesRegex(storage.StorageError, "storage_identity"):
            storage._identity(types.SimpleNamespace(st_ino=0, st_dev=1))

    def test_interrupt_retains_invocation_and_reports_fixed_code(self):
        def interrupt(number):
            if number == 2:
                raise KeyboardInterrupt()

        self.policy.capacity_hook = interrupt
        error = self.fails("storage_interrupted")
        self.assertTrue(error.cleanup_pending)
        self.assertTrue(self.root.parent.exists())

    def test_caller_document_is_snapshotted_before_io(self):
        document = provenance(qualification={"run_id": "100"})

        def mutate(number):
            document["qualification"]["run_id"] = "/private"

        self.policy.capacity_hook = mutate
        result = self.retain(provenance=document)
        saved = json.loads((self.root / result["receipt_relative_path"]).read_bytes())
        self.assertEqual("100", saved["qualification"]["run_id"])

    def test_write_failure_does_not_publish_complete(self):
        with mock.patch.object(storage.os, "fsync", side_effect=OSError("private path")):
            error = self.fails("storage_io")
        self.assertTrue(error.cleanup_pending)
        self.assertTrue(self.root.parent.exists())

    def test_native_policy_is_unavailable_without_any_linux_native_query(self):
        if os.name == "nt":
            self.skipTest("This is an explicitly non-native portability assertion")
        with mock.patch.object(storage.shutil, "disk_usage") as disk:
            with self.assertRaisesRegex(storage.StorageError, "storage_policy"):
                storage.NativeStoragePolicy()
        disk.assert_not_called()

    def test_production_uses_no_package_network_hardlink_or_acl_write_apis(self):
        source = SOURCE.read_text()
        tree = ast.parse(source)
        imports = {
            name.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for name in node.names
        }
        self.assertFalse(imports & {"subprocess", "requests", "urllib", "socket", "pip"})
        for forbidden in (
            "os.link(",
            "os.replace(",
            "SetNamedSecurityInfo",
            "SetFileSecurity",
            "icacls",
            "rmtree(",
            ".unlink(",
            ".rmdir(",
            "pip install",
        ):
            self.assertNotIn(forbidden, source)


class NativePolicyModelTests(unittest.TestCase):
    """Exercise ACL decisions against inert SID/ACE models, never native APIs."""

    def setUp(self):
        self.owner_query = mock.patch.object(storage, "_token_owner", return_value=PRINCIPAL)
        self.owner_query.start()
        self.addCleanup(self.owner_query.stop)

    def policy(self, owner=PRINCIPAL, entries=()):
        policy = object.__new__(storage.NativeStoragePolicy)
        policy.principal = PRINCIPAL
        policy.default_owner = PRINCIPAL
        policy._native = types.SimpleNamespace(
            current_user=lambda: PRINCIPAL,
            descriptor=lambda path: (owner, entries),
            trusted_installer=lambda: False,
        )

        def expanded(mask):
            for generic, specific in (
                (0x80000000, 0x00120089),
                (0x40000000, 0x00120116),
                (0x20000000, 0x001200A0),
                (0x10000000, 0x001F01FF),
            ):
                if mask & generic:
                    mask = (mask & ~generic) | specific
            return mask

        policy._module = types.SimpleNamespace(_expanded_mask=expanded)
        return policy

    def ace(self, mask, sid="S-1-1-0", kind=0, flags=0):
        return types.SimpleNamespace(mask=mask, sid=sid, kind=kind, flags=flags)

    def test_public_read_allowed_but_every_foreign_mutation_is_rejected(self):
        self.policy(entries=[self.ace(0x80000000)]).admit(Path("unused"), owned=True)
        for mask in (
            2,
            4,
            0x10,
            0x40,
            0x100,
            0x10000,
            0x40000,
            0x80000,
            0x1000000,
            0x40000000,
            0x10000000,
        ):
            with self.subTest(mask=mask):
                with self.assertRaisesRegex(storage.StorageError, "storage_acl"):
                    self.policy(entries=[self.ace(mask)]).admit(Path("unused"), owned=True)

    def test_trusted_writes_owner_rights_and_creator_owner(self):
        for sid in (PRINCIPAL, "S-1-5-18", "S-1-5-32-544", "S-1-3-4"):
            self.policy(entries=[self.ace(0x10000000, sid)]).admit(Path("unused"), owned=True)
        self.policy(entries=[self.ace(0x10000000, "S-1-3-0", flags=0x0B)]).admit(Path("unused"))

    def test_foreign_owner_and_namespace_noncurrent_owner_rejected(self):
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            self.policy(owner="S-1-1-0").admit(Path("unused"))
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            self.policy(owner="S-1-5-18").admit(Path("unused"), owned=True)
        self.policy(owner="S-1-5-18").admit(Path("unused"), ancestor=True)

    def test_deny_does_not_cancel_foreign_allow_and_inherited_write_rejected(self):
        entries = [self.ace(2, kind=1), self.ace(2)]
        with self.assertRaisesRegex(storage.StorageError, "storage_acl"):
            self.policy(entries=entries).admit(Path("unused"), owned=True)
        with self.assertRaisesRegex(storage.StorageError, "storage_acl"):
            self.policy(entries=[self.ace(2, flags=0x0B)]).admit(Path("unused"), owned=True)

    def test_ancestor_create_alone_cannot_mutate_owned_child(self):
        self.policy(entries=[self.ace(2 | 4)]).admit(Path("unused"), ancestor=True)
        with self.assertRaisesRegex(storage.StorageError, "storage_acl"):
            self.policy(entries=[self.ace(0x40)]).admit(Path("unused"), ancestor=True)

    def test_acl_refusal_identifies_foreign_mutation_without_principal_or_path(self):
        policy = self.policy(entries=[self.ace(2)])
        with self.assertRaises(storage.StorageError) as caught:
            policy.admit(Path("PRIVATE_CANARY"))
        diagnostic = caught.exception.acl_diagnostic
        self.assertEqual("ace_policy", diagnostic["phase"])
        self.assertEqual("foreign_mutating_allow", diagnostic["reason"])
        self.assertNotIn("PRIVATE_CANARY", json.dumps(diagnostic))
        self.assertNotIn(PRINCIPAL, json.dumps(diagnostic))

    def test_changed_principal_and_unknown_native_error_are_sanitized(self):
        policy = self.policy()
        policy._native.current_user = lambda: "S-1-5-21-2000"
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            policy.admit(Path("unused"))
        policy = self.policy()
        policy._native.descriptor = mock.Mock(side_effect=OSError("C:\\private"))
        with self.assertRaisesRegex(storage.StorageError, "^storage_acl$"):
            policy.admit(Path("unused"))

    def test_verified_trusted_installer_is_only_allowed_for_ancestors(self):
        policy = self.policy(owner=storage._TRUSTED_INSTALLER)
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            policy.admit(Path("unused"), ancestor=True)
        policy._native.trusted_installer = lambda: True
        policy.admit(Path("unused"), ancestor=True)
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            policy.admit(Path("unused"), owned=True)
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            policy.admit(Path("unused"))

    def test_verified_token_default_owner_permits_elevated_admin_creation(self):
        policy = self.policy(owner="S-1-5-32-544")
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            policy.admit(Path("unused"), owned=True)
        policy.default_owner = "S-1-5-32-544"
        with mock.patch.object(storage, "_token_owner", return_value="S-1-5-32-544"):
            policy.admit(Path("unused"), owned=True)
        with self.assertRaisesRegex(storage.StorageError, "storage_ownership"):
            policy.admit(Path("unused"), owned=True)


class AclDiagnosticTests(unittest.TestCase):
    """Run the pinned descriptor parser with inert native-call fixtures."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "_acl_diagnostic_pinned_helper",
            SOURCE.parent.parent / "src/k5vision/windows_identity_security.py",
        )
        self.helper = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.helper
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(self.helper)
        owner_query = mock.patch.object(storage, "_token_owner", return_value=PRINCIPAL)
        owner_query.start()
        self.addCleanup(owner_query.stop)

    def policy(self, *, result=0, acl=b"\x02\0\x08\0\0\0\0\0", valid_acl=True):
        policy = object.__new__(storage.NativeStoragePolicy)
        policy.principal = policy.default_owner = PRINCIPAL
        policy._module = self.helper
        native = object.__new__(self.helper._NativeSecurity)
        buffer = ctypes.create_string_buffer(acl)
        calls = []

        def query(*args):
            calls.append(args)
            ctypes.cast(args[3], ctypes.POINTER(ctypes.c_void_p))[0] = 1
            ctypes.cast(args[5], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            ctypes.cast(args[7], ctypes.POINTER(ctypes.c_void_p))[0] = 2
            return result

        native.security = types.SimpleNamespace(
            GetNamedSecurityInfoW=query, IsValidAcl=lambda pointer: valid_acl
        )
        native.kernel = types.SimpleNamespace(LocalFree=mock.Mock())
        native.current_user = lambda: PRINCIPAL
        native.sid = lambda pointer: PRINCIPAL
        native.trusted_installer = lambda: False
        policy._native = native
        return policy, query, calls

    def refusal(self, policy):
        with self.assertRaises(storage.StorageError) as caught:
            policy.admit(Path("PRIVATE_CANARY"))
        self.assertEqual("storage_acl", caught.exception.code)
        self.assertNotIn("PRIVATE_CANARY", json.dumps(caught.exception.acl_diagnostic))
        self.assertNotIn(PRINCIPAL, json.dumps(caught.exception.acl_diagnostic))
        return caught.exception.acl_diagnostic

    def test_success_observes_original_query_once_and_leaves_callable_unchanged(self):
        policy, query, calls = self.policy()
        policy.admit(Path("PRIVATE_CANARY"))
        self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)
        self.assertEqual(1, len(calls))
        self.assertEqual(("PRIVATE_CANARY", 1, 5), calls[0][:3])
        self.assertIsNone(calls[0][4])
        self.assertIsNone(calls[0][6])
        self.assertEqual(1, policy._native.kernel.LocalFree.call_count)

    def test_interleaved_descriptor_calls_never_mutate_or_mix_native_observers(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        policy, query, calls = self.policy()
        barrier = Barrier(2)

        def simultaneous(*args):
            self.assertIs(simultaneous, policy._native.security.GetNamedSecurityInfoW)
            query(*args)
            barrier.wait(timeout=5)
            return 5 if args[0] == "PRIVATE_CANARY_A" else 0

        policy._native.security.GetNamedSecurityInfoW = simultaneous

        def admit(name):
            try:
                policy.admit(Path(name))
            except storage.StorageError as error:
                return error.acl_diagnostic
            return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            refused, allowed = workers.map(admit, ("PRIVATE_CANARY_A", "PRIVATE_CANARY_B"))
        self.assertEqual("access_denied", refused["native_error"])
        self.assertEqual("native_query", refused["reason"])
        self.assertIsNone(allowed)
        self.assertEqual(2, len(calls))
        self.assertIs(simultaneous, policy._native.security.GetNamedSecurityInfoW)

    def test_read_only_native_library_never_requires_attribute_mutation(self):
        class ReadOnlySecurity:
            def __init__(self, query):
                object.__setattr__(self, "GetNamedSecurityInfoW", query)
                object.__setattr__(self, "IsValidAcl", lambda pointer: True)

            def __setattr__(self, name, value):
                raise AssertionError("Native library must remain unchanged")

        for outcome in ("success", "native_error", "parser_error", "os_error", "interrupt"):
            with self.subTest(outcome=outcome):
                policy, query, calls = self.policy(
                    result=5 if outcome == "native_error" else 0,
                    acl=b"\x03\0\x08\0\0\0\0\0"
                    if outcome == "parser_error"
                    else b"\x02\0\x08\0\0\0\0\0",
                )
                invoked = []

                def api(*args, outcome=outcome, invoked=invoked, query=query):
                    invoked.append(args)
                    if outcome == "os_error":
                        raise OSError("PRIVATE_CANARY")
                    if outcome == "interrupt":
                        raise KeyboardInterrupt
                    return query(*args)

                library = ReadOnlySecurity(api)
                policy._native.security = library
                if outcome == "success":
                    policy.admit(Path("PRIVATE_CANARY"))
                elif outcome == "interrupt":
                    with self.assertRaises(KeyboardInterrupt):
                        policy.admit(Path("PRIVATE_CANARY"))
                else:
                    self.refusal(policy)
                self.assertEqual(1, len(invoked))
                self.assertIs(library, policy._native.security)
                self.assertIs(api, library.GetNamedSecurityInfoW)

    def test_native_query_failure_categories_keep_original_failure_and_single_call(self):
        for status, category in (
            (2, "path_missing"),
            (3, "path_missing"),
            (5, "access_denied"),
            (87, "invalid_parameter"),
            (122, "insufficient_buffer"),
            (1336, "invalid_acl"),
            (1338, "invalid_security_descriptor"),
            (99999, "other_error"),
        ):
            with self.subTest(category=category):
                policy, query, calls = self.policy(result=status)
                diagnostic = self.refusal(policy)
                self.assertEqual("descriptor_query", diagnostic["phase"])
                self.assertEqual("native_query", diagnostic["reason"])
                self.assertEqual(category, diagnostic["native_error"])
                self.assertEqual("GetNamedSecurityInfoW", diagnostic["native_call"])
                self.assertEqual(1, len(calls))
                self.assertEqual(("PRIVATE_CANARY", 1, 5), calls[0][:3])
                self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)

    def test_real_parser_refusal_is_not_reported_as_unsafe_allow_or_query_failure(self):
        policy, query, calls = self.policy(acl=b"\x03\0\x08\0\0\0\0\0")
        diagnostic = self.refusal(policy)
        self.assertEqual("descriptor_parse", diagnostic["reason"])
        self.assertEqual("success", diagnostic["native_error"])
        self.assertEqual(1, len(calls))
        self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)

    def test_descriptor_validation_and_owner_sid_errors_have_distinct_fixed_reasons(self):
        policy, query, calls = self.policy(valid_acl=False)
        self.assertEqual("descriptor_validation", self.refusal(policy)["reason"])
        self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)
        policy, query, calls = self.policy()
        policy._native.sid = mock.Mock(
            side_effect=self.helper.WindowsIdentitySecurityError(
                "invalid native Windows security identifier"
            )
        )
        self.assertEqual("owner_sid_parse", self.refusal(policy)["reason"])
        self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)

    def test_unknown_and_spoofed_helper_messages_never_escape(self):
        for error in (
            self.helper.WindowsIdentitySecurityError("PRIVATE_CANARY"),
            ValueError("unsupported Windows identity ACL"),
        ):
            policy, query, calls = self.policy()
            policy._native.sid = mock.Mock(side_effect=error)
            self.assertEqual("unknown", self.refusal(policy)["reason"])
            self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)

    def test_native_api_exception_and_interruption_restore_without_retry(self):
        for error in (OSError("PRIVATE_CANARY"), KeyboardInterrupt()):
            policy, query, calls = self.policy()
            original = mock.Mock(side_effect=error)
            policy._native.security.GetNamedSecurityInfoW = original
            if isinstance(error, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    policy.admit(Path("PRIVATE_CANARY"))
            else:
                diagnostic = self.refusal(policy)
                self.assertEqual("unknown", diagnostic["reason"])
                self.assertEqual("not_observed", diagnostic["native_error"])
            self.assertEqual(1, original.call_count)
            self.assertIs(original, policy._native.security.GetNamedSecurityInfoW)

    def test_principal_token_owner_and_trusted_installer_failures_keep_fixed_phase(self):
        for phase in ("principal_query", "token_owner_query", "trusted_installer_query"):
            policy, query, calls = self.policy()
            if phase == "principal_query":
                policy._native.current_user = mock.Mock(side_effect=OSError("PRIVATE_CANARY"))
            elif phase == "trusted_installer_query":
                policy._native.sid = lambda pointer: storage._TRUSTED_INSTALLER
                policy._native.trusted_installer = mock.Mock(side_effect=OSError("PRIVATE_CANARY"))
            token = (
                mock.patch.object(storage, "_token_owner", side_effect=OSError("PRIVATE_CANARY"))
                if phase == "token_owner_query"
                else nullcontext()
            )
            with token:
                with self.assertRaises(storage.StorageError) as caught:
                    policy.admit(Path("PRIVATE_CANARY"), ancestor=True)
            self.assertEqual(phase, caught.exception.acl_diagnostic["phase"])
            self.assertEqual("unknown", caught.exception.acl_diagnostic["reason"])
            self.assertIs(query, policy._native.security.GetNamedSecurityInfoW)

    def test_diagnostic_projection_refuses_arbitrary_fields_types_and_values(self):
        values = {key: "PRIVATE_CANARY" for key in storage._ACL_DIAGNOSTIC_ENUMS}
        values.update(ancestor_distance=999, path="PRIVATE_CANARY", sid=PRINCIPAL)
        diagnostic = storage.bounded_acl_diagnostic(values)
        self.assertEqual(
            set(storage._ACL_DIAGNOSTIC_ENUMS) | {"schema_version", "ancestor_distance"},
            diagnostic.keys(),
        )
        self.assertNotIn("PRIVATE_CANARY", json.dumps(diagnostic))
        self.assertNotIn(PRINCIPAL, json.dumps(diagnostic))
        self.assertIsNone(diagnostic["ancestor_distance"])
        for bad in (None, [], "PRIVATE_CANARY"):
            self.assertIsNone(storage.bounded_acl_diagnostic(bad))


class NativePublicationModelTests(unittest.TestCase):
    def test_rename_buffer_is_relative_no_replace_and_utf16(self):
        leaf = f"100-2-{SHA}"
        buffer = storage._rename_information(999, leaf)
        header = storage._RenameInformation.from_buffer(buffer)
        self.assertEqual(0, header.flags)
        self.assertEqual(999, header.root)
        self.assertEqual(len(leaf.encode("utf-16-le")), header.name_length)
        raw = bytes(buffer)[storage._RenameInformation.name.offset :]
        self.assertEqual(leaf, raw.decode("utf-16-le"))

    def test_publication_uses_both_bound_handles_and_no_replace(self):
        policy = object.__new__(storage.NativeStoragePolicy)
        source, destination = Path("/owned/partial"), Path(f"/owned/100-2-{SHA}")
        events = []

        @contextmanager
        def locked(path, expected, *, access, sharing):
            events.append(("open", path, expected, access, sharing))
            yield 100 if path == source.parent else 200
            events.append(("close", path))

        def rename(handle, info_class, buffer, size):
            header = storage._RenameInformation.from_buffer(buffer)
            events.append(("rename", handle, info_class, header.root, header.flags))
            return True

        policy._locked_directory = locked
        policy._native = types.SimpleNamespace(
            kernel=types.SimpleNamespace(SetFileInformationByHandle=rename)
        )
        with (
            mock.patch.object(storage.os, "name", "nt"),
            mock.patch.object(storage, "_same"),
            mock.patch.object(storage.os.path, "lexists", return_value=False),
        ):
            policy.publish(source, destination, expected_source=(1, 2), expected_parent=(1, 3))
        self.assertEqual(("open", source.parent, (1, 3), 0x84, 3), events[0])
        self.assertEqual(("open", source, (1, 2), 0x10080, 1), events[1])
        self.assertEqual(("rename", 200, 3, 100, 0), events[2])
        self.assertEqual(("close", source), events[3])
        self.assertEqual(("close", source.parent), events[4])

    def test_locked_directory_binds_open_identity_and_closes_transferred_handle(self):
        policy = object.__new__(storage.NativeStoragePolicy)
        opened, closed_native, closed_fd = [], [], []

        def create(*arguments):
            opened.append(arguments)
            return 123

        policy._native = types.SimpleNamespace(
            kernel=types.SimpleNamespace(CreateFileW=create, CloseHandle=closed_native.append)
        )
        policy._open_osfhandle = lambda handle, flags: 456
        info = types.SimpleNamespace(st_dev=1, st_ino=2, st_mode=0o040700, st_file_attributes=0)
        with (
            mock.patch.object(storage.os, "O_BINARY", 0, create=True),
            mock.patch.object(storage.os, "fstat", return_value=info),
            mock.patch.object(storage.os, "close", side_effect=closed_fd.append),
            mock.patch.object(storage, "_same"),
        ):
            with policy._locked_directory(
                Path("/owned/partial"), (1, 2), access=0x10080, sharing=1
            ) as handle:
                self.assertEqual(123, handle)
        self.assertEqual(
            (str(Path("/owned/partial")), 0x10080, 1, None, 3, 0x02200000, None), opened[0]
        )
        self.assertEqual([456], closed_fd)
        self.assertEqual([], closed_native)

    def test_locked_directory_identity_failure_closes_without_mutation(self):
        policy = object.__new__(storage.NativeStoragePolicy)
        policy._native = types.SimpleNamespace(
            kernel=types.SimpleNamespace(CreateFileW=lambda *args: 123, CloseHandle=mock.Mock())
        )
        policy._open_osfhandle = lambda handle, flags: 456
        info = types.SimpleNamespace(st_dev=1, st_ino=99, st_mode=0o040700, st_file_attributes=0)
        with (
            mock.patch.object(storage.os, "O_BINARY", 0, create=True),
            mock.patch.object(storage.os, "fstat", return_value=info),
            mock.patch.object(storage.os, "close") as close,
        ):
            with self.assertRaisesRegex(storage.StorageError, "storage_identity"):
                with policy._locked_directory(
                    Path("/owned/partial"), (1, 2), access=0x10080, sharing=1
                ):
                    self.fail("Replaced directory was admitted")
        close.assert_called_once_with(456)
        policy._native.kernel.CloseHandle.assert_not_called()

    def test_handle_transfer_failure_closes_native_handle(self):
        policy = object.__new__(storage.NativeStoragePolicy)
        policy._native = types.SimpleNamespace(
            kernel=types.SimpleNamespace(CreateFileW=lambda *args: 123, CloseHandle=mock.Mock())
        )
        policy._open_osfhandle = mock.Mock(side_effect=OSError("transfer failed"))
        with mock.patch.object(storage.os, "O_BINARY", 0, create=True):
            with self.assertRaises(OSError):
                with policy._locked_directory(
                    Path("/owned/partial"), (1, 2), access=0x10080, sharing=1
                ):
                    self.fail("Transfer failure was ignored")
        policy._native.kernel.CloseHandle.assert_called_once_with(123)


class HashSnapshotModelTests(unittest.TestCase):
    """Model CPython 3.12 Windows path/handle clocks without native calls."""

    def snapshot(self, *, descriptor=False, **changes):
        return types.SimpleNamespace(
            **{
                "st_dev": 7,
                "st_ino": 11,
                "st_mode": 0o100600,
                "st_nlink": 1,
                "st_file_attributes": 0,
                "st_size": 3,
                "st_mtime_ns": 150,
                # CPython 3.12.10 lstat exposes CreationTime, fstat ChangeTime.
                "st_ctime_ns": 200 if descriptor else 100,
                "st_birthtime_ns": 100,
                **changes,
            }
        )

    def exercise(self, *, opened=None, descriptor_after=None, path_checked=None, path_after=None):
        before = self.snapshot()
        opened = opened or self.snapshot(descriptor=True)
        descriptor_after = descriptor_after or opened
        path_checked = path_checked or before
        path_after = path_after or before
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated.whl"
            path.write_bytes(b"abc")
            with (
                mock.patch.object(storage, "_checked", return_value=before),
                mock.patch.object(storage, "_same", side_effect=[path_checked, path_after]),
                mock.patch.object(storage.os, "fstat", side_effect=[opened, descriptor_after]),
            ):
                return storage._hash(path, 3, FakePolicy(), (7, 11))

    def test_stable_windows_split_ctime_semantics_are_admitted(self):
        digest, identity = self.exercise()
        self.assertEqual(hashlib.sha256(b"abc").hexdigest(), digest)
        self.assertEqual((7, 11), identity)

    def test_stable_same_ctime_semantics_are_admitted(self):
        digest, _ = self.exercise(opened=self.snapshot())
        self.assertEqual(hashlib.sha256(b"abc").hexdigest(), digest)

    def test_descriptor_metadata_changes_are_refused(self):
        for changes in (
            {"st_ctime_ns": 201},
            {"st_mtime_ns": 151},
            {"st_size": 4},
            {"st_birthtime_ns": 101},
            {"st_ino": 12},
            {"st_dev": 8},
            {"st_ino": 0},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(storage.StorageError, "storage_identity"):
                    self.exercise(descriptor_after=self.snapshot(descriptor=True, **changes))

    def test_path_metadata_changes_are_refused(self):
        for changes in (
            {"st_ctime_ns": 101},
            {"st_mtime_ns": 151},
            {"st_size": 4},
            {"st_birthtime_ns": 101},
            {"st_ino": 12},
            {"st_dev": 8},
            {"st_ino": 0},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(storage.StorageError, "storage_identity"):
                    self.exercise(path_after=self.snapshot(**changes))

    def test_path_mutation_between_admission_and_open_is_refused(self):
        for changes in (
            {"st_ctime_ns": 101},
            {"st_mtime_ns": 151},
            {"st_size": 4},
            {"st_birthtime_ns": 101},
            {"st_ino": 12},
            {"st_dev": 8},
            {"st_ino": 0},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(storage.StorageError, "storage_identity"):
                    self.exercise(path_checked=self.snapshot(**changes))

    def test_cross_api_shared_field_disagreement_is_refused(self):
        for changes in (
            {"st_mtime_ns": 151},
            {"st_size": 4},
            {"st_birthtime_ns": 101},
            {"st_ino": 12},
            {"st_dev": 8},
            {"st_ino": 0},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(storage.StorageError, "storage_identity"):
                    self.exercise(opened=self.snapshot(descriptor=True, **changes))

    def test_descriptor_reparse_or_hard_link_changes_are_refused(self):
        for changes in ({"st_file_attributes": 0x400}, {"st_nlink": 2}):
            for boundary in ("opened", "descriptor_after"):
                with self.subTest(changes=changes, boundary=boundary):
                    with self.assertRaisesRegex(storage.StorageError, "storage_path"):
                        self.exercise(**{boundary: self.snapshot(descriptor=True, **changes)})


class TokenOwnerModelTests(unittest.TestCase):
    def test_default_owner_query_is_read_only_bounded_and_closes_token(self):
        classes, handles = [], []

        def get_info(token, information_class, buffer, length, needed):
            classes.append(information_class)
            needed._obj.value = ctypes.sizeof(ctypes.c_void_p)
            if buffer is not None:
                ctypes.c_void_p.from_buffer(buffer).value = 1234
                return True
            return False

        def open_token(process, rights, token):
            self.assertEqual(0x0008, rights)
            token._obj.value = 5678
            return True

        native = types.SimpleNamespace(
            kernel=types.SimpleNamespace(
                GetCurrentProcess=lambda: -1, CloseHandle=lambda token: handles.append(token.value)
            ),
            security=types.SimpleNamespace(
                OpenProcessToken=open_token, GetTokenInformation=get_info
            ),
            sid=lambda pointer: PRINCIPAL if pointer == 1234 else "invalid",
        )
        self.assertEqual(PRINCIPAL, storage._token_owner(native))
        self.assertEqual([4, 4], classes)
        self.assertEqual([5678], handles)


if __name__ == "__main__":
    unittest.main()
