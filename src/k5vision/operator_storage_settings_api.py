"""Current-administrator storage policy routes for the native workspace."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from k5vision.domain.users import UserRole
from k5vision.operator_product_service import OperatorProductError
from k5vision.operator_storage_settings import (
    MAX_QUOTA_BYTES,
    MAX_RETENTION_SECONDS,
    StorageDirectoryError,
    StorageSettingsConflict,
    StorageSettingsError,
)

_PREFIX = "/api/v1/operator/admin/storage"


class _StoragePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=0, le=2**63 - 1)
    quota_bytes: StrictInt | None = Field(default=None, ge=1, le=MAX_QUOTA_BYTES)
    minimum_free_bytes: StrictInt | None = Field(default=None, ge=0, le=MAX_QUOTA_BYTES)
    retention_seconds: StrictInt | None = Field(default=None, ge=0, le=MAX_RETENTION_SECONDS)
    recording_directory: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def has_change(self):
        fields = self.model_fields_set - {"expected_revision"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("A storage change is required")
        return self


def install_operator_storage_settings_api(
    app: FastAPI, *, service, require_admin, authenticate, mutations
) -> None:
    dependency = Depends(require_admin)

    async def current_admin(session):
        account, token = await authenticate(session[1])
        if account.id != session[0].id or account.role is not UserRole.ADMINISTRATOR:
            raise HTTPException(403, "Insufficient storage administration permission")
        return account, token

    def unavailable(error):
        if isinstance(error, StorageSettingsConflict):
            return HTTPException(409, "storage_conflict")
        if isinstance(error, StorageDirectoryError):
            return HTTPException(422, "storage_directory_refused")
        if isinstance(error, ValueError):
            return HTTPException(422, "storage_invalid_request")
        return HTTPException(503, "storage_unavailable")

    @app.get(_PREFIX, tags=["operator storage"])
    async def storage(response: Response, session=dependency):
        response.headers["Cache-Control"] = "no-store"
        await current_admin(session)
        try:
            return service.storage_administration()
        except (StorageSettingsError, OSError, ValueError):
            raise HTTPException(503, "storage_unavailable") from None

    @app.patch(_PREFIX, tags=["operator storage"])
    async def configure_storage(payload: _StoragePatch, response: Response, session=dependency):
        response.headers["Cache-Control"] = "no-store"
        async with mutations:
            await current_admin(session)
            try:
                result = await service.configure_storage(**payload.model_dump(exclude_unset=True))
            except (StorageSettingsError, OperatorProductError, OSError, ValueError) as error:
                raise unavailable(error) from None
            await current_admin(session)
            return result
