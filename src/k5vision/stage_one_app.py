"""Stage-One application factory with private physical live-operator wiring.

Use this factory for human physical acceptance on the Windows operator host. It reads
the already-established private Stage-03 source/credential environment at process
startup and injects the concrete source resolver and Windows launcher into the normal
K5 control-plane application. Missing or malformed private runtime configuration stays
fail closed; no secret is copied into public API models or retained evidence.
"""

from __future__ import annotations

from os import environ

from fastapi import FastAPI

from k5vision.main import create_app
from k5vision.operator_recording import STAGE_ONE_RECORDING_ROOT_ENV
from k5vision.operator_runtime import build_environment_operator_runtime


def create_stage_one_app() -> FastAPI:
    """Build the standard K5 app with the bounded physical live runtime enabled."""
    source_resolver, launcher = build_environment_operator_runtime(environ)
    recording_root = environ.get(STAGE_ONE_RECORDING_ROOT_ENV, "").strip() or None
    return create_app(
        operator_source_resolver=source_resolver,
        operator_launcher=launcher,
        operator_recording_root=recording_root,
    )
