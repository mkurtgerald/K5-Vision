"""Killable MP4 decoder; its sole private file selection arrives over stdin."""

from __future__ import annotations

import json
import math
import stat
import struct
import sys
from pathlib import Path


def _boxes(source, first, last):
    position = first
    count = 0
    while position < last:
        count += 1
        if count > 100000 or last - position < 8:
            raise ValueError
        source.seek(position)
        length, kind = struct.unpack(">I4s", source.read(8))
        header = 8
        if length == 1:
            if last - position < 16:
                raise ValueError
            length = struct.unpack(">Q", source.read(8))[0]
            header = 16
        elif length == 0:
            length = last - position
        if not header <= length <= last - position:
            raise ValueError
        yield position + header, position + length, kind
        position += length


def _child(source, parent, kind, *, optional=False):
    matches = [item for item in _boxes(source, parent[0], parent[1]) if item[2] == kind]
    if not matches and optional:
        return None
    if len(matches) != 1:
        raise ValueError
    return matches[0]


def _clock(source, box):
    source.seek(box[0])
    header = source.read(min(32, box[1] - box[0]))
    if len(header) < 20:
        raise ValueError
    if header[0] == 0:
        scale, duration = struct.unpack(">II", header[12:20])
    elif header[0] == 1 and len(header) >= 32:
        scale, duration = struct.unpack(">IQ", header[20:32])
    else:
        raise ValueError
    if not 1 <= scale <= 1_000_000_000:
        raise ValueError
    return scale, duration


def _presentation_duration(path: Path) -> float:
    """Read the movie timeline, including edits that trim decoder preroll.

    Raw sample counts can include a preceding key frame and its GOP. They are
    not the displayed duration of a MediaMTX MP4 with an edit list.
    """
    size = path.stat().st_size
    with path.open("rb") as source:
        movie = _child(source, (0, size), b"moov")
        scale, duration = _clock(source, _child(source, movie, b"mvhd"))
        seconds = duration / scale
        if not math.isfinite(seconds) or not 0 < seconds <= 3605:
            raise ValueError
        return seconds


def _runs(source, box, *, composition=False):
    source.seek(box[0])
    header = source.read(8)
    if len(header) != 8 or header[0] not in ({0, 1} if composition else {0}):
        raise ValueError
    count = struct.unpack(">I", header[4:8])[0]
    if not 1 <= count <= 100000 or box[1] - box[0] != 8 + count * 8:
        raise ValueError
    payload = source.read(count * 8)
    result = list(struct.iter_unpack(">Ii" if composition and header[0] == 1 else ">II", payload))
    if any(samples < 1 or (not composition and value < 1) for samples, value in result):
        raise ValueError
    return result


