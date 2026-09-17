"""Source-free evidence for Stage-06 RTP delivery qualification."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RtpDeliveryEvidence(BaseModel):
    """Retained proof that ephemeral RTP reached the K5 consumer boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|local)$")
    execution_context: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    runtime: Literal["GStreamer 1.28.7"] = "GStreamer 1.28.7"
    source_transport: Literal["rtsp-udp"] = "rtsp-udp"
    delivery_transport: Literal["loopback-udp-rtp"] = "loopback-udp-rtp"
    valid_packets: int = Field(ge=1, le=4096)
    invalid_packets: int = Field(ge=0, le=4096)
    delivered_bytes: int = Field(ge=1)
    consumer_callbacks: int = Field(ge=1, le=4096)
    elapsed_ms: int = Field(ge=0, le=600_000)

    @field_validator("execution_context")
    @classmethod
    def reject_network_identity(cls, value: str) -> str:
        try:
            ip_address(value)
        except ValueError:
            return value
        raise ValueError("execution context must not contain a literal network address")

    @property
    def accepted(self) -> bool:
        return (
            self.valid_packets > 0
            and self.consumer_callbacks == self.valid_packets
            and self.delivered_bytes > 0
        )
