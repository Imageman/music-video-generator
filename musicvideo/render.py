"""Сборка кадра из слоёв и полный конвейер рендера."""
from __future__ import annotations

import multiprocessing as mp
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from PIL import Image

from . import ffmpeg
from .audio import Analysis, analyze
from .config import Config, TextItemCfg
from .i18n import tr
from .layers import Background, Cover, ImageOverlay, Overlay, ProgressBar, TextLayer, Visualizer
from .layers.background import collect_images

#: Частота дискретизации для анализа и итогового звука.
SAMPLE_RATE = 44100


class FrameRenderer:
    """Держит подготовленные слои и собирает кадр по его номеру."""

    def __init__(self, cfg: Config, analysis: Analysis, duration: float,
                 cover_image: Path | None = None) -> None:
        self.cfg = cfg
        self.analysis = analysis
        self.duration = duration
        self.size = (cfg.video.width, cfg.video.height)
        self.fps = cfg.video.fps

        bg_images = collect_images(cfg.background.image)
        self.background = Background(cfg.background, self.size, duration)
        self.cover = Cover(
            cfg.cover, self.size,
            cover_image or (bg_images[0] if bg_images else None),
        )
        self.visualizer = Visualizer(cfg.visualizer, self.size, analysis)
        self.text = TextLayer(self._text_items(), self.size)
        self.progress = ProgressBar(cfg.progress, self.size, duration)
        self.overlay = Overlay(
            cfg.overlay, self.size, duration,
            grain=cfg.background.grain, grain_size=cfg.background.grain_size,
        )
        self.image_overlay = ImageOverlay(cfg.overlay, self.size)

    def _text_items(self) -> list[TextItemCfg]:
        t = self.cfg.text
        return [t.title, t.artist, *t.extra]

    # ------------------------------------------------------------------ #
    def frame(self, index: int) -> np.ndarray:
        """Готовый кадр (H, W, 3) uint8."""
        a = self.analysis
        i = a.clamp(index)
        t = index / self.fps
        bass = float(a.bass[i])
        beat = float(a.beat[i])
        rng = np.random.default_rng(1000 + index)

        canvas = self.background.frame(t, bass, beat, rng).convert("RGBA")
        self.cover.paste(canvas, t, bass)

        viz = self.visualizer.render(a, i)
        if viz is not None:
            image, pos = viz
            canvas.alpha_composite(image, pos)

        self.text.paste(canvas, t)
        self.progress.paste(canvas, t, self.text_alpha(t))

        arr = np.asarray(canvas.convert("RGB"), dtype=np.float32)
        arr = self.overlay.apply(arr, t, index, beat)
        arr = np.clip(arr, 0.0, 255.0).astype(np.uint8)
        if self.image_overlay.enabled:
            frame = Image.fromarray(arr, "RGB").convert("RGBA")
            scheduled = self.image_overlay.scheduled_alpha(
                self.cfg.overlay, t, self.duration,
            )
            self.image_overlay.paste(
                frame, self.overlay.fade_alpha(t) * scheduled,
            )
            return np.asarray(frame.convert("RGB"), dtype=np.uint8).copy()
        return arr

    def text_alpha(self, t: float) -> float:
        """Общая прозрачность UI — совпадает с появлением заголовка."""
        item = self.cfg.text.title
        if item.fade_in <= 0:
            return 1.0 if t >= item.delay else 0.0
        return float(np.clip((t - item.delay) / item.fade_in, 0.0, 1.0))


# --------------------------------------------------------------------------- #
#  Параллельный рендер
# --------------------------------------------------------------------------- #
_WORKER: FrameRenderer | None = None


def _init_worker(cfg: Config, analysis: Analysis, duration: float,
                 cover: Path | None) -> None:
    global _WORKER
    _WORKER = FrameRenderer(cfg, analysis, duration, cover)


def _render_one(index: int) -> bytes:
    assert _WORKER is not None
    return _WORKER.frame(index).tobytes()


