"""Analytics integration contracts for K5 Vision."""

from k5vision.analytics.contracts import (
    ANALYTICS_API_VERSION,
    AnalyticEvent,
    AnalyticHealth,
    AnalyticManifest,
    AnalyticState,
    BoundingBox,
    EvidenceReference,
)
from k5vision.analytics.registry import AnalyticRegistry

__all__ = [
    "ANALYTICS_API_VERSION",
    "AnalyticEvent",
    "AnalyticHealth",
    "AnalyticManifest",
    "AnalyticRegistry",
    "AnalyticState",
    "BoundingBox",
    "EvidenceReference",
]
