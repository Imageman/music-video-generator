"""Мост к ffmpeg: декодирование звука, аудиоэффекты и кодирование видео."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
import threading

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from tqdm import tqdm

from .config import AudioCfg, VideoCfg
from .i18n import tr


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe отсутствует или завершился с ошибкой."""


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise FFmpegError(
            tr("err.tool_missing", name=name)
        )
    return path


def check_tools() -> None:
    """Проверяет наличие ffmpeg и ffprobe до начала долгой работы."""
    _tool("ffmpeg")
    _tool("ffprobe")


def _run(
    cmd: list[str], *, progress_total: float | None = None,
    progress_desc: str = "", show_progress: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    if (not show_progress or progress_total is None or progress_total <= 0
            or not sys.stderr.isatty()):
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-12:]
            raise FFmpegError(
                tr("err.tool_failed", tool=Path(cmd[0]).stem,
                   code=proc.returncode, details="\n  ".join(tail))
            )
        return proc

    progress_cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    process = subprocess.Popen(
        progress_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    errors: list[str] = []
    bar = tqdm(
        total=progress_total, desc=progress_desc, unit="s", ascii=True,
        mininterval=2.0, dynamic_ncols=True, file=sys.stderr, leave=False,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if "=" not in line:
                if line:
                    errors.append(line)
                continue
            key, value = line.split("=", 1)
            if key == "out_time" and value != "N/A":
                try:
                    hours, minutes, seconds = value.split(":")
                    current = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
                except ValueError:
                    continue
                target = min(progress_total, current)
                if target > bar.n:
                    bar.update(target - bar.n)
            elif key == "progress" and value == "end" and bar.n < progress_total:
                bar.update(progress_total - bar.n)
        returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait()
        raise
    finally:
        bar.close()

    if returncode != 0:
        raise FFmpegError(
            tr("err.tool_failed", tool=Path(cmd[0]).stem,
               code=returncode, details="\n  ".join(errors[-12:]))
        )
    return subprocess.CompletedProcess(cmd, returncode, b"", b"")


# --------------------------------------------------------------------------- #
#  Метаданные
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MediaInfo:
    duration: float
    sample_rate: int
    channels: int
    title: str | None = None
    artist: str | None = None


def probe(path: str | Path) -> MediaInfo:
    """Читает длительность, параметры аудиопотока и теги файла."""
    p = Path(path)
    if not p.exists():
        raise FFmpegError(tr("err.audio_missing", path=p))

    out = _run([
        _tool("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-select_streams", "a:0", str(p),
    ]).stdout
    data = json.loads(out or b"{}")

    streams = data.get("streams") or []
    if not streams:
        raise FFmpegError(tr("err.no_audio_stream", path=p))
    stream = streams[0]
    fmt = data.get("format") or {}

    duration = float(fmt.get("duration") or stream.get("duration") or 0.0)
    if duration <= 0:
        raise FFmpegError(tr("err.no_duration", path=p))

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    tags.update({k.lower(): v for k, v in (stream.get("tags") or {}).items()})

    return MediaInfo(
        duration=duration,
        sample_rate=int(stream.get("sample_rate") or 44100),
        channels=int(stream.get("channels") or 2),
        title=tags.get("title"),
        artist=tags.get("artist") or tags.get("album_artist"),
    )


# --------------------------------------------------------------------------- #
#  Цепочка аудиоэффектов
# --------------------------------------------------------------------------- #
def build_audio_filters(
    cfg: AudioCfg,
    sample_rate: int,
    out_duration: float,
    apply_fades: bool = True,
) -> list[str]:
    """Собирает -af цепочку: скорость -> бас -> реверб -> громкость -> фейды."""
    f: list[str] = []

    if abs(cfg.speed - 1.0) > 1e-3:
        # asetrate меняет и темп, и высоту — это и есть эффект "slowed"/"sped up"
        f.append(f"asetrate={int(round(sample_rate * cfg.speed))}")
        f.append(f"aresample={sample_rate}")

    if abs(cfg.bass_boost) > 1e-3:
        f.append(f"bass=g={cfg.bass_boost:.2f}:f=110:width_type=q:w=0.7")

    if cfg.reverb > 1e-3:
        f.extend(_reverb_filters(cfg.reverb, cfg.reverb_room))

    if cfg.normalize:
        f.append("loudnorm=I=-14:TP=-1.5:LRA=11")
        f.append(f"aresample={sample_rate}")

    if apply_fades and cfg.fade_in > 0:
        f.append(f"afade=t=in:st=0:d={cfg.fade_in:.3f}")
    if apply_fades and cfg.fade_out > 0 and out_duration > cfg.fade_out:
        start = max(0.0, out_duration - cfg.fade_out)
        f.append(f"afade=t=out:st={start:.3f}:d={cfg.fade_out:.3f}")

    f.append("alimiter=limit=0.97:level=false")
    return f


def _reverb_filters(amount: float, room_ms: int) -> list[str]:
    """Две каскадные ступени aecho — плотный хвост без внешнего IR-файла."""
    amount = float(np.clip(amount, 0.0, 1.0))
    room = max(40, int(room_ms))

    # ранние отражения (взаимно непериодичные задержки дают меньше "флэнжера")
    early_d = [int(room * k) for k in (0.17, 0.29, 0.41)]
    early_g = [round(amount * g, 3) for g in (0.55, 0.42, 0.33)]
    # хвост
    late_d = [int(room * k) for k in (0.61, 0.83, 1.0)]
    late_g = [round(amount * g, 3) for g in (0.45, 0.34, 0.26)]

    out_gain = round(1.0 - 0.22 * amount, 3)
    return [
        "aecho=0.86:{g}:{d}:{dec}".format(
            g=out_gain, d="|".join(map(str, early_d)), dec="|".join(map(str, early_g))
        ),
        "aecho=0.9:{g}:{d}:{dec}".format(
            g=out_gain, d="|".join(map(str, late_d)), dec="|".join(map(str, late_g))
        ),
    ]


def render_audio(
    src: str | Path,
    dst: str | Path,
    cfg: AudioCfg,
    info: MediaInfo,
    sample_rate: int = 44100,
    *,
    apply_fades: bool = True,
    show_progress: bool = True,
) -> float:
    """Применяет эффекты и пишет WAV, который пойдёт и в анализ, и в видео.

    Возвращает фактическую длительность результата в секундах.
    """
    src_span = max(0.0, info.duration - cfg.start)
    if cfg.duration is not None:
        src_span = min(src_span, max(0.0, cfg.duration))
    if src_span <= 0:
        raise FFmpegError(
            tr("err.empty_span", start=cfg.start, duration=info.duration)
        )
    out_duration = src_span / max(cfg.speed, 1e-6)

    cmd = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    if cfg.start > 0:
        cmd += ["-ss", f"{cfg.start:.3f}"]
    cmd += ["-i", str(src)]
    if cfg.duration is not None:
        cmd += ["-t", f"{src_span:.3f}"]

    filters = build_audio_filters(cfg, sample_rate, out_duration, apply_fades)
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-ac", "2", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    _run(
        cmd, progress_total=out_duration,
        progress_desc=tr("progress.audio_track", name=Path(src).name),
        show_progress=show_progress,
    )

    return probe(dst).duration


def expected_audio_duration(sources: list[str | Path], cfg: AudioCfg) -> float:
    """Оценивает длительность списка треков для предпросмотра кадра."""
    if not math.isfinite(cfg.track_gap):
        raise FFmpegError(tr("err.track_gap_invalid"))
    total = 0.0
    for source in sources:
        info = probe(source)
        span = max(0.0, info.duration - cfg.start)
        if cfg.duration is not None:
            span = min(span, max(0.0, cfg.duration))
        if span <= 0:
            raise FFmpegError(tr("err.empty_span", start=cfg.start, duration=info.duration))
        total += span / max(cfg.speed, 1e-6)
    total += max(0, len(sources) - 1) * cfg.track_gap
    return max(0.0, total)


def join_audio(
    sources: list[str | Path],
    dst: str | Path,
    cfg: AudioCfg,
    sample_rate: int = 44100,
    *,
    show_progress: bool = True,
) -> float:
    """Собирает обработанные WAV с паузами или equal-power кроссфейдами."""
    if len(sources) < 2:
        raise ValueError("join_audio requires at least two tracks")

    durations = [probe(source).duration for source in sources]
    overlap = max(0.0, -cfg.track_gap)
    if overlap > 0:
        for index, duration in enumerate(durations):
            required = overlap * (2 if 0 < index < len(durations) - 1 else 1)
            if duration <= required:
                raise FFmpegError(tr(
                    "err.track_overlap_long",
                    overlap=f"{overlap:g}", duration=f"{duration:g}",
                    required=f"{required:g}",
                ))

    cmd = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        cmd += ["-i", str(source)]

    silence_ids: list[int] = []
    if cfg.track_gap > 0:
        for _ in range(len(sources) - 1):
            silence_ids.append(len(sources) + len(silence_ids))
            cmd += [
                "-f", "lavfi", "-t", f"{cfg.track_gap:.6f}", "-i",
                f"anullsrc=r={sample_rate}:cl=stereo",
            ]

    filters: list[str] = []
    current = "0:a"
    for index in range(len(sources) - 1):
        output = f"seq{index}"
        if cfg.track_gap < 0:
            filters.append(
                f"[{current}][{index + 1}:a]acrossfade="
                f"d={overlap:.6f}:c1=qsin:c2=qsin[{output}]"
            )
        elif cfg.track_gap > 0:
            silence = silence_ids[index]
            filters.append(
                f"[{current}][{silence}:a][{index + 1}:a]"
                f"concat=n=3:v=0:a=1[{output}]"
            )
        else:
            filters.append(
                f"[{current}][{index + 1}:a]concat=n=2:v=0:a=1[{output}]"
            )
        current = output

    duration = sum(durations) + cfg.track_gap * (len(sources) - 1)
    if duration <= 0 or not math.isfinite(duration):
        raise FFmpegError(tr("err.track_mix_duration"))

    final_filters: list[str] = []
    if cfg.fade_in > 0:
        final_filters.append(f"afade=t=in:st=0:d={cfg.fade_in:.3f}")
    if cfg.fade_out > 0 and duration > cfg.fade_out:
        start = max(0.0, duration - cfg.fade_out)
        final_filters.append(f"afade=t=out:st={start:.3f}:d={cfg.fade_out:.3f}")
    final_filters.append("alimiter=limit=0.97:level=false")
    filters.append(f"[{current}]{','.join(final_filters)}[out]")

    cmd += [
        "-filter_complex", ";".join(filters), "-map", "[out]",
        "-ac", "2", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst),
    ]
    _run(
        cmd, progress_total=duration,
        progress_desc=tr("progress.audio_join"), show_progress=show_progress,
    )
    return probe(dst).duration


def decode_mono(
    path: str | Path,
    sample_rate: int = 44100,
    expected_duration: float | None = None,
    *,
    show_progress: bool = True,
) -> np.ndarray:
    """Декодирует моно float32 и показывает прогресс по прочитанным сэмплам."""
    command = [
        _tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-f", "f32le", "-acodec", "pcm_f32le",
        "-ac", "1", "-ar", str(sample_rate), "-",
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_data = bytearray()

    def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(8192)
            if not chunk:
                return
            stderr_data.extend(chunk)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    total_samples = (
        max(1, round(expected_duration * sample_rate))
        if expected_duration is not None else None
    )
    initial_bytes = total_samples * 4 if total_samples is not None else 0
    audio_bytes = bytearray(initial_bytes)
    offset = 0
    bar = tqdm(
        total=total_samples, desc=tr("progress.audio_decode"),
        unit="sample", unit_scale=True, ascii=True, mininterval=2.0,
        dynamic_ncols=True, file=sys.stderr,
        disable=not show_progress or not sys.stderr.isatty(), leave=False,
    )
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            end = offset + len(chunk)
            if end > len(audio_bytes):
                audio_bytes.extend(bytes(end - len(audio_bytes)))
            audio_bytes[offset:end] = chunk
            offset = end
            decoded_samples = offset // 4
            if decoded_samples > bar.n:
                bar.update(decoded_samples - bar.n)
        returncode = proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
        raise
    finally:
        bar.close()
        stderr_thread.join()

    if returncode != 0:
        tail = stderr_data.decode("utf-8", "replace").strip().splitlines()[-8:]
        raise FFmpegError(tr("err.decode_failed", details="\n  ".join(tail)))
    del audio_bytes[offset:]
    return np.frombuffer(audio_bytes, dtype="<f4").astype(np.float32, copy=False)


# --------------------------------------------------------------------------- #
#  Кодирование видео
# --------------------------------------------------------------------------- #
class VideoEncoder:
    """Принимает RGB-кадры в stdin ffmpeg и склеивает их с готовым звуком."""

    def __init__(
        self,
        output: str | Path,
        audio_path: str | Path,
        video: VideoCfg,
        audio_bitrate: str | None = None,
    ) -> None:
        self.output = Path(output)
        self.output.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{video.width}x{video.height}", "-r", str(video.fps),
            "-i", "-",
            "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
        ]
        if video.encoder == "h264_nvenc":
            # NVENC's CQ scale is separate from x264 CRF. VBR with no bitrate
            # ceiling lets -cq control quality while bitrate follows the scene.
            cmd.extend([
                "-c:v", "h264_nvenc", "-preset", video.nvenc_preset,
                "-rc", "vbr", "-cq", str(video.cq), "-b:v", "0",
            ])
            # FFmpeg's automatic NVENC surface count can grow too large for
            # streamed 4K raw frames. Keep the in-flight GPU frame pool bounded.
            if video.width * video.height >= 3840 * 2160:
                cmd.extend(["-surfaces", "8"])
        elif video.encoder == "libx264":
            cmd.extend([
                "-c:v", "libx264", "-preset", video.preset,
                "-crf", str(video.crf),
            ])
        else:
            raise ValueError(f"Unsupported video encoder: {video.encoder}")
        cmd.extend([
            "-pix_fmt", video.pixel_format,
            "-c:a", "aac", "-b:a", audio_bitrate or video.audio_bitrate,
            "-movflags", "+faststart", "-shortest", str(self.output),
        ])
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._closed = False
        # 4K NVENC can encode faster than real time and FFmpeg then buffers
        # raw frames faster than the long-running stream can drain them.
        self._frame_interval = (
            1.0 / video.fps
            if video.width * video.height >= 3840 * 2160 and video.fps > 0
            else 0.0
        )
        self._last_frame_write_at: float | None = None

    def write(self, frame: np.ndarray | bytes) -> None:
        """Пишет один кадр: массив (H, W, 3) uint8 или уже готовые байты."""
        assert self._proc.stdin is not None
        if self._frame_interval and self._last_frame_write_at is not None:
            elapsed = time.perf_counter() - self._last_frame_write_at
            if elapsed < self._frame_interval:
                time.sleep(self._frame_interval - elapsed)
        self._last_frame_write_at = time.perf_counter()
        data = frame if isinstance(frame, (bytes, bytearray, memoryview)) else frame.tobytes()
        try:
            self._proc.stdin.write(data)
        except (BrokenPipeError, OSError) as e:
            raise FFmpegError(
                self._stderr_tail() or tr("err.pipe_closed", message=e)
            ) from e

    def _stderr_tail(self) -> str:
        if self._proc.stderr is None:
            return ""
        try:
            data = self._proc.stderr.read() or b""
        except Exception:
            return ""
        lines = data.decode("utf-8", "replace").strip().splitlines()[-12:]
        return "ffmpeg:\n  " + "\n  ".join(lines) if lines else ""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc.stdin:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        err = self._proc.stderr.read() if self._proc.stderr else b""
        code = self._proc.wait()
        if code != 0:
            lines = err.decode("utf-8", "replace").strip().splitlines()[-12:]
            raise FFmpegError(
                tr("err.encode_failed", code=code, details="\n  ".join(lines))
            )

    def abort(self) -> None:
        """Аварийно завершает ffmpeg (например, по Ctrl+C)."""
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.kill()
        except OSError:
            pass
        finally:
            self._proc.wait()

    def __enter__(self) -> VideoEncoder:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


def extract_cover(audio_path: str | Path, dst: str | Path) -> Path | None:
    """Вытаскивает встроенную обложку из тегов, если она есть."""
    dst = Path(dst)
    proc = subprocess.run(
        [
            _tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(audio_path), "-an", "-vcodec", "copy", str(dst),
        ],
        capture_output=True,
    )
    if proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
        return dst
    if dst.exists():
        dst.unlink(missing_ok=True)
    return None
