"""Preserve legal notices from the exact Go sources used by native workers.

This maintainer utility reads a previously downloaded official source archive.
It does not download, build, install, or redistribute a media-worker binary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

_SOURCES = {
    "go1.26.8": (
        "4e39b98e42f946fa05ac8bc5b71877df97dbdb7cbb1a777b541667ad7117fd2e",
        34_150_120,
    ),
    "go1.27.2": (
        "03495da2ba64894d40f5c4992e49454fa78b50690604ff92b6afff5081b76e62",
        35_134_614,
    ),
}
_NAMED_NOTICE = re.compile(
    r"^(?:LICENSE|LICENCE|COPYING|COPYRIGHT|NOTICE|PATENTS|AUTHORS)(?:[._-].*)?$",
    re.IGNORECASE,
)
_COMMENTS = re.compile(
    rb"(?m)^[ \t]*//[^\r\n]*(?:\r?\n[ \t]*//[^\r\n]*)*|/\*.*?\*/",
    re.DOTALL,
)
_LEGAL = re.compile(
    rb"copyright|permission is hereby|redistribution|licen[cs]e|patent grant",
    re.IGNORECASE,
)
_SOURCE_SUFFIXES = {".go", ".s", ".c", ".h", ".cc", ".cpp"}


def preserve(source_archive: Path, output_root: Path, version: str) -> dict:
    """Extract unchanged notices and verbatim legal comment blocks."""
    expected_hash, expected_size = _SOURCES[version]
    if (
        source_archive.stat().st_size != expected_size
        or hashlib.sha256(source_archive.read_bytes()).hexdigest() != expected_hash
    ):
        raise ValueError("Go source archive does not match the reviewed input")
    destination = output_root / version
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    comments: dict[bytes, list[dict]] = {}
    with tarfile.open(source_archive, "r:gz") as archive:
        for member in sorted(archive.getmembers(), key=lambda item: item.name):
            relative = PurePosixPath(member.name).relative_to("go")
            if not member.isfile() or ".." in relative.parts:
                continue
            is_notice = bool(_NAMED_NOTICE.fullmatch(relative.name))
            is_source = relative.suffix in _SOURCE_SUFFIXES
            if not is_notice and not is_source:
                continue
            reader = archive.extractfile(member)
            if reader is None:
                raise ValueError("Official source member cannot be read")
            payload = reader.read()
            if is_notice:
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                files.append(
                    {
                        "archive_path": member.name,
                        "source_relative_path": relative.as_posix(),
                        "path": target.as_posix(),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size_bytes": len(payload),
                        "scope": "supplied-source-notice",
                    }
                )
            if is_source:
                for match in _COMMENTS.finditer(payload):
                    block = match.group()
                    if _LEGAL.search(block):
                        comments.setdefault(block, []).append(
                            {
                                "source_relative_path": relative.as_posix(),
                                "source_sha256": hashlib.sha256(payload).hexdigest(),
                                "source_line": payload[: match.start()].count(b"\n") + 1,
                            }
                        )
    notice_bytes = bytearray(
        b"Exact Go source legal comment blocks. Supplemental notice preservation;\n"
        b"source presence does not establish that a file was linked for Windows.\n\n"
    )
    for block, origins in sorted(comments.items(), key=lambda item: item[0]):
        notice_bytes.extend(b"------------------------------------------------------------\n")
        for origin in origins:
            notice_bytes.extend((json.dumps(origin, sort_keys=True) + "\n").encode())
        notice_bytes.extend(block)
        notice_bytes.extend(b"\n\n")
    notice = destination / "SOURCE-COPYRIGHT-NOTICES.txt"
    notice.write_bytes(notice_bytes)
    files.append(
        {
            "path": notice.as_posix(),
            "sha256": hashlib.sha256(notice_bytes).hexdigest(),
            "size_bytes": len(notice_bytes),
            "scope": "verbatim-source-legal-comment-aggregate",
            "unique_comment_blocks": len(comments),
        }
    )
    return {
        "version": version,
        "source_archive_url": f"https://go.dev/dl/{version}.src.tar.gz",
        "archive_sha256": expected_hash,
        "archive_size_bytes": expected_size,
        "official_release_metadata_url": "https://go.dev/dl/?mode=json&include=all",
        "files": files,
        "review_scope": "runtime-and-source-notice-preservation",
        "source_notice_presence_is_not_linkage_evidence": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", choices=sorted(_SOURCES), required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = preserve(args.source_archive, args.output_root, args.version)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
