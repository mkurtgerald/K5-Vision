"""Independent local OpenVINO SSD person adapter for optional view-only boxes.

K5 owns this adapter, written against Intel's documented public input/output API.
It copies no Analytics Lab detector/tracker source and imports no Analytics Lab
package. The separately licensed Apache-2.0 model stays locally provisioned;
neither this module nor application startup downloads models or dependencies.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from k5vision.analytics_config import _local_path, _unique_object
from k5vision.analytics_runtime import OwnedAnalyticsProvider, _AnalyticsSession

PERSON_CONFIG_ENV = "K5_OPERATOR_PERSON_CONFIG"
PERSON_PROVIDER = "openvino-omz-person-view-v1"
MODEL_REVISION = "6697dead54ed1cdd664b0313189c2cb52ee6335e"
PERSON_MANIFEST_SHA256 = "b15ac68ec04b1fa9b1c95b320e2694469b8e764049fac544d6986a940f1fd794"
PERSON_MANIFEST_CRLF_SHA256 = "dd401367fb24dbb26aeff42e32689f5bbd19a11fd35c3dc5677683f011970f11"
_MANIFEST = Path(__file__).parent / "data" / "operator-person-model-manifest.json"
_MAX_CONFIG_BYTES = 4096


class OperatorPersonConfigurationError(RuntimeError):
    """Fixed diagnostic without local paths or supplied values."""


@dataclass(frozen=True, slots=True)
class OperatorPersonConfiguration:
    artifact_root: Path


def _manifest() -> dict:
    raw = _MANIFEST.read_bytes()
    if hashlib.sha256(raw).hexdigest() not in {PERSON_MANIFEST_SHA256, PERSON_MANIFEST_CRLF_SHA256}:
        raise ValueError
    return json.loads(raw)


def validate_person_runtime(config: OperatorPersonConfiguration) -> None:
    """Admit exact manifest, package versions and immutable local model bytes."""
    try:
        if not isinstance(config, OperatorPersonConfiguration):
            raise ValueError
        manifest = _manifest()
        for package, version in manifest["runtime_versions"].items():
            if metadata.version(package) != version:
                raise ValueError
        root = _local_path(str(config.artifact_root), directory=True)
        for artifact in manifest["artifacts"]:
            path = _local_path(str(root / artifact["relative_path"]), directory=False)
            before = path.stat()
            if before.st_size != artifact["bytes"]:
                raise ValueError
            digest = hashlib.sha384()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            after = path.stat()

            def identity(item):
                return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)

            if digest.hexdigest() != artifact["sha384"] or identity(before) != identity(after):
                raise ValueError
    except Exception:
        raise OperatorPersonConfigurationError("Person runtime admission failed.") from None


def _model_bytes(config: OperatorPersonConfiguration) -> tuple[bytes, bytes]:
    """Pass verified immutable bytes into native code, without reopening paths."""
    result = []
    for artifact in _manifest()["artifacts"]:
        path = _local_path(str(config.artifact_root / artifact["relative_path"]), directory=False)
        before = path.stat()
        with path.open("rb") as stream:
            raw = stream.read(artifact["bytes"] + 1)
        _local_path(str(path), directory=False)
        after = path.stat()
        if (
            len(raw) != artifact["bytes"]
            or hashlib.sha384(raw).hexdigest() != artifact["sha384"]
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("Person model bytes were refused")
        result.append(raw)
    return result[0], result[1]


def load_person_configuration(
    environment: Mapping[str, str],
) -> OperatorPersonConfiguration | None:
    """Missing configuration disables inference; invalid explicit input fails closed."""
    if PERSON_CONFIG_ENV not in environment:
        return None
    try:
        path = _local_path(environment[PERSON_CONFIG_ENV], directory=False)
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError
        document = json.loads(raw, object_pairs_hook=_unique_object)
        if type(document) is not dict or document.keys() != {
            "schema_version",
            "provider",
            "model_revision",
            "artifact_root",
        }:
            raise ValueError
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["provider"] != PERSON_PROVIDER
            or document["model_revision"] != MODEL_REVISION
        ):
            raise ValueError
        config = OperatorPersonConfiguration(_local_path(document["artifact_root"], directory=True))
    except Exception:
        raise OperatorPersonConfigurationError("Person configuration was refused.") from None
    validate_person_runtime(config)
    return config


def load_installed_person_configuration(
    environment: Mapping[str, str],
) -> OperatorPersonConfiguration | None:
    """Use an explicit engineering override or the immutable installed model.

    Absence remains supported by source-only/legacy installations. A present
    but damaged installed model is refused rather than silently hidden.
    """
    if PERSON_CONFIG_ENV in environment:
        return load_person_configuration(environment)
    root = Path(__file__).parent / "data" / "person-models"
    if not root.exists():
        return None
    config = OperatorPersonConfiguration(root.absolute())
    validate_person_runtime(config)
    return config


@dataclass(frozen=True, slots=True)
class _PersonBox:
    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True, slots=True)
class _PersonDetection:
    # Only a per-frame index, deliberately no tracking or identity assertion.
    track_id: str
    category: str
    confidence: float
    box: _PersonBox


class _PersonDetector:
    def __init__(self, config: OperatorPersonConfiguration) -> None:
        validate_person_runtime(config)
        import numpy as np
        import openvino as ov
        from openvino.preprocess import ColorFormat, PrePostProcessor, ResizeAlgorithm

        xml, weights = _model_bytes(config)
        model = ov.Core().read_model(
            model=xml.decode("utf-8"), weights=ov.Tensor(np.frombuffer(weights, dtype=np.uint8))
        )
        if (
            len(model.inputs) != 1
            or len(model.outputs) != 1
            or list(model.input("data").shape) != [1, 3, 320, 544]
            or list(model.output(0).shape) != [1, 1, 200, 7]
        ):
            raise ValueError("Person model interface was refused")
        processing = PrePostProcessor(model)
        processing.input().tensor().set_element_type(ov.Type.u8).set_layout(
            ov.Layout("NHWC")
        ).set_color_format(ColorFormat.BGR).set_spatial_dynamic_shape()
        processing.input().preprocess().resize(ResizeAlgorithm.RESIZE_LINEAR)
        processing.input().model().set_layout(ov.Layout("NCHW"))
        compiled = ov.Core().compile_model(
            processing.build(), "CPU", {"INFERENCE_NUM_THREADS": 2, "NUM_STREAMS": "1"}
        )
        self._numpy = np
        self._request = compiled.create_infer_request()

    def infer(
        self, payload: bytes, width: int, height: int, stride: int, _elapsed_ms: int
    ) -> tuple[object, ...]:
        np = self._numpy
        raw = np.frombuffer(payload, dtype=np.uint8, count=height * stride)
        bgrx = raw.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
        bgr = np.ascontiguousarray(bgrx[:, :, :3])
        self._request.infer({0: bgr[np.newaxis, ...]})
        output = self._request.get_output_tensor(0).data
        if tuple(output.shape) != (1, 1, 200, 7):
            raise ValueError("Person detector output was refused")
        selected = []
        for index, row in enumerate(output[0, 0]):
            if not bool(np.isfinite(row).all()):
                raise ValueError("Person detector output was refused")
            image_id, label, confidence, left, top, right, bottom = map(float, row)
            if image_id < 0:
                break
            if image_id != 0 or label != 1 or not 0.5 <= confidence <= 1:
                continue
            left, top, right, bottom = (
                min(1.0, max(0.0, item)) for item in (left, top, right, bottom)
            )
            if right <= left or bottom <= top:
                continue
            selected.append(
                _PersonDetection(
                    f"view-person-{index}",
                    "person",
                    confidence,
                    _PersonBox(left, top, right, bottom),
                )
            )
        return tuple(sorted(selected, key=lambda item: item.confidence, reverse=True)[:32])


class _PersonSession(_AnalyticsSession):
    """Reuse K5's bounded native worker lifecycle, replacing all detector logic."""

    def _initialize(self) -> bool:
        try:
            self._backend = _PersonDetector(self._config)
            return True
        except BaseException:
            # Keep native exception tracebacks/model paths out of waiter Futures.
            return False


class PersonProviderFactory:
    """Independent person workers; capacity survives canceled native calls."""

    def __init__(self, config: OperatorPersonConfiguration) -> None:
        validate_person_runtime(config)
        self._config = config
        self._capacity = threading.BoundedSemaphore(4)

    async def __call__(self) -> OwnedAnalyticsProvider:
        if not self._capacity.acquire(blocking=False):
            raise RuntimeError("Person native capacity is unavailable.")
        try:
            session = _PersonSession(self._config, self._capacity.release)
        except Exception:
            self._capacity.release()
            raise RuntimeError("Person session initialization failed.") from None
        try:
            await session.start()
            return session
        except BaseException:
            await session.aclose()
            raise
