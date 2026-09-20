"""Bounded source-free reusable catalog for arbitrary viewport layouts."""

from __future__ import annotations

import enum
import typing
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from k5vision.media.viewport_geometry import ViewportLayout

_MAX_CATALOG_VIEWS = 64
_MAX_CATALOG_BYTES = 262_144


class ViewportCatalogErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_PAYLOAD = "invalid_payload"
    PAYLOAD_LIMIT = "payload_limit"
    VIEW_NOT_FOUND = "view_not_found"
    INCOMPATIBLE_LAYOUT = "incompatible_layout"


class ViewportCatalogError(ValueError):
    """Sanitized catalog failure that never reflects supplied content."""

    def __init__(self, code: ViewportCatalogErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ViewportCatalogEntry(BaseModel):
    """One logical reusable view containing geometry only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    view_id: int = Field(ge=0, le=_MAX_CATALOG_VIEWS - 1)
    layout: ViewportLayout


class ViewportCatalog(BaseModel):
    """Versioned bounded source-free view catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    views: tuple[ViewportCatalogEntry, ...] = Field(
        default=(),
        max_length=_MAX_CATALOG_VIEWS,
    )

    @model_validator(mode="after")
    def validate_unique_views(self) -> ViewportCatalog:
        view_ids = [entry.view_id for entry in self.views]
        if len(view_ids) != len(set(view_ids)):
            raise ValueError("catalog view identifiers must be unique")
        return self

    def by_id(self) -> dict[int, ViewportLayout]:
        return {entry.view_id: entry.layout for entry in self.views}


def build_viewport_catalog(entries: Iterable[ViewportCatalogEntry]) -> ViewportCatalog:
    """Build canonical view-id order from already validated source-free entries."""
    try:
        materialized = tuple(entries)
    except Exception:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_CONFIGURATION,
            "viewport catalog entries are invalid",
        ) from None
    if len(materialized) > _MAX_CATALOG_VIEWS or not all(
        isinstance(entry, ViewportCatalogEntry) for entry in materialized
    ):
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_CONFIGURATION,
            "viewport catalog entries are invalid",
        )
    try:
        return ViewportCatalog(views=tuple(sorted(materialized, key=lambda entry: entry.view_id)))
    except ValidationError:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_CONFIGURATION,
            "viewport catalog entries are invalid",
        ) from None


def serialize_viewport_catalog(catalog: ViewportCatalog) -> bytes:
    """Serialize one catalog deterministically without source/media identity."""
    if not isinstance(catalog, ViewportCatalog):
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_CONFIGURATION,
            "viewport catalog is invalid",
        )
    canonical = build_viewport_catalog(catalog.views)
    payload = canonical.model_dump_json().encode("utf-8")
    if len(payload) > _MAX_CATALOG_BYTES:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.PAYLOAD_LIMIT,
            "viewport catalog payload exceeds limit",
        )
    return payload


def parse_viewport_catalog(payload: bytes | str) -> ViewportCatalog:
    """Parse bounded UTF-8 JSON into a canonical validated catalog."""
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, bytes):
        encoded = payload
    else:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_PAYLOAD,
            "viewport catalog payload is invalid",
        )
    if len(encoded) > _MAX_CATALOG_BYTES:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.PAYLOAD_LIMIT,
            "viewport catalog payload exceeds limit",
        )
    try:
        decoded = encoded.decode("utf-8")
        catalog = ViewportCatalog.model_validate_json(decoded)
    except (UnicodeDecodeError, ValidationError, ValueError):
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_PAYLOAD,
            "viewport catalog payload is invalid",
        ) from None
    return build_viewport_catalog(catalog.views)


def catalog_view_for_active_layout(
    catalog: ViewportCatalog,
    view_id: int,
    active_layout: ViewportLayout,
) -> ViewportLayout:
    """Return one reusable layout only when its logical slot set is compatible."""
    if (
        not isinstance(catalog, ViewportCatalog)
        or isinstance(view_id, bool)
        or not isinstance(view_id, int)
        or not isinstance(active_layout, ViewportLayout)
    ):
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INVALID_CONFIGURATION,
            "viewport catalog request is invalid",
        )
    candidate = catalog.by_id().get(view_id)
    if candidate is None:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.VIEW_NOT_FOUND,
            "viewport catalog view is unavailable",
        )
    active_slots = {item.logical_slot for item in active_layout.placements}
    candidate_slots = {item.logical_slot for item in candidate.placements}
    if candidate_slots != active_slots:
        raise ViewportCatalogError(
            ViewportCatalogErrorCode.INCOMPATIBLE_LAYOUT,
            "viewport catalog view is incompatible with active layout",
        )
    return candidate
