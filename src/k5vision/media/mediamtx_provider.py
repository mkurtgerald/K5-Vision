"""Read-only, project-owned boundary to a private MediaMTX media worker.

MediaMTX owns continuous transport and recording. K5 owns site/device identity,
authorization and configuration. This client exposes fixed read operations only;
donor configuration, camera URLs, credentials and playback URLs are never returned.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from k5vision.domain.streams import VideoCodec

MEDIAMTX_VERSION = "v1.21.2"
MAX_RESPONSE_BYTES = 262_144
# A full day of the generated worker's 60-second segments, including its open
# segment, fits this bound. Playback queries independently cover at most one day.
MAX_RECORDING_ITEMS = 2_048
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_STREAM = re.compile(r"[A-Za-z0-9._:-]{1,256}")


class MediaWorkerErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    VERSION_MISMATCH = "version_mismatch"
    BUSY = "busy"
    TIMEOUT = "timeout"


class MediaWorkerError(RuntimeError):
    """Source-free failure; raw donor/network exception text is never retained."""

    def __init__(self, code: MediaWorkerErrorCode) -> None:
        self.code = code
        super().__init__(f"Media worker {code.value}")


@dataclass(frozen=True, slots=True)
class MediaMTXBinding:
    """An enrolled selection, scoped to its owning K5 site."""

    site_id: str
    device_id: UUID
    stream_token: str = "main"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.site_id, str)
            or _SITE.fullmatch(self.site_id) is None
            or not isinstance(self.device_id, UUID)
            or not isinstance(self.stream_token, str)
            or _STREAM.fullmatch(self.stream_token) is None
        ):
            raise ValueError("media binding must contain a bounded site, device and stream")

    @property
    def path_name(self) -> str:
        identity = json.dumps(
            ["k5-mediamtx-v1", self.site_id, self.device_id.hex, self.stream_token],
            separators=(",", ":"),
        ).encode("ascii")
        return "k5_" + hashlib.sha256(identity).hexdigest()


@dataclass(frozen=True, slots=True)
class MediaMTXCredentials:
    """Private service credentials, excluded from diagnostic representations."""

    username: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.username, str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", self.username) is None
            or self.username == "any"
            or not isinstance(self.password, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.password) is None
        ):
            raise ValueError("media service credentials require a named user and opaque secret")


@dataclass(frozen=True, slots=True)
class MediaWorkerInfo:
    version: str
    started_at: datetime
    provider: str = "mediamtx"


@dataclass(frozen=True, slots=True)
class MediaPathState:
    device_id: UUID
    stream_token: str
    available: bool
    online: bool
    video_codecs: tuple[VideoCodec, ...]
    reader_count: int
    inbound_bytes: int
    outbound_bytes: int


@dataclass(frozen=True, slots=True)
class MediaPathConfiguration:
    device_id: UUID
    stream_token: str
    recording_enabled: bool
    source_on_demand: bool
    recording_format: str


@dataclass(frozen=True, slots=True)
class MediaRecordingSegment:
    recording_id: str
    device_id: UUID
    stream_token: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class MediaPlaybackSpan:
    recording_id: str
    device_id: UUID
    stream_token: str
    started_at: datetime
    duration_seconds: float


def _loopback_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        valid = (
            isinstance(value, str)
            and value == value.strip()
            and not any(ord(character) < 32 for character in value)
            and parsed.scheme == "http"
            and address.is_loopback
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("media worker origin must be an explicit loopback HTTP origin")
    return value.rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get(request: Request, timeout: float, max_bytes: int) -> bytes:
    # Do not inherit proxy configuration or forward private Basic credentials
    # through a redirect. The only accepted origins are numeric loopback hosts.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError
        return response.read(max_bytes + 1)


HttpRequester = Callable[[Request, float, int], bytes]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise ValueError
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError
    return result.astimezone(UTC)


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _recording_id(binding: MediaMTXBinding, started: datetime) -> str:
    return hashlib.sha256((binding.path_name + ":" + started.isoformat()).encode()).hexdigest()


class MediaMTXProvider:
    """Bound asynchronous reads of one site-owned, fixed-version local worker.

    Native urllib reads run outside the event loop. Timeout/cancellation does not
    release a capacity slot until the actual thread exits, so a slow native read
    cannot create an unbounded thread backlog. No API mutation method is exposed.
    """

    def __init__(
        self,
        *,
        site_id: str,
        api_origin: str,
        playback_origin: str,
        credentials: MediaMTXCredentials,
        timeout_seconds: float = 3.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_active_requests: int = 4,
        requester: HttpRequester = _http_get,
    ) -> None:
        if not isinstance(site_id, str) or _SITE.fullmatch(site_id) is None:
            raise ValueError("media worker site identifier is invalid")
        if not isinstance(credentials, MediaMTXCredentials) or not callable(requester):
            raise TypeError("media worker credentials and HTTP requester are required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 30
            or type(max_response_bytes) is not int
            or not 1024 <= max_response_bytes <= 1_048_576
            or type(max_active_requests) is not int
            or not 1 <= max_active_requests <= 16
        ):
            raise ValueError("media worker request bounds are invalid")
        self._site_id = site_id
        self._api_origin = _loopback_origin(api_origin)
        self._playback_origin = _loopback_origin(playback_origin)
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_response_bytes
        self._max_active = max_active_requests
        self._requester = requester
        self._requests: set[asyncio.Task] = set()
        self._version_admitted = False

    def _binding(self, binding: MediaMTXBinding) -> str:
        if not isinstance(binding, MediaMTXBinding) or binding.site_id != self._site_id:
            raise ValueError("media selection belongs to a different site")
        return binding.path_name

    def _read(self, origin: str, route: str) -> object | MediaWorkerErrorCode:
        credential = base64.b64encode(
            f"{self._credentials.username}:{self._credentials.password}".encode("ascii")
        ).decode("ascii")
        request = Request(
            origin + route,
            headers={"Authorization": "Basic " + credential, "Accept": "application/json"},
            method="GET",
        )
        try:
            body = self._requester(request, self._timeout_seconds, self._max_bytes)
            if not isinstance(body, bytes) or len(body) > self._max_bytes:
                return MediaWorkerErrorCode.INVALID_RESPONSE
            return json.loads(
                body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                return MediaWorkerErrorCode.UNAUTHORIZED
            if code == 404:
                return MediaWorkerErrorCode.NOT_FOUND
            return MediaWorkerErrorCode.UNAVAILABLE
        except (ValueError, UnicodeError, RecursionError):
            return MediaWorkerErrorCode.INVALID_RESPONSE
        except TimeoutError:
            return MediaWorkerErrorCode.TIMEOUT
        except Exception:
            return MediaWorkerErrorCode.UNAVAILABLE

    def _completed(self, task: asyncio.Task) -> None:
        self._requests.discard(task)
        if not task.cancelled():
            task.exception()

    async def _get(self, origin: str, route: str) -> object:
        if len(self._requests) >= self._max_active:
            raise MediaWorkerError(MediaWorkerErrorCode.BUSY)
        task = asyncio.create_task(asyncio.to_thread(self._read, origin, route))
        self._requests.add(task)
        task.add_done_callback(self._completed)
        timed_out = False
        try:
            result = await asyncio.wait_for(asyncio.shield(task), self._timeout_seconds)
        except TimeoutError:
            timed_out = True
        if timed_out:
            raise MediaWorkerError(MediaWorkerErrorCode.TIMEOUT)
        if isinstance(result, MediaWorkerErrorCode):
            raise MediaWorkerError(result)
        return result

    async def info(self) -> MediaWorkerInfo:
        self._version_admitted = False
        result = await self._get(self._api_origin, "/v3/info")
        if not isinstance(result, dict):
            raise MediaWorkerError(MediaWorkerErrorCode.INVALID_RESPONSE)
        if result.get("version") != MEDIAMTX_VERSION:
            raise MediaWorkerError(MediaWorkerErrorCode.VERSION_MISMATCH)
        invalid = False
        try:
            started = _timestamp(result.get("started"))
        except ValueError:
            invalid = True
        if invalid:
            raise MediaWorkerError(MediaWorkerErrorCode.INVALID_RESPONSE)
        self._version_admitted = True
        return MediaWorkerInfo(MEDIAMTX_VERSION, started)

    async def _admit(self) -> None:
        if not self._version_admitted:
            await self.info()

    async def path_state(self, binding: MediaMTXBinding) -> MediaPathState:
        path = self._binding(binding)
        await self._admit()
        result = await self._get(self._api_origin, "/v3/paths/get/" + quote(path, safe=""))
        invalid = False
        try:
            if not isinstance(result, dict) or result.get("name") != path:
                raise ValueError
            tracks = result.get("tracks2")
            readers = result.get("readers")
            tracks = [] if tracks is None else tracks
            readers = [] if readers is None else readers
            if not isinstance(tracks, list) or not isinstance(readers, list):
                raise ValueError
            if len(tracks) > 32 or len(readers) > MAX_RECORDING_ITEMS:
                raise ValueError
            codecs = set()
            for track in tracks:
                if not isinstance(track, dict) or not isinstance(track.get("codec"), str):
                    raise ValueError
                codec = track["codec"].lower()
                if codec in {"h264", "h265", "mjpeg"}:
                    codecs.add(VideoCodec(codec))
            state = MediaPathState(
                binding.device_id,
                binding.stream_token,
                _boolean(result.get("available")),
                _boolean(result.get("online")),
                tuple(sorted(codecs)),
                len(readers),
                _count(result.get("inboundBytes")),
                _count(result.get("outboundBytes")),
            )
        except (ValueError, TypeError, KeyError):
            invalid = True
        if invalid:
            raise MediaWorkerError(MediaWorkerErrorCode.INVALID_RESPONSE)
        return state

    async def path_configuration(self, binding: MediaMTXBinding) -> MediaPathConfiguration:
        path = self._binding(binding)
        await self._admit()
        result = await self._get(self._api_origin, "/v3/config/paths/get/" + quote(path, safe=""))
        invalid = False
        try:
            if not isinstance(result, dict) or result.get("recordFormat") != "fmp4":
                raise ValueError
            configuration = MediaPathConfiguration(
                binding.device_id,
                binding.stream_token,
                _boolean(result.get("record")),
                _boolean(result.get("sourceOnDemand")),
                "fmp4",
            )
        except (ValueError, TypeError):
            invalid = True
        if invalid:
            raise MediaWorkerError(MediaWorkerErrorCode.INVALID_RESPONSE)
        return configuration

    async def recordings(self, binding: MediaMTXBinding) -> tuple[MediaRecordingSegment, ...]:
        path = self._binding(binding)
        await self._admit()
        try:
            result = await self._get(self._api_origin, "/v3/recordings/get/" + quote(path, safe=""))
        except MediaWorkerError as exc:
            if exc.code is MediaWorkerErrorCode.NOT_FOUND:
                return ()
            raise
        invalid = False
        try:
            if not isinstance(result, dict) or result.get("name") != path:
                raise ValueError
            segments = result.get("segments")
            if not isinstance(segments, list) or len(segments) > MAX_RECORDING_ITEMS:
                raise ValueError
            starts = {_timestamp(segment["start"]) for segment in segments}
            recordings = tuple(
                MediaRecordingSegment(
                    _recording_id(binding, started),
                    binding.device_id,
                    binding.stream_token,
                    started,
                )
                for started in sorted(starts)
            )
        except (ValueError, TypeError, KeyError):
            invalid = True
        if invalid:
            raise MediaWorkerError(MediaWorkerErrorCode.INVALID_RESPONSE)
        return recordings

    async def playback_spans(
        self,
        binding: MediaMTXBinding,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[MediaPlaybackSpan, ...]:
        path = self._binding(binding)
        end = datetime.now(UTC) if end is None else end
        if not isinstance(end, datetime):
            raise ValueError("playback selection requires an aware interval of at most 24 hours")
        start = end - timedelta(hours=24) if start is None else start
        if (
            not isinstance(start, datetime)
            or not isinstance(end, datetime)
            or start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() is None
            or end.utcoffset() is None
            or not timedelta(0) < end - start <= timedelta(hours=24)
        ):
            raise ValueError("playback selection requires an aware interval of at most 24 hours")
        await self._admit()
        query = urlencode(
            {
                "path": path,
                "start": start.astimezone(UTC).isoformat(),
                "end": end.astimezone(UTC).isoformat(),
            }
        )
        try:
            result = await self._get(self._playback_origin, "/list?" + query)
        except MediaWorkerError as exc:
            if exc.code is MediaWorkerErrorCode.NOT_FOUND:
                return ()
            raise
        invalid = False
        try:
            if not isinstance(result, list) or len(result) > MAX_RECORDING_ITEMS:
                raise ValueError
            spans = []
            for item in result:
                started = _timestamp(item["start"])
                duration = item["duration"]
                if (
                    type(duration) not in (int, float)
                    or not 0 < duration <= 7 * 24 * 60 * 60
                    or not math.isfinite(duration)
                ):
                    raise ValueError
                spans.append(
                    MediaPlaybackSpan(
                        _recording_id(binding, started),
                        binding.device_id,
                        binding.stream_token,
                        started,
                        float(duration),
                    )
                )
            normalized = tuple(sorted(spans, key=lambda span: span.started_at))
        except (ValueError, TypeError, KeyError):
            invalid = True
        if invalid:
            raise MediaWorkerError(MediaWorkerErrorCode.INVALID_RESPONSE)
        return normalized
