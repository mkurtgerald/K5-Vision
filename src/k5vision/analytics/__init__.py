"""Optional analytics acceleration boundaries for K5 Vision.

The package deliberately keeps analytics replaceable and outside the product-owned
media, authorization, evidence, and operator-control planes.
"""

from k5vision.analytics.hardware_acceleration import (
    ComputeBackend,
    HardwareProfile,
    probe_hardware,
)
from k5vision.analytics.jsonl_skill_provider import JsonlSkillProvider, SkillProviderError
from k5vision.analytics.skill_protocol import (
    SkillBox,
    SkillProtocolError,
    SkillTrackedDetection,
)

__all__ = [
    "ComputeBackend",
    "HardwareProfile",
    "JsonlSkillProvider",
    "SkillBox",
    "SkillProtocolError",
    "SkillProviderError",
    "SkillTrackedDetection",
    "probe_hardware",
]
