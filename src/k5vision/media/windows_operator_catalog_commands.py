"""Bounded source-free command dispatch for accepted reusable-view operations."""

from __future__ import annotations

import enum
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.windows_operator_catalog_authoring import (
    BoundedAuthoringWindowsOperatorControl,
    WindowsOperatorCatalogAuthoringSnapshot,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)


class WindowsOperatorCatalogCommandKind(enum.StrEnum):
    """Accepted reusable-view commands exposed above the authoring boundary."""

    SAVE = "save"
    APPLY = "apply"
    DELETE = "delete"


class WindowsOperatorCatalogCommand(BaseModel):
    """One bounded source-free reusable-view command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    kind: WindowsOperatorCatalogCommandKind
    view_id: int = Field(strict=True, ge=0, le=63)


class WindowsOperatorCatalogCommandSnapshot(BaseModel):
    """Aggregate command observability without retaining command arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    authoring: WindowsOperatorCatalogAuthoringSnapshot
    catalog_commands: int = Field(default=0, ge=0)
    catalog_save_commands: int = Field(default=0, ge=0)
    catalog_apply_commands: int = Field(default=0, ge=0)
    catalog_delete_commands: int = Field(default=0, ge=0)


class BoundedCommandWindowsOperatorControl(BoundedAuthoringWindowsOperatorControl):
    """Dispatch validated catalog commands onto already-accepted operator methods."""

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._catalog_commands = 0
        self._catalog_save_commands = 0
        self._catalog_apply_commands = 0
        self._catalog_delete_commands = 0

    @property
    def catalog_command_snapshot(self) -> WindowsOperatorCatalogCommandSnapshot:
        return WindowsOperatorCatalogCommandSnapshot(
            authoring=self.catalog_authoring_snapshot,
            catalog_commands=self._catalog_commands,
            catalog_save_commands=self._catalog_save_commands,
            catalog_apply_commands=self._catalog_apply_commands,
            catalog_delete_commands=self._catalog_delete_commands,
        )

    def dispatch_catalog_command(
        self,
        command: WindowsOperatorCatalogCommand,
    ) -> WindowsOperatorCatalogCommandSnapshot:
        """Execute one validated source-free command and count only success."""
        if not isinstance(command, WindowsOperatorCatalogCommand):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator catalog command is invalid",
            )

        if command.kind == WindowsOperatorCatalogCommandKind.SAVE:
            self.save_catalog_view(command.view_id)
            self._catalog_save_commands += 1
        elif command.kind == WindowsOperatorCatalogCommandKind.APPLY:
            self.apply_catalog_view(command.view_id)
            self._catalog_apply_commands += 1
        elif command.kind == WindowsOperatorCatalogCommandKind.DELETE:
            self.delete_catalog_view(command.view_id)
            self._catalog_delete_commands += 1
        else:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator catalog command is invalid",
            )

        self._catalog_commands += 1
        return self.catalog_command_snapshot