def iter_frames(
    cfg: Config, analysis: Analysis, duration: float, n_frames: int,
    cover: Path | None, workers: int,
) -> Iterator[bytes]:
    """Выдаёт кадры по порядку — в одном процессе или пулом воркеров."""
    if workers <= 1:
        renderer = FrameRenderer(cfg, analysis, duration, cover)
        for i in range(n_frames):
            yield renderer.frame(i).tobytes()
        return

    ctx = mp.get_context("spawn")
    pool = ctx.Pool(
        processes=workers, initializer=_init_worker,
        initargs=(cfg, analysis, duration, cover),
    )
    try:
        chunk = max(1, min(16, n_frames // (workers * 8) or 1))
        yield from pool.imap(_render_one, range(n_frames), chunksize=chunk)
    finally:
        pool.terminate()
        pool.join()


# --------------------------------------------------------------------------- #
#  Конвейер целиком
# --------------------------------------------------------------------------- #
ProgressFn = Callable[[int, int, float], None]


def render_video(cfg: Config, on_progress: ProgressFn | None = None,
                 log: Callable[[str], None] = print) -> Path:
    """Готовит звук, анализирует его и кодирует итоговый ролик."""
    ffmpeg.check_tools()
    _validate(cfg)

    workdir = Path(tempfile.mkdtemp(prefix="musicvideo-"))

    try:
        wav = workdir / "audio.wav"
        log(tr("log.audio"))
        show_progress = bool(getattr(on_progress, "enabled", on_progress is not None))
        duration, src = _render_audio_sequence(
            cfg, wav, workdir, show_progress=show_progress,
        )

        full_frames = max(1, int(round(duration * cfg.video.fps)))
        n_frames = full_frames
        if cfg.preview:
            n_frames = max(1, min(full_frames, int(round(cfg.preview * cfg.video.fps))))

        log(tr("log.analysis"))
        samples = ffmpeg.decode_mono(
            wav, SAMPLE_RATE, duration, show_progress=show_progress,
        )
        analysis = analyze(
            samples, SAMPLE_RATE, cfg.video.fps, cfg.analysis, full_frames,
            show_progress=show_progress,
        )
        del samples

        cover = _resolve_cover(cfg, src, workdir)
        gpu_renderer = _make_gpu_renderer(cfg, analysis, duration, cover, log)
        workers = 1 if gpu_renderer is not None else _worker_count(cfg.workers, n_frames)
        log(tr("log.render", frames=n_frames, width=cfg.video.width,
              height=cfg.video.height, fps=cfg.video.fps, workers=workers))

        started = time.monotonic()
        try:
            encoder = ffmpeg.VideoEncoder(cfg.output, wav, cfg.video)
            if gpu_renderer is not None:
                for i in range(n_frames):
                    encoder.write(gpu_renderer.frame(i))
                    if on_progress is not None:
                        on_progress(i + 1, n_frames, time.monotonic() - started)
            else:
                frames = iter_frames(cfg, analysis, duration, n_frames, cover, workers)
                for i, payload in enumerate(frames, start=1):
                    encoder.write(payload)
                    if on_progress is not None:
                        on_progress(i, n_frames, time.monotonic() - started)
            encoder.close()
        except BaseException:
            if "encoder" in locals():
                encoder.abort()
            raise
        finally:
            if gpu_renderer is not None:
                gpu_renderer.close()

        return Path(cfg.output)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _validate(cfg: Config) -> None:
    """Быстрые проверки до долгой работы: пути, размеры, имена стилей."""
    from .layers.visualizer import STYLES

    collect_images(cfg.background.image)          # бросит, если пути нет
    if not cfg.audio_file:
        raise ValueError(tr("err.no_audio"))
    if not math.isfinite(cfg.audio.track_gap):
        raise ValueError(tr("err.track_gap_invalid"))
    if cfg.overlay.image:
        overlay_path = Path(cfg.overlay.image).expanduser()
        if not overlay_path.is_file():
            raise FileNotFoundError(tr("err.overlay_image_missing", path=overlay_path))
        try:
            with Image.open(overlay_path) as image:
                image.verify()
        except (OSError, ValueError) as exc:
            raise ValueError(tr("err.overlay_image_invalid", path=overlay_path)) from exc
        if (
            isinstance(cfg.overlay.image_width, bool)
            or not isinstance(cfg.overlay.image_width, (int, float))
            or not math.isfinite(cfg.overlay.image_width)
            or not 0 < cfg.overlay.image_width <= 1
        ):
            raise ValueError(tr("err.overlay_image_width"))
        if (
            isinstance(cfg.overlay.image_opacity, bool)
            or not isinstance(cfg.overlay.image_opacity, (int, float))
            or not math.isfinite(cfg.overlay.image_opacity)
            or not 0 <= cfg.overlay.image_opacity <= 1
        ):
            raise ValueError(tr("err.overlay_image_opacity"))
        try:
            position = cfg.overlay.image_position
            valid_position = (
                isinstance(position, (list, tuple))
                and len(position) == 2
                and all(
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and 0 <= value <= 1
                    for value in position
                )
            )
        except (TypeError, ValueError):
            valid_position = False
        if not valid_position:
            raise ValueError(tr("err.overlay_image_position"))
    if cfg.cover.enabled and cfg.cover.image:
        if not Path(cfg.cover.image).expanduser().exists():
            raise FileNotFoundError(tr("err.cover_missing", path=cfg.cover.image))

    if cfg.visualizer.style not in STYLES:
        raise ValueError(
            tr("err.unknown_style", name=cfg.visualizer.style,
              available=", ".join(STYLES))
        )
    if cfg.renderer not in ("cpu", "gpu", "auto"):
        raise ValueError("renderer must be cpu, gpu, or auto")
    if (isinstance(cfg.gpu.texture_cache_mb, bool)
            or not isinstance(cfg.gpu.texture_cache_mb, int)
            or cfg.gpu.texture_cache_mb < 1):
        raise ValueError("gpu.texture_cache_mb must be a positive integer")
    for field in ("wave_strength", "ripple_strength", "glow_strength",
                  "color_shift_strength", "audio_light_strength", "contrast",
                  "neon_gate_softness", "neon_threshold_softness",
                  "neon_heat_blur_radius", "neon_blur_radius", "neon_strength",
                  "neon_release_half_life", "neon_saturation_attack",
                  "neon_saturation_release", "neon_steady_power", "neon_loudness_gamma",
                  "neon_steady_dip_width"):
        value = getattr(cfg.gpu, field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError(f"gpu.{field} must be a finite non-negative number")
    for field in (
        "neon_bass_gate", "neon_mid_gate", "neon_high_gate",
        "neon_bass_threshold", "neon_mid_threshold", "neon_high_threshold",
        "neon_bass_expansion", "neon_mid_expansion", "neon_high_expansion",
        "neon_adaptation_margin", "neon_adaptation_range",
        "neon_steady_strength",
    ):
        value = getattr(cfg.gpu, field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError(f"gpu.{field} must be between 0 and 1")
    if cfg.gpu.neon_gate_softness > 1.0:
        raise ValueError("gpu.neon_gate_softness must be between 0 and 1")
    for band in ("bass", "mid", "high"):
        for name in ("adaptation_margin", "adaptation_range", "saturation_attack", "saturation_release"):
            field = f"neon_{band}_{name}"
            value = getattr(cfg.gpu, field)
            if value is None:
                continue
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value))
            if valid:
                valid = (0 <= value <= 1 if name == "adaptation_margin" else
                         0 < value <= 1 if name == "adaptation_range" else value > 0)
            if not valid:
                raise ValueError(f"gpu.{field} must be a valid finite number or null")
    for field in ("neon_loudness_floor_db", "neon_loudness_full_db"):
        value = getattr(cfg.gpu, field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"gpu.{field} must be finite")
    if cfg.gpu.neon_loudness_floor_db >= cfg.gpu.neon_loudness_full_db:
        raise ValueError("neon loudness floor must be below full-level reference")
    for field in (
        "neon_release_half_life", "neon_saturation_attack",
        "neon_saturation_release",
        "neon_adaptation_range",
        "neon_steady_power", "neon_loudness_gamma", "neon_steady_dip_width",
    ):
        if getattr(cfg.gpu, field) <= 0.0:
            raise ValueError(f"gpu.{field} must be greater than 0")
    if not 0.001 <= cfg.gpu.neon_threshold_softness <= 0.5:
        raise ValueError("gpu.neon_threshold_softness must be between 0.001 and 0.5")
    if cfg.gpu.neon_blur_radius > 256.0:
        raise ValueError(
            "gpu.neon_blur_radius must be between 0 and 1 of the short side "
            "(or up to 256 legacy pixels)"
        )
    if cfg.gpu.neon_heat_blur_radius > 256.0:
        raise ValueError("gpu.neon_heat_blur_radius must be between 0 and 256 pixels")
    if cfg.gpu.neon_strength > 8.0:
        raise ValueError("gpu.neon_strength must be between 0 and 8")
    if not isinstance(cfg.gpu.effects, list) or any(
        not isinstance(name, str) for name in cfg.gpu.effects
    ):
        raise ValueError("gpu.effects must be a list of effect names")
    for field in ("image_intro_duration", "image_outro_duration",
                  "image_fade_in", "image_fade_out"):
        value = getattr(cfg.overlay, field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError(f"overlay.{field} must be a finite non-negative number")
    if cfg.video.width < 16 or cfg.video.height < 16:
        raise ValueError(
            tr("err.frame_too_small", width=cfg.video.width, height=cfg.video.height)
        )
    if cfg.video.width % 2 or cfg.video.height % 2:
        raise ValueError(
            tr("err.odd_size", pixel_format=cfg.video.pixel_format,
              width=cfg.video.width, height=cfg.video.height)
        )
    if cfg.video.fps < 1:
        raise ValueError(tr("err.bad_fps", value=cfg.video.fps))
    if cfg.video.encoder not in ("libx264", "h264_nvenc"):
        raise ValueError(tr("err.video_encoder"))
    if (
        isinstance(cfg.video.cq, bool)
        or not isinstance(cfg.video.cq, int)
        or not 1 <= cfg.video.cq <= 51
    ):
        raise ValueError(tr("err.video_cq"))
    if not isinstance(cfg.video.nvenc_preset, str) or cfg.video.nvenc_preset not in tuple(
        f"p{i}" for i in range(1, 8)
    ):
        raise ValueError(tr("err.nvenc_preset"))
    if cfg.audio.speed <= 0:
        raise ValueError(tr("err.bad_speed", value=cfg.audio.speed))


def _worker_count(requested: int, n_frames: int) -> int:
    if requested > 0:
        return max(1, requested)
    cpus = os.cpu_count() or 2
    return max(1, min(cpus - 1 if cpus > 2 else 1, max(1, n_frames // 40)))


def _make_gpu_renderer(cfg: Config, analysis: Analysis, duration: float,
                       cover: Path | None,
                       log: Callable[[str], None] | None = None):
    """Select GPU explicitly or fall back to CPU only in auto mode."""
    if cfg.renderer == "cpu":
        return None
    from .gpu.context import GPUUnavailable
    from .gpu.renderer import GPUFrameRenderer

    try:
        renderer = GPUFrameRenderer(cfg, analysis, duration, cover)
    except GPUUnavailable as exc:
        if cfg.renderer == "gpu":
            raise ValueError(f"GPU renderer requested but unavailable: {exc}") from exc
        if log is not None:
            log(tr("log.renderer.cpu_fallback", reason=exc))
        return None
    if log is not None:
        log(tr("log.renderer.gpu"))
    return renderer


def _resolve_cover(cfg: Config, audio: Path, workdir: Path) -> Path | None:
    """Обложка: из конфига, из тегов файла или первая картинка фона."""
    if not cfg.cover.enabled:
        return None
    if cfg.cover.image:
        path = Path(cfg.cover.image).expanduser()
        if not path.exists():
            raise FileNotFoundError(tr("err.cover_missing", path=path))
        return path

    embedded = ffmpeg.extract_cover(audio, workdir / "cover.jpg")
    if embedded is not None:
        return embedded

    images = collect_images(cfg.background.image)
    return images[0] if images else None


def _audio_sources(spec: str | list[str]) -> list[Path]:
    items = spec if isinstance(spec, list) else [spec]
    return [Path(item).expanduser() for item in items]


def _render_audio_sequence(
    cfg: Config, wav: Path, workdir: Path, *, show_progress: bool = False,
) -> tuple[float, Path]:
    """Обрабатывает треки отдельно, затем собирает общий звуковой ряд."""
    sources = _audio_sources(cfg.audio_file)
    if not sources:
        raise ValueError(tr("err.no_audio"))
    infos = [ffmpeg.probe(source) for source in sources]
    if len(sources) == 1:
        duration = ffmpeg.render_audio(
            sources[0], wav, cfg.audio, infos[0], SAMPLE_RATE,
            show_progress=show_progress,
        )
        return duration, sources[0]

    parts: list[Path] = []
    for index, (source, info) in enumerate(zip(sources, infos)):
        part = workdir / f"track-{index:04d}.wav"
        ffmpeg.render_audio(
            source, part, cfg.audio, info, SAMPLE_RATE, apply_fades=False,
            show_progress=show_progress,
        )
        parts.append(part)
    duration = ffmpeg.join_audio(
        parts, wav, cfg.audio, SAMPLE_RATE, show_progress=show_progress,
    )
    return duration, sources[0]


def render_still(cfg: Config, at: float = 0.0) -> Image.Image:
    """Один кадр без кодирования — для быстрой проверки настроек."""
    ffmpeg.check_tools()
    _validate(cfg)
    workdir = Path(tempfile.mkdtemp(prefix="musicvideo-still-"))
    try:
        wav = workdir / "audio.wav"
        duration, src = _render_audio_sequence(cfg, wav, workdir)
        samples = ffmpeg.decode_mono(wav, SAMPLE_RATE, duration)
        n_frames = max(1, int(round(duration * cfg.video.fps)))
        analysis = analyze(samples, SAMPLE_RATE, cfg.video.fps, cfg.analysis, n_frames)
        cover = _resolve_cover(cfg, src, workdir)
        index = int(round(float(np.clip(at, 0.0, duration)) * cfg.video.fps))
        gpu_renderer = _make_gpu_renderer(cfg, analysis, duration, cover)
        if gpu_renderer is not None:
            try:
                payload = gpu_renderer.frame(min(index, n_frames - 1))
                return Image.frombytes("RGB", (cfg.video.width, cfg.video.height), payload)
            finally:
                gpu_renderer.close()
        renderer = FrameRenderer(cfg, analysis, duration, cover)
        return Image.fromarray(renderer.frame(min(index, n_frames - 1)), "RGB")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
