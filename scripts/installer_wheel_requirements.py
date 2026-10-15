"""Bounded, stdlib-only static wheel dependency checks; never a resolver.

``verify_closure`` accepts metadata already read by the collector, not paths or
archives. It performs no I/O, imports no distributions, and executes no metadata.
Run it independently for each inventory whose closure is being claimed.

Supported subset of the PyPA version/dependency specifications:
* Final numeric releases (zero-padded comparison) and normalized local labels;
  ==, !=, >=, <=, >, <, ~=, comma conjunctions, and ==/!= release-prefix wildcards.
* Named requirements, optional parentheses around constraints, and extra lists.
  Requested dependency extras are syntactically checked even on inactive edges;
  an ACTIVE edge requesting extras is blocked because this contract selects none.
* Parenthesized markers with and/or precedence and one known variable per
  comparison. Python/implementation versions use the version operators above.
  OS, architecture and implementation-name strings support ==, !=, in, not in.
  platform_release/platform_version support string containment; their other
  comparisons require supported versions (no ambiguous fallback to strings).
* Nonempty ``extra == 'name'`` guards (either operand order) are false. Other
  extra operators and empty extra literals are blocked rather than assuming
  semantics that differ between metadata interpreters.

Unsupported syntax/semantics fail closed, including on inactive marker branches:
epochs, pre/post/dev releases, arbitrary equality, direct URLs, escapes, unknown
marker variables, variable-to-variable comparisons, version containment and
ordered string comparisons. This is deliberately NOT a full PEP 440/508 engine.
See https://packaging.python.org/en/latest/specifications/version-specifiers/
and https://packaging.python.org/en/latest/specifications/dependency-specifiers/.

Bounds: 128 wheels; 256 requirements per wheel; 4096 total edges; 4096 characters
per requirement; 2048 per constraint/marker; 32 specifiers; 256 marker tokens;
16 nested parentheses; 128-character names/versions; eight release/local parts;
nine digits per release part; 256 characters per environment value/literal.
Only fixed reason codes escape on rejection; raw metadata is never in errors.
"""

from __future__ import annotations

import re

MAX_WHEELS = 128
MAX_REQUIREMENTS_PER_WHEEL = 256
MAX_EDGES = 4096
MAX_REQUIREMENT_LENGTH = 4096
MAX_EXPRESSION_LENGTH = 2048
MAX_SPECIFIERS = 32
MAX_MARKER_TOKENS = 256
MAX_MARKER_DEPTH = 16
MAX_NAME_LENGTH = 128
MAX_VERSION_LENGTH = 128
MAX_VERSION_PARTS = 8
MAX_ENVIRONMENT_LENGTH = 256

_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_VERSION = re.compile(r"([0-9]+(?:\.[0-9]+)*)(?:\+([A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*))?")
_SPECIFIER = re.compile(r"(===|~=|==|!=|<=|>=|<|>)[ \t]*([^ \t]+)")
_TOKEN = re.compile(
    r"""[ \t]*(?:("[^"\\]*"|'[^'\\]*')|([A-Za-z_][A-Za-z0-9_]*)|(===|~=|==|!=|<=|>=|<|>|[()]))"""
)
_VERSION_FIELDS = frozenset({"python_version", "python_full_version", "implementation_version"})
_PLATFORM_VERSION_FIELDS = frozenset({"platform_release", "platform_version"})
_STRING_FIELDS = frozenset(
    {
        "os_name",
        "sys_platform",
        "platform_machine",
        "platform_system",
        "platform_python_implementation",
        "implementation_name",
    }
)
_FIELDS = _VERSION_FIELDS | _PLATFORM_VERSION_FIELDS | _STRING_FIELDS | {"extra"}