def _leading_video_offset(path: Path) -> float:
    """Derive the first retained presentation sample's shift after its edit.

    FFmpeg/OpenCV rebases that first sample to zero. Its fraction of a frame
    between the edit boundary and first retained PTS remains in movie duration.
    Intersect timing runs in closed form, including B-frame composition offsets.
    """
    with path.open("rb") as source:
        movie = _child(source, (0, path.stat().st_size), b"moov")
        movie_scale, _duration = _clock(source, _child(source, movie, b"mvhd"))
        tracks = [item for item in _boxes(source, movie[0], movie[1]) if item[2] == b"trak"]
        for track in tracks:
            media = _child(source, track, b"mdia")
            handler = _child(source, media, b"hdlr")
            source.seek(handler[0])
            if source.read(12)[8:12] != b"vide":
                continue
            scale, _duration = _clock(source, _child(source, media, b"mdhd"))
            edit_container = _child(source, track, b"edts", optional=True)
            if edit_container is None:
                return 0
            edit = _child(source, edit_container, b"elst")
            source.seek(edit[0])
            header = source.read(8)
            if len(header) != 8 or header[0] not in {0, 1}:
                raise ValueError
            count = struct.unpack(">I", header[4:8])[0]
            width = 20 if header[0] == 1 else 12
            if not 1 <= count <= 2 or edit[1] - edit[0] != 8 + count * width:
                raise ValueError
            entries = list(
                struct.iter_unpack(">Qqhh" if width == 20 else ">Iihh", source.read(count * width))
            )
            empty = 0.0
            cutoff = None
            for index, (length, at, rate, fraction) in enumerate(entries):
                if rate != 1 or fraction != 0:
                    raise ValueError
                if index == 0 and at == -1 and count == 2:
                    empty = length / movie_scale
                elif at >= 0 and index == count - 1:
                    cutoff = at
                else:
                    raise ValueError
            if cutoff is None:
                raise ValueError
            table = _child(source, _child(source, media, b"minf"), b"stbl")
            timing = _runs(source, _child(source, table, b"stts"))
            offsets = _child(source, table, b"ctts", optional=True)
            composition = (
                _runs(source, offsets, composition=True)
                if offsets is not None
                else [(sum(n for n, _ in timing), 0)]
            )
            if sum(n for n, _ in timing) != sum(n for n, _ in composition):
                raise ValueError
            index = 0
            remaining, offset = composition[0]
            dts = 0
            first_pts = None
            for samples, delta in timing:
                while samples:
                    take = min(samples, remaining)
                    pts = dts + offset
                    step = max(0, (cutoff - pts + delta - 1) // delta)
                    if step < take:
                        candidate = pts + step * delta
                        first_pts = candidate if first_pts is None else min(first_pts, candidate)
                    dts += take * delta
                    samples -= take
                    remaining -= take
                    if remaining == 0 and index + 1 < len(composition):
                        index += 1
                        remaining, offset = composition[index]
            if first_pts is None:
                raise ValueError
            return empty + (first_pts - cutoff) / scale
    raise ValueError


def decode(path: str) -> dict:
    # The parent owns directory custody and checks unchanged identity around
    # this operation. This child accepts neither a URL nor an external codec.
    file = Path(path)
    if not file.is_absolute() or ".." in file.parts:
        raise ValueError
    before = file.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError
    duration = _presentation_duration(file)
    leading = _leading_video_offset(file)
    video_duration = duration - leading
    if not math.isfinite(leading) or not 0 <= leading < duration:
        raise ValueError
    import cv2

    capture = cv2.VideoCapture(
        str(file),
        cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1500, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 1500],
    )
    try:
        if not capture.isOpened():
            raise ValueError
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 33_554_432):
            raise ValueError
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(count) or count < 1 or not math.isfinite(fps) or fps <= 0:
            raise ValueError
        success, frame = capture.read()
        if not success or frame is None or not frame.size:
            raise ValueError
        # Seek in the displayed timeline, not the raw sample index. Edit-list
        # preroll samples are deliberately absent from that presented timeline.
        if not capture.set(
            cv2.CAP_PROP_POS_MSEC, max(0, video_duration - min(video_duration / 2, 1)) * 1000
        ):
            raise ValueError
        tail = False
        for _ in range(4096):
            success, frame = capture.read()
            if not success or frame is None or not frame.size:
                break
            elapsed = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if (
                math.isfinite(elapsed)
                and max(0, video_duration - max(0.1, 1.5 / fps))
                <= elapsed
                <= video_duration + 1 / fps
            ):
                tail = True
                break
        if not tail:
            raise ValueError
        after = file.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
        return {
            "frame_count": count,
            "fps": fps,
            "duration": duration,
            "leading_offset": leading,
            "tail_time": elapsed,
            "first": True,
            "last": True,
        }
    finally:
        capture.release()


def main() -> int:
    try:
        request = sys.stdin.buffer.read(8193)
        if len(request) > 8192:
            return 1
        document = json.loads(request)
        if (
            type(document) is not dict
            or document.keys() != {"path"}
            or not isinstance(document["path"], str)
            or not 1 <= len(document["path"]) <= 4096
            or "://" in document["path"]
        ):
            return 1
        result = decode(document["path"])
        sys.stdout.buffer.write(json.dumps(result, allow_nan=False).encode("ascii"))
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
