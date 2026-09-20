"""Bounded source-free catalog authoring above the accepted operator control."""

from __future__ import annotations

import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_catalog import (
    ViewportCatalogEntry,
    ViewportCatalogError,
    ViewportCatalogErrorCode,
    build_viewport_catalog,
    serialize_viewport_catalog,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_selection import (
    BoundedSelectableWindowsOperatorControl,
    WindowsOperatorSelectionSnapshot,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState

_MAX_CATALOG_VIEWS = 64


class WindowsOperatorCatalogAuthoringSnapshot(BaseModel):
    """Aggregate source-free observability for reusable view authoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    selection: WindowsOperatorSelectionSnapshot
    catalog_saves: int = Field(default=0, ge=0)
    catalog_deletes: int = Field(default=0, ge=0)
    catalog_exports: int = Field(default=0, ge=0)


class BoundedAuthoringWindowsOperatorControl(BoundedSelectableWindowsOperatorControl):
    """Create, replace, delete and export source-free reusable arbitrary views."""

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._catalog_saves = 0
        self._catalog_deletes = 0
        self._catalog_exports = 0

    @property
    def catalog_authoring_snapshot(self) -> WindowsOperatorCatalogAuthoringSnapshot:
        return WindowsOperatorCatalogAuthoringSnapshot(
            selection=self.selection_snapshot,
            catalog_saves=self._catalog_saves,
            catalog_deletes=self._catalog_deletes,
            catalog_exports=self._catalog_exports,
        )

    @staticmethod
    def _validate_catalog_view_id(view_id: int) -> None:
        if (
            isinstance(view_id, bool)
            or not isinstance(view_id, int)
            or not 0 <= view_id < _MAX_CATALOG_VIEWS
        ):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator catalog view identifier is invalid",
            )

    def _require_catalog_authoring_state(self) -> None:
        if (
            self._state != WindowsOperatorSessionState.RUNNING
            or self._application is None
            or self._active_layout is None
        ):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_STATE,
                "operator catalog cannot mutate from current state",
            )

    def save_catalog_view(self, view_id: int) -> WindowsOperatorCatalogAuthoringSnapshot:
        """Atomically create or replace one view from the current source-free layout."""
        self._validate_catalog_view_id(view_id)
        self._require_catalog_authoring_state()
        assert self._active_layout is not None

        entries = [entry for entry in self._catalog.views if entry.view_id != view_id]
        entries.append(ViewportCatalogEntry(view_id=view_id, layout=self._active_layout))
        try:
            candidate = build_viewport_catalog(entries)
            serialize_viewport_catalog(candidate)
        except ViewportCatalogError as exc:
            code = (
                WindowsOperatorControlErrorCode.CONTROL_LIMIT
                if exc.code == ViewportCatalogErrorCode.PAYLOAD_LIMIT
                else WindowsOperatorControlErrorCode.INVALID_EDIT
            )
            raise WindowsOperatorControlError(
                code,
                "operator catalog view cannot be saved",
            ) from None

        self._catalog = candidate
        self._catalog_saves += 1
        return self.catalog_authoring_snapshot

    def delete_catalog_view(self, view_id: int) -> WindowsOperatorCatalogAuthoringSnapshot:
        """Atomically remove one identified reusable view."""
        self._validate_catalog_view_id(view_id)
        self._require_catalog_authoring_state()
        if view_id not in self._catalog.by_id():
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_EDIT,
                "operator catalog view is unavailable",
            )

        entries = tuple(entry for entry in self._catalog.views if entry.view_id != view_id)
        try:
            candidate = build_viewport_catalog(entries)
        except ViewportCatalogError:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_EDIT,
                "operator catalog view cannot be deleted",
            ) from None

        self._catalog = candidate
        self._catalog_deletes += 1
        return self.catalog_authoring_snapshot

    def export_catalog(self) -> bytes:
        """Return canonical source-free bytes for caller-owned persistence."""
        self._require_catalog_authoring_state()
        try:
            payload = serialize_viewport_catalog(self._catalog)
        except ViewportCatalogError as exc:
            code = (
                WindowsOperatorControlErrorCode.CONTROL_LIMIT
                if exc.code == ViewportCatalogErrorCode.PAYLOAD_LIMIT
                else WindowsOperatorControlErrorCode.APPLICATION_FAILURE
            )
            raise WindowsOperatorControlError(
                code,
                "operator catalog cannot be exported",
            ) from None
        self._catalog_exports += 1
        return payload
