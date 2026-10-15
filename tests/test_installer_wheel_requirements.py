"""Portable, synthetic closure contracts; no wheel/package/native code is run.

Run without installed packages: python -I -S -B tests/test_installer_wheel_requirements.py
These checks are also collected by pytest when the development tools are present.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts/installer_wheel_requirements.py"
SPEC = importlib.util.spec_from_file_location("wheel_requirements", SOURCE)
assert SPEC is not None and SPEC.loader is not None
closure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(closure)


def environment(**changes: str) -> dict[str, str]:
    return {
        "python_version": "3.12",
        "python_full_version": "3.12.10",
        "implementation_version": "3.12.10",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "sys_platform": "win32",
        "os_name": "nt",
        "platform_system": "Windows",
        "platform_machine": "AMD64",
        "platform_release": "10",
        "platform_version": "10.0.26100",
        **changes,
    }


def wheel(name: str = "root", version: str = "1.0", *requirements: str) -> dict:
    return {
        "name": name,
        "version": version,
        "requires_python": ">=3.12,<3.13",
        "requires_dist": list(requirements),
    }


class ClosureTests(unittest.TestCase):
    def rejects(self, wheels: object, code: str, env: object = None) -> None:
        with self.assertRaises(closure.ClosureError) as raised:
            closure.verify_closure(wheels, environment() if env is None else env)
        self.assertEqual(code, raised.exception.code)
        self.assertEqual(code, raised.exception.reason_code)
        self.assertEqual(code, str(raised.exception))

    def matches(self, requirement: str, version: str = "1.2.3") -> dict:
        return closure.verify_closure(
            [wheel("root", "1", requirement), wheel("dep", version)], environment()
        )

    def test_complete_report_and_normalization(self) -> None:
        result = closure.verify_closure(
            [
                wheel("root", "1", "DEP_Name ( >= 01.2, < 2.0, )"),
                wheel("dep-name", "1.02.3"),
            ],
            environment(),
        )
        self.assertEqual(result["result"], "pass")
        self.assertEqual(result["selected_extras"], [])
        self.assertEqual(result["wheel_count"], 2)
        self.assertEqual(
            result["dependency_edges"],
            [
                {
                    "source": "root",
                    "target": "dep-name",
                    "specifier": "<2.0,>=1.2",
                    "requested_extras": [],
                    "marker_present": False,
                    "active": True,
                    "selected_version": "1.2.3",
                }
            ],
        )
        self.assertEqual(len(result["python_requirements"]), 2)

    def test_final_release_specifiers(self) -> None:
        for spec in (
            ">=1.2.3",
            ">1.2.2",
            "<=1.2.3",
            "<1.2.4",
            "==1.2.3.0",
            "!=1.2.4",
            "~=1.2",
            "~=1.2.0",
            "==1.2.*",
            "!=1.3.*",
            ">=1.2,!=1.2.2,<2",
        ):
            with self.subTest(spec=spec):
                self.assertEqual(self.matches("dep" + spec)["result"], "pass")
        for spec in (
            "<1.2.3",
            ">1.2.3",
            ">=1.2.4",
            "<=1.2.2",
            "==1.2",
            "!=1.2.3.0",
            "~=1.3",
            "~=1.2.4",
            "==1.3.*",
            "!=1.2.*",
            ">=1.2,<1.2.3",
        ):
            with self.subTest(spec=spec):
                self.rejects(
                    [wheel("root", "1", "dep" + spec), wheel("dep", "1.2.3")],
                    "dependency_version_mismatch",
                )

    def test_release_padding_and_compatible_precision(self) -> None:
        for spec in ("==1.0.0", "==1.0.0.*", "<=1.0", ">=1.0", "~=1.0.0"):
            self.assertEqual(self.matches("dep" + spec, "1")["result"], "pass")
        self.assertEqual(self.matches("dep~=1.2", "1.9")["result"], "pass")
        self.rejects(
            [wheel("root", "1", "dep~=1.2.0"), wheel("dep", "1.9")],
            "dependency_version_mismatch",
        )

    def test_local_version_semantics_and_analytics_version(self) -> None:
        sha = "a" * 40
        version = "0.0.0+g" + sha
        for spec in ("==0", "==0.0.*", ">=0", "<=0", "==" + version, "!=0+different"):
            self.assertEqual(self.matches("dep" + spec, version)["result"], "pass")
        for spec in (">0", "<0", "!=0", "==0+different"):
            self.rejects(
                [wheel("root", "1", "dep" + spec), wheel("dep", version)],
                "dependency_version_mismatch",
            )
        self.assertEqual(self.matches("dep==1+ABC.1", "1+abc_01")["result"], "pass")

    def test_missing_duplicate_and_invalid_distributions(self) -> None:
        self.rejects([wheel("root", "1", "dep")], "missing_dependency")
        self.rejects([wheel(), wheel()], "duplicate_distribution")
        self.rejects([wheel("Root_Name")], "invalid_name")
        self.rejects([wheel("bad/name")], "invalid_name")

    def test_requires_python_checks_every_distribution(self) -> None:
        incompatible = wheel("orphan")
        incompatible["requires_python"] = "<3.12"
        self.rejects([wheel(), incompatible], "requires_python_mismatch")
        for spec in ("", "==3.12.*", ">=3.12.1,!=3.12.9", "~=3.12.0"):
            value = wheel()
            value["requires_python"] = spec
            self.assertEqual(closure.verify_closure([value], environment())["result"], "pass")

    def test_python_and_os_markers_with_precedence(self) -> None:
        active = (
            'python_version >= "3.12"',
            'python_full_version == "3.12.*"',
            'implementation_version > "3.12.9"',
            '"3.11" < python_version',
            '"win32" == sys_platform',
            'os_name == "nt"',
            'platform_system == "Windows"',
            'platform_machine == "AMD64"',
            'platform_python_implementation == "CPython"',
            'implementation_name == "cpython"',
            'sys_platform in "win32,darwin"',
            '"win" in sys_platform',
            'sys_platform not in "linux,darwin"',
            'platform_release == "10.0"',
            'platform_version >= "10.0.26000"',
            '(sys_platform == "linux" or os_name == "nt") and python_version >= "3.12"',
            'sys_platform == "win32" or os_name == "posix" and python_version < "3"',
        )
        for marker in active:
            with self.subTest(marker=marker):
                result = self.matches("dep; " + marker)
                self.assertTrue(result["dependency_edges"][0]["active"])
                self.rejects([wheel("root", "1", "dep; " + marker)], "missing_dependency")
        inactive = (
            'sys_platform == "linux"',
            'python_version < "3.12"',
            '(sys_platform == "win32" or os_name == "posix") and python_version < "3"',
            'platform_machine == "amd64"',
            'platform_system != "Windows"',
        )
        for marker in inactive:
            result = closure.verify_closure(
                [wheel("root", "1", "absent; " + marker)], environment()
            )
            edge = result["dependency_edges"][0]
            self.assertFalse(edge["active"])
            self.assertIsNone(edge["selected_version"])
            self.assertTrue(edge["marker_present"])

    def test_extra_guards_and_active_requested_extras(self) -> None:
        for marker in ('extra == "test"', '"test" == extra', 'extra == "PDF_export"'):
            result = closure.verify_closure(
                [wheel("root", "1", "missing[test]>=1; " + marker)], environment()
            )
            self.assertFalse(result["dependency_edges"][0]["active"])
        self.rejects(
            [wheel("root", "1", 'dep[speed]; extra == "x" or os_name == "nt"'), wheel("dep")],
            "dependency_extras_unsupported",
        )
        for marker in ('extra != "test"', 'extra == ""', 'extra in "test"', 'extra >= "test"'):
            self.rejects([wheel("root", "1", "dep; " + marker)], "unsupported_extra_marker")

    def test_inactive_branches_never_hide_bad_syntax_or_semantics(self) -> None:
        cases = (
            (
                'missing @ https://example.invalid/private; extra == "x"',
                "direct_reference_unsupported",
            ),
            ('missing>=1rc1; extra == "x"', "unsupported_version"),
            ('missing[bad/name]; extra == "x"', "invalid_name"),
            ('missing; extra == "x" and unknown == "secret"', "unknown_marker"),
            ('missing; os_name == "nt" or unknown == "secret"', "unknown_marker"),
            ('missing; extra == "x" and python_version === "3.12"', "unsupported_specifier"),
            ('missing; extra == "x" and platform_machine < "x"', "unsupported_marker_comparison"),
            ('missing; extra == "x" and python_version < "3.14rc1"', "unsupported_version"),
            ('missing; extra == "x" and (os_name == "nt"', "invalid_marker"),
        )
        for requirement, code in cases:
            with self.subTest(requirement=requirement):
                self.rejects([wheel("root", "1", requirement)], code)

    def test_unsupported_versions_and_specifiers_fail_closed(self) -> None:
        for version in ("1!2", "1rc1", "1.post1", "1.dev1", "v1", "1-1", "", "1..0", "1+bad!"):
            with self.subTest(version=version):
                self.rejects([wheel(version=version)], "unsupported_version")
        for spec in ("===1", "~=1", ">=1+abc", "<1+abc", "~=1.2+abc", "==1+abc.*", ">=1.*"):
            with self.subTest(spec=spec):
                self.rejects([wheel("root", "1", "dep" + spec)], "unsupported_specifier")
        for requirement in (
            "dep()",
            "dep>=1,,<2",
            "dep>=1,,",
            "dep[]",
            "dep;",
            "dep; ()",
            "dep #x",
        ):
            with self.subTest(requirement=requirement):
                with self.assertRaises(closure.ClosureError):
                    closure.verify_closure([wheel("root", "1", requirement)], environment())

    def test_unsupported_markers_and_no_execution(self) -> None:
        markers = (
            'python_version in "3.12"',
            '"3.12" == "3.12"',
            "python_version == python_full_version",
            'os_name ~= "nt"',
            'os_name === "nt"',
            'python_version < "3.13" < "3.14"',
            'not os_name == "nt"',
            '__import__("os").system("false")',
            'os_name == "n\\t"',
            'extras == "test"',
            'dependency_groups == "test"',
        )
        for marker in markers:
            with self.subTest(marker=marker):
                with self.assertRaises(closure.ClosureError):
                    self.matches("dep; " + marker)
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imports = {
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertEqual(imports, {"__future__", "re"})
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "compile", "open", "__import__"}
                for node in ast.walk(tree)
            )
        )

    def test_explicit_environment_and_consistency(self) -> None:
        self.rejects([wheel()], "missing_environment", {})
        self.rejects([wheel()], "invalid_environment", environment(python_version="3.11"))
        self.rejects([wheel()], "invalid_environment", environment(python_full_version="3.12"))
        self.rejects([wheel()], "invalid_environment", environment(unknown="value"))
        self.rejects([wheel()], "selected_extras_unsupported", environment(extra="test"))
        minimal = {"python_version": "3.12", "python_full_version": "3.12.10"}
        self.assertEqual(closure.verify_closure([wheel()], minimal)["result"], "pass")
        self.rejects(
            [wheel("root", "1", 'missing; extra == "x" and os_name == "nt"')],
            "missing_environment",
            minimal,
        )

    def test_platform_numeric_and_string_distinction(self) -> None:
        self.assertEqual(self.matches('dep; platform_release < "9"')["result"], "pass")
        self.assertEqual(self.matches('dep; platform_release == "10.*"')["result"], "pass")
        env = environment(platform_release="custom")
        self.rejects(
            [wheel("root", "1", 'dep; platform_release == "other"')],
            "unsupported_version",
            env,
        )
        self.rejects(
            [wheel("root", "1", 'dep; platform_release == "v1.0"')],
            "unsupported_version",
            environment(platform_release="v1"),
        )
        self.rejects(
            [wheel("root", "1", 'dep; platform_release == "10rc1"')], "unsupported_version"
        )

    def test_untrusted_text_is_not_in_errors(self) -> None:
        secret = "private-secret-token"
        for requirement in (f"dep @ https://example.invalid/{secret}", f'dep; {secret} == "x"'):
            with self.assertRaises(closure.ClosureError) as raised:
                closure.verify_closure([wheel("root", "1", requirement)], environment())
            self.assertNotIn(secret, str(raised.exception))

    def test_shape_and_ascii_validation(self) -> None:
        for value, code in (
            ([], "invalid_inventory"),
            ({}, "invalid_inventory"),
            ([{}], "invalid_metadata"),
        ):
            self.rejects(value, code)
        for requirement in (None, 5, "dep\n", "dep\x00", "dép"):
            self.rejects([wheel("root", "1", requirement)], "invalid_requirement")
        invalid = wheel()
        invalid["requires_dist"] = ("dep",)
        self.rejects([invalid], "invalid_metadata")

    def test_depth_token_length_and_count_limits(self) -> None:
        atom = 'os_name == "posix"'
        marker = "(" * closure.MAX_MARKER_DEPTH + atom + ")" * closure.MAX_MARKER_DEPTH
        self.assertEqual(self.matches("dep; " + marker)["result"], "pass")
        self.rejects([wheel("root", "1", "dep; (" + marker + ")")], "resource_limit")
        many_tokens = " or ".join([atom] * 65)
        self.rejects([wheel("root", "1", "dep; " + many_tokens)], "resource_limit")
        self.rejects(
            [wheel("root", "1", "x" * (closure.MAX_REQUIREMENT_LENGTH + 1))], "resource_limit"
        )
        self.rejects([wheel("x" * (closure.MAX_NAME_LENGTH + 1))], "resource_limit")
        self.rejects([wheel(version="9" * 10)], "resource_limit")
        self.rejects([wheel(version="1." * 8 + "1")], "resource_limit")
        self.rejects([wheel(version="1+" + ".".join(["a"] * 9))], "resource_limit")
        self.rejects([wheel("root", "1", "dep" + ",".join([">=1"] * 33))], "resource_limit")
        self.rejects([wheel("root", "1", *(["root"] * 257))], "resource_limit")
        self.rejects([wheel("w" + str(index)) for index in range(129)], "resource_limit")
        self.rejects(
            [wheel("w" + str(index), "1", *(["w0"] * 256)) for index in range(17)],
            "resource_limit",
        )
        self.rejects([wheel()], "resource_limit", environment(platform_version="x" * 257))

    def test_independent_inventories_and_determinism(self) -> None:
        for count in (29, 36):
            wheels = [wheel("w" + str(index), "1", "w0") for index in range(count)]
            result = closure.verify_closure(wheels, environment())
            self.assertEqual(result["wheel_count"], count)
            self.assertEqual(result, closure.verify_closure(wheels[::-1], environment()))
        inputs = [wheel("root", "1", "dep"), wheel("dep")]
        self.assertEqual(closure.verify_closure(inputs, environment())["result"], "pass")
        self.rejects(inputs[:1], "missing_dependency")


if __name__ == "__main__":
    unittest.main()