class ClosureError(ValueError):
    """A static verification rejection with a fixed, non-sensitive reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        self.code = reason_code
        super().__init__(reason_code)


def _text(value: object, limit: int, code: str) -> str:
    if type(value) is not str:
        raise ClosureError(code)
    if len(value) > limit:
        raise ClosureError("resource_limit")
    if any(not (32 <= ord(char) <= 126 or char == "\t") for char in value):
        raise ClosureError(code)
    return value


def _name(value: object) -> str:
    value = _text(value, MAX_NAME_LENGTH, "invalid_name")
    if _NAME.fullmatch(value) is None:
        raise ClosureError("invalid_name")
    return re.sub(r"[-_.]+", "-", value).lower()


class _Version:
    __slots__ = ("release", "local", "text")

    def __init__(self, value: str) -> None:
        value = _text(value, MAX_VERSION_LENGTH, "unsupported_version")
        match = _VERSION.fullmatch(value)
        if match is None:
            raise ClosureError("unsupported_version")
        release = match[1].split(".")
        local = re.split(r"[._-]", match[2].lower()) if match[2] else []
        if (
            len(release) > MAX_VERSION_PARTS
            or len(local) > MAX_VERSION_PARTS
            or any(len(part) > 9 for part in release)
        ):
            raise ClosureError("resource_limit")
        self.release = tuple(int(part) for part in release)
        # Numeric local segments compare numerically even for exact equality.
        self.local = tuple(str(int(part)) if part.isdigit() else part for part in local)
        self.text = ".".join(map(str, self.release))
        if self.local:
            self.text += "+" + ".".join(self.local)


def _release_cmp(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    left += (0,) * (size - len(left))
    right += (0,) * (size - len(right))
    return (left > right) - (left < right)


class _Clause:
    __slots__ = ("operator", "version", "wildcard", "text")

    def __init__(self, operator: str, value: str) -> None:
        if operator not in {"==", "!=", ">=", "<=", ">", "<", "~="}:
            raise ClosureError("unsupported_specifier")
        self.operator = operator
        self.wildcard = value.endswith(".*")
        self.version = _Version(value[:-2] if self.wildcard else value)
        if self.wildcard and (operator not in {"==", "!="} or self.version.local):
            raise ClosureError("unsupported_specifier")
        if self.version.local and operator not in {"==", "!="}:
            raise ClosureError("unsupported_specifier")
        if operator == "~=" and len(self.version.release) < 2:
            raise ClosureError("unsupported_specifier")
        self.text = operator + self.version.text + (".*" if self.wildcard else "")

    def matches(self, candidate: _Version) -> bool:
        comparison = _release_cmp(candidate.release, self.version.release)
        if self.wildcard:
            count = len(self.version.release)
            padded = candidate.release + (0,) * max(0, count - len(candidate.release))
            equal = padded[:count] == self.version.release
        else:
            equal = comparison == 0 and (
                not self.version.local or candidate.local == self.version.local
            )
        if self.operator == "==":
            return equal
        if self.operator == "!=":
            return not equal
        if self.operator == ">=":
            return comparison >= 0
        if self.operator == "<=":
            return comparison <= 0
        if self.operator == ">":
            return comparison > 0
        if self.operator == "<":
            return comparison < 0
        prefix = self.version.release[:-1]
        padded = candidate.release + (0,) * max(0, len(prefix) - len(candidate.release))
        return comparison >= 0 and padded[: len(prefix)] == prefix


def _specifiers(value: str) -> list[_Clause]:
    value = _text(value, MAX_EXPRESSION_LENGTH, "unsupported_specifier").strip()
    if not value:
        return []
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
        if not value:
            raise ClosureError("unsupported_specifier")
    parts = value.split(",")
    if parts[-1].strip() == "":
        parts.pop()  # PEP 508 permits a single trailing comma.
    if len(parts) > MAX_SPECIFIERS:
        raise ClosureError("resource_limit")
    clauses = []
    for part in parts:
        match = _SPECIFIER.fullmatch(part.strip())
        if match is None:
            raise ClosureError("unsupported_specifier")
        clauses.append(_Clause(match[1], match[2]))
    if not clauses:
        raise ClosureError("unsupported_specifier")
    return clauses


def _environment(value: object) -> dict[str, str]:
    if type(value) is not dict or len(value) > len(_FIELDS):
        raise ClosureError("invalid_environment")
    for key, item in value.items():
        if type(key) is not str or key not in _FIELDS:
            raise ClosureError("invalid_environment")
        _text(item, MAX_ENVIRONMENT_LENGTH, "invalid_environment")
    if not {"python_version", "python_full_version"} <= value.keys():
        raise ClosureError("missing_environment")
    if value.get("extra", "") != "":
        raise ClosureError("selected_extras_unsupported")
    versions = {key: _Version(value[key]) for key in _VERSION_FIELDS if key in value}
    if any(version.local for version in versions.values()):
        raise ClosureError("invalid_environment")
    short = versions["python_version"].release
    full = versions["python_full_version"].release
    if len(short) != 2 or len(full) < 3 or short != full[:2]:
        raise ClosureError("invalid_environment")
    return value.copy()


def _string_comparison(left: str, operator: str, right: str) -> bool:
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "in":
        return left in right
    if operator == "not in":
        return left not in right
    raise ClosureError("unsupported_marker_comparison")


class _Marker:
    """Recursive descent with bounded nesting and eager branch validation."""

    def __init__(self, value: str, environment: dict[str, str]) -> None:
        value = _text(value, MAX_EXPRESSION_LENGTH, "invalid_marker").strip()
        self.tokens: list[tuple[str, str]] = []
        self.position = 0
        self.environment = environment
        offset = 0
        while offset < len(value):
            match = _TOKEN.match(value, offset)
            if match is None:
                raise ClosureError("invalid_marker")
            if match[1] is not None:
                literal = _text(match[1][1:-1], MAX_ENVIRONMENT_LENGTH, "invalid_marker")
                self.tokens.append(("literal", literal))
            elif match[2] is not None:
                self.tokens.append(("word", match[2]))
            else:
                self.tokens.append(("symbol", match[3]))
            if len(self.tokens) > MAX_MARKER_TOKENS:
                raise ClosureError("resource_limit")
            offset = match.end()

    def _peek(self, value: str) -> bool:
        return self.position < len(self.tokens) and self.tokens[self.position] == (
            "word" if value in {"and", "or", "not", "in"} else "symbol",
            value,
        )

    def _take(self) -> tuple[str, str]:
        if self.position >= len(self.tokens):
            raise ClosureError("invalid_marker")
        token = self.tokens[self.position]
        self.position += 1
        return token

    def evaluate(self) -> bool:
        result = self._or(0)
        if self.position != len(self.tokens):
            raise ClosureError("invalid_marker")
        return result

    def _or(self, depth: int) -> bool:
        result = self._and(depth)
        while self._peek("or"):
            self.position += 1
            right = self._and(depth)
            result = result or right
        return result

    def _and(self, depth: int) -> bool:
        result = self._atom(depth)
        while self._peek("and"):
            self.position += 1
            right = self._atom(depth)
            result = result and right
        return result

    def _atom(self, depth: int) -> bool:
        if self._peek("("):
            if depth >= MAX_MARKER_DEPTH:
                raise ClosureError("resource_limit")
            self.position += 1
            result = self._or(depth + 1)
            if not self._peek(")"):
                raise ClosureError("invalid_marker")
            self.position += 1
            return result
        left = self._operand()
        kind, operator = self._take()
        if (
            (kind == "word" and operator not in {"in", "not"})
            or (kind == "symbol" and operator in {"(", ")"})
            or kind == "literal"
        ):
            raise ClosureError("invalid_marker")
        if operator == "not":
            if not self._peek("in"):
                raise ClosureError("invalid_marker")
            self.position += 1
            operator = "not in"
        return self._compare(left, operator, self._operand())

    def _operand(self) -> tuple[str, str]:
        kind, value = self._take()
        if kind == "word" and value not in _FIELDS:
            raise ClosureError("unknown_marker")
        if kind not in {"word", "literal"}:
            raise ClosureError("invalid_marker")
        return kind, value

    def _compare(self, left: tuple[str, str], operator: str, right: tuple[str, str]) -> bool:
        if left[0] == right[0]:
            raise ClosureError("unsupported_marker_comparison")
        variable, literal = (left[1], right[1]) if left[0] == "word" else (right[1], left[1])
        if variable == "extra":
            if operator != "==" or not literal:
                raise ClosureError("unsupported_extra_marker")
            _name(literal)
            return False
        if variable not in self.environment:
            raise ClosureError("missing_environment")
        value = self.environment[variable]
        first, second = (value, literal) if left[0] == "word" else (literal, value)
        if variable in _STRING_FIELDS:
            return _string_comparison(first, operator, second)
        if variable in _PLATFORM_VERSION_FIELDS:
            if operator in {"in", "not in"}:
                return _string_comparison(first, operator, second)
            # Do not guess that a failed parse is a non-version: even v-prefixed
            # or whitespace-padded strings may have PEP 440 version semantics.
        if operator in {"in", "not in"}:
            raise ClosureError("unsupported_marker_comparison")
        return _Clause(operator, second).matches(_Version(first))


def _requirement(value: object, environment: dict[str, str]) -> tuple:
    value = _text(value, MAX_REQUIREMENT_LENGTH, "invalid_requirement").strip()
    # Split only once; semicolons within marker literals remain quoted tokens.
    requirement, separator, marker = value.partition(";")
    match = _NAME.match(requirement)
    if match is None:
        raise ClosureError("invalid_requirement")
    name = _name(match[0])
    remainder = requirement[match.end() :].strip()
    extras: list[str] = []
    if remainder.startswith("["):
        end = remainder.find("]")
        if end == -1:
            raise ClosureError("invalid_requirement")
        raw_extras = remainder[1:end].split(",")
        if len(raw_extras) > MAX_SPECIFIERS:
            raise ClosureError("resource_limit")
        extras = sorted({_name(extra.strip()) for extra in raw_extras})
        remainder = remainder[end + 1 :].strip()
    if "@" in remainder:
        raise ClosureError("direct_reference_unsupported")
    clauses = _specifiers(remainder)
    # Always parse/evaluate the complete expression, including inactive branches.
    active = _Marker(marker, environment).evaluate() if separator else True
    return name, clauses, extras, bool(separator), active


def verify_closure(wheels: list[dict], environment: dict[str, str]) -> dict:
    """Return a bounded, deterministic closure report or raise ``ClosureError``.

    Each input record needs name (already normalized), version, requires_python
    (possibly empty), and requires_dist (list of strings). Additional record keys
    are ignored. python_version and python_full_version are mandatory and must
    agree; any other marker variable must be supplied when used. No host-derived
    default is substituted. ``extra`` must be absent or empty.

    The report contains result='pass', wheel_count, selected_extras=[],
    dependency_edges, and python_requirements. Edges include source, target,
    normalized specifier, requested_extras, marker_present, active, and the
    selected_version (null for inactive edges). Inactive edges are retained.
    A pass proves only static closure of these inputs, not their exact inventory,
    provenance, installability, artifact integrity, or native compatibility.
    """
    environment = _environment(environment)
    if type(wheels) is not list or not wheels:
        raise ClosureError("invalid_inventory")
    if len(wheels) > MAX_WHEELS:
        raise ClosureError("resource_limit")
    selected: dict[str, _Version] = {}
    parsed: list[tuple[str, list[_Clause], list]] = []
    edge_count = 0
    for wheel in wheels:
        if (
            type(wheel) is not dict
            or not {"name", "version", "requires_python", "requires_dist"} <= wheel.keys()
        ):
            raise ClosureError("invalid_metadata")
        name = _name(wheel["name"])
        if name != wheel["name"]:
            raise ClosureError("invalid_name")
        if name in selected:
            raise ClosureError("duplicate_distribution")
        selected[name] = _Version(wheel["version"])
        python_spec = _specifiers(wheel["requires_python"])
        dependencies = wheel["requires_dist"]
        if type(dependencies) is not list:
            raise ClosureError("invalid_metadata")
        edge_count += len(dependencies)
        if len(dependencies) > MAX_REQUIREMENTS_PER_WHEEL or edge_count > MAX_EDGES:
            raise ClosureError("resource_limit")
        parsed.append((name, python_spec, dependencies))
    python_version = _Version(environment["python_full_version"])
    edges = []
    python_requirements = []
    for source, python_spec, dependencies in sorted(parsed):
        if not all(clause.matches(python_version) for clause in python_spec):
            raise ClosureError("requires_python_mismatch")
        python_requirements.append(
            {"name": source, "specifier": ",".join(sorted({c.text for c in python_spec}))}
        )
        for dependency in dependencies:
            target, clauses, extras, marker_present, active = _requirement(dependency, environment)
            if active:
                if extras:
                    raise ClosureError("dependency_extras_unsupported")
                if target not in selected:
                    raise ClosureError("missing_dependency")
                if not all(clause.matches(selected[target]) for clause in clauses):
                    raise ClosureError("dependency_version_mismatch")
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "specifier": ",".join(sorted({clause.text for clause in clauses})),
                    "requested_extras": extras,
                    "marker_present": marker_present,
                    "active": active,
                    "selected_version": selected[target].text if active else None,
                }
            )
    edges.sort(
        key=lambda edge: (
            edge["source"],
            edge["target"],
            edge["specifier"],
            edge["requested_extras"],
            edge["marker_present"],
            edge["active"],
        )
    )
    return {
        "result": "pass",
        "wheel_count": len(wheels),
        "selected_extras": [],
        "dependency_edges": edges,
        "python_requirements": python_requirements,
    }
