"""Pause-aware adapter for the accepted presentation playback delivery."""

from __future__ import annotations

from k5vision.media.playback_control import PlaybackPauseControl
from k5vision.media.presentation_playback import BoundedPresentationPlaybackDelivery


class PausablePresentationPlaybackDelivery(BoundedPresentationPlaybackDelivery):
    """Reuse the accepted presentation path with an optional bounded pause control."""

    def __init__(
        self,
        *args,
        pause_control: PlaybackPauseControl | None = None,
        **kwargs,
    ) -> None:
        if pause_control is not None and not isinstance(pause_control, PlaybackPauseControl):
            raise TypeError("pause_control must be a PlaybackPauseControl")
        super().__init__(*args, **kwargs)
        if pause_control is not None:
            self._pump.bind_pause_control(pause_control)
