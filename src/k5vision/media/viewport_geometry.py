"""Renderer-neutral bounded viewport geometry for future operator composition."""

from __future__ import annotations

import typing

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_COORDINATE = 1_000_000
_MAX_DIMENSION = 1_000_000
_MAX_Z_INDEX = 65_535


class ViewportGeometry(BaseModel):
    """Serializable geometry independent of camera/source and native handles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    x: int = Field(ge=0, le=_MAX_COORDINATE)
    y: int = Field(ge=0, le=_MAX_COORDINATE)
    width: int = Field(ge=1, le=_MAX_DIMENSION)
    height: int = Field(ge=1, le=_MAX_DIMENSION)
    z_index: int = Field(default=0, ge=0, le=_MAX_Z_INDEX)

    @model_validator(mode="after")
    def validate_bounds(self) -> ViewportGeometry:
        if self.x + self.width > _MAX_COORDINATE + _MAX_DIMENSION:
            raise ValueError("viewport horizontal extent is invalid")
        if self.y + self.height > _MAX_COORDINATE + _MAX_DIMENSION:
            raise ValueError("viewport vertical extent is invalid")
        return self


class ViewportPlacement(BaseModel):
    """Logical-slot placement with no source, path, payload, or native identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    logical_slot: int = Field(ge=0, le=4095)
    geometry: ViewportGeometry


class ViewportLayout(BaseModel):
    """Bounded arbitrary viewport composition contract.

    This intentionally does not implement drag/drop or persistence. It freezes the
    receiving geometry contract so a later composer can be developed independently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    placements: tuple[ViewportPlacement, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_unique_slots(self) -> ViewportLayout:
        slots = [item.logical_slot for item in self.placements]
        if len(slots) != len(set(slots)):
            raise ValueError("viewport logical slots must be unique")
        return self

    def by_slot(self) -> dict[int, ViewportGeometry]:
        return {item.logical_slot: item.geometry for item in self.placements}
