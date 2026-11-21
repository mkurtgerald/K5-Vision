"""Compile the sealed native Windows service host and graphical client.

This development helper uses the Windows-shipped .NET Framework compiler. It
does not install a compiler, invoke a shell, or become an operator setup step.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "K5Vision.cs",
    "K5RecorderHost.cs",
    "K5ServiceInstallation.cs",
    "K5VisionClient.cs",
)
REFERENCES = (
    "System.dll",
    "System.Core.dll",
    "System.Drawing.dll",
    "System.Windows.Forms.dll",
    "System.Web.Extensions.dll",
    "System.IO.Compression.dll",
    "System.IO.Compression.FileSystem.dll",
)


def command(build: Path, compiler: Path) -> list[str]:
    """Build a fixed compiler argv, preserving paths with spaces as arguments."""
    identity = build / "PayloadIdentity.generated.cs"
    sources = [ROOT / "windows" / "desktop" / name for name in SOURCES]
    if any(path.is_symlink() or not path.is_file() for path in [identity, *sources]):
        raise ValueError("Native source or generated identity missing")
    payload = build / "payload"
    if payload.is_symlink() or not payload.is_dir():
        raise ValueError("Admitted native payload missing")
    output = payload / "K5Vision.exe"
    if output.exists():
        raise ValueError("Native launcher output must be new")
    return [
        str(compiler),
        "/nologo",
        "/target:winexe",
        "/main:K5VisionDesktop.ClientProgram",
        "/platform:x64",
        "/optimize+",
        "/warn:4",
        "/warnaserror+",
        "/codepage:65001",
        *("/reference:" + name for name in REFERENCES),
        "/out:" + str(output),
        *(str(path) for path in sources),
        str(identity),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise ValueError("Native launcher compilation requires Windows")
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise ValueError("Windows .NET Framework compiler missing")
    subprocess.run(command(args.build.absolute(), compiler), check=True, timeout=120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
