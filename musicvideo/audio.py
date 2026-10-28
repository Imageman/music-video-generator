"""Анализ звука: спектр по кадрам, огибающие баса/середины/верха и биты."""
from __future__ import annotations

from dataclasses import dataclass
import sys

import numpy as np
from numba import njit
from tqdm import tqdm

from .config import AnalysisCfg
from .i18n import tr

#: Сколько кадров считаем за один проход FFT (компромисс память/скорость).
_CHUNK = 256

#: Ниже этого уровня (дБ) сигнал считаем тишиной и не растягиваем на него шкалу.
_SILENCE_REF_DB = -50.0


@dataclass
class Analysis:
    """Покадровые данные для визуализации. Все значения нормированы в 0..1."""

    fps: int
    n_frames: int
    #: (n_frames, n_bands) — уровни частотных полос
    bands: np.ndarray
    #: центральные частоты полос, Гц
    freqs: np.ndarray
    #: (n_frames,) — огибающие по диапазонам
    bass: np.ndarray
    mid: np.ndarray
    high: np.ndarray
    #: (n_frames,) — общая громкость
    level: np.ndarray
    #: (n_frames,) — импульс на ударе (быстрая атака, плавный спад)
    beat: np.ndarray

    def __len__(self) -> int:
        return self.n_frames

    def clamp(self, index: int) -> int:
        return 0 if self.n_frames == 0 else min(max(index, 0), self.n_frames - 1)


# --------------------------------------------------------------------------- #
#  Вспомогательные функции
# --------------------------------------------------------------------------- #
def _triangular_bank(
    n_bands: int, n_bins: int, sample_rate: int, n_fft: int,
    fmin: float, fmax: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Банк треугольных фильтров с логарифмическим шагом по частоте."""
    nyquist = sample_rate / 2.0
    fmax = min(fmax, nyquist * 0.98)
    fmin = max(fmin, sample_rate / n_fft)
    edges = np.geomspace(fmin, fmax, n_bands + 2)
    bin_freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)

    bank = np.zeros((n_bands, n_bins), dtype=np.float32)
    for b in range(n_bands):
        lo, ctr, hi = edges[b], edges[b + 1], edges[b + 2]
        rising = (bin_freqs > lo) & (bin_freqs <= ctr)
        falling = (bin_freqs > ctr) & (bin_freqs < hi)
        bank[b, rising] = (bin_freqs[rising] - lo) / max(ctr - lo, 1e-9)
        bank[b, falling] = (hi - bin_freqs[falling]) / max(hi - ctr, 1e-9)
        total = bank[b].sum()
        if total > 0:
            bank[b] /= total
        else:
            # полоса уже полосы FFT: берём ближайший бин
            bank[b, int(np.argmin(np.abs(bin_freqs - ctr)))] = 1.0
    return bank, edges[1:-1].astype(np.float32)


def _smooth_bands(x: np.ndarray, sigma: float) -> np.ndarray:
    """Гауссово сглаживание вдоль оси полос."""
    if sigma <= 0 or x.shape[1] < 3:
        return x
    radius = max(1, int(round(sigma * 2.5)))
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(x, ((0, 0), (radius, radius)), mode="edge")
    out = np.zeros_like(x)
    for i, w in enumerate(k):
        out += padded[:, i:i + x.shape[1]] * w
    return out


def _envelope(x: np.ndarray, attack: float, decay: float) -> np.ndarray:
    """Асимметричное сглаживание по времени: резко вверх, плавно вниз."""
    attack = float(np.clip(attack, 1e-3, 1.0))
    decay = float(np.clip(decay, 1e-3, 1.0))
    matrix = x.reshape(-1, 1) if x.ndim == 1 else x
    return _envelope_kernel(matrix, attack, decay).reshape(x.shape)


@njit(cache=True)
def _envelope_kernel(x: np.ndarray, attack: float, decay: float) -> np.ndarray:
    """Compile the sequential recurrence without per-frame NumPy allocations."""
    out = np.empty_like(x)
    if x.shape[0] == 0:
        return out
    for band in range(x.shape[1]):
        prev = np.float64(x[0, band])
        for frame in range(x.shape[0]):
            cur = np.float64(x[frame, band])
            rate = attack if cur > prev else decay
            prev = prev + (cur - prev) * rate
            out[frame, band] = prev
    return out


def _normalize(x: np.ndarray, percentile: float = 99.0, floor: float = 0.0) -> np.ndarray:
    """Масштабирует по верхнему перцентилю, чтобы пики доходили до 1."""
    if x.size == 0:
        return x
    top = float(np.percentile(x, percentile))
    if top <= floor + 1e-9:
        return np.zeros_like(x)
    return np.clip((x - floor) / (top - floor), 0.0, 1.0)


# --------------------------------------------------------------------------- #
#  Основной анализ
# --------------------------------------------------------------------------- #
def analyze(
    samples: np.ndarray,
    sample_rate: int,
    fps: int,
    cfg: AnalysisCfg,
    n_frames: int | None = None,
    *,
    show_progress: bool = False,
) -> Analysis:
    """Считает спектр и огибающие для каждого кадра видео."""
    n_fft = int(cfg.n_fft)
    hop = sample_rate / float(fps)
    if n_frames is None:
        n_frames = max(1, int(np.ceil(len(samples) / hop)))

    # Окна выбираются из массива без его копии, дополненной тишиной. Это
    # сохраняет память примерно на уровне самого декодированного аудио даже
    # для часовых треков вместо создания ещё одной часовой копии.
    half = n_fft // 2
    samples = np.asarray(samples, dtype=np.float32)
    windows = (np.lib.stride_tricks.sliding_window_view(samples, n_fft)
               if len(samples) >= n_fft else None)
    window = np.hanning(n_fft).astype(np.float32)

    bank, freqs = _triangular_bank(
        cfg.n_bands, n_fft // 2 + 1, sample_rate, n_fft, cfg.fmin, cfg.fmax
    )
    # подъём высоких частот, иначе визуализатор «живёт» только слева
    tilt_gain = (10.0 * np.log10(np.maximum(freqs, 1.0) / max(cfg.fmin, 1.0))
                 * float(cfg.tilt)).astype(np.float32)

    raw = np.zeros((n_frames, cfg.n_bands), dtype=np.float32)
    rms = np.zeros(n_frames, dtype=np.float32)
    flux = np.zeros(n_frames, dtype=np.float32)
    prev_mag: np.ndarray | None = None
    chunk_starts = range(0, n_frames, _CHUNK)
    for begin in tqdm(
        chunk_starts,
        total=(n_frames + _CHUNK - 1) // _CHUNK,
        desc=tr("progress.audio_analysis"), unit="block", ascii=True,
        mininterval=2.0, dynamic_ncols=True, file=sys.stderr,
        disable=not show_progress or not sys.stderr.isatty(), leave=False,
    ):
        end = min(begin + _CHUNK, n_frames)
        starts = (np.arange(begin, end) * hop).astype(np.int64)
        source_starts = starts - half
        block = np.zeros((end - begin, n_fft), dtype=np.float32)
        if windows is not None:
            valid = ((source_starts >= 0)
                     & (source_starts + n_fft <= len(samples)))
            if valid.any():
                block[valid] = windows[source_starts[valid]]
        # Only the first/last FFT window needs explicit zero padding.
        for row in np.flatnonzero((source_starts < 0)
                                  | (source_starts + n_fft > len(samples))):
            source_start = int(source_starts[row])
            left = max(0, source_start)
            right = min(len(samples), source_start + n_fft)
            if right > left:
                dst_left = left - source_start
                block[row, dst_left:dst_left + right - left] = samples[left:right]
        block *= window

        spectrum = np.abs(np.fft.rfft(block, axis=1)).astype(np.float32)
        spectrum *= 2.0 / n_fft

        raw[begin:end] = spectrum @ bank.T
        rms[begin:end] = np.sqrt(np.mean(block * block, axis=1))

        # спектральный поток: сумма приростов энергии — основа детекции ударов
        if prev_mag is not None:
            first = np.maximum(spectrum[0] - prev_mag, 0.0).sum()
            flux[begin] = first
        diff = np.maximum(spectrum[1:] - spectrum[:-1], 0.0).sum(axis=1)
        flux[begin + 1:end] = diff
        prev_mag = spectrum[-1]

    # в дБ, с частотным наклоном
    db = 20.0 * np.log10(raw + 1e-9) + tilt_gain[None, :]
    # Опорный уровень берём по верхнему перцентилю, но не ниже абсолютного
    # порога: иначе на тишине (интро, пауза) шкала растянется на шум и
    # визуализатор замрёт в случайной позе вместо того, чтобы погаснуть.
    ref = max(float(np.percentile(db, 99.5)), _SILENCE_REF_DB)
    levels = np.clip((db - (ref - cfg.db_range)) / cfg.db_range, 0.0, 1.0)

    levels = _smooth_bands(levels, cfg.band_blur)
    levels = _envelope(levels, cfg.attack, cfg.decay)

    bass = _band_mean(levels, freqs, 0.0, 200.0)
    mid = _band_mean(levels, freqs, 200.0, 2000.0)
    high = _band_mean(levels, freqs, 2000.0, 1e9)

    level_db = 20.0 * np.log10(rms + 1e-9)
    level = np.clip((level_db + 50.0) / 50.0, 0.0, 1.0)
    level = _envelope(level, 0.4, 0.12)

    beat = _envelope(_normalize(flux, 98.0), 0.85, 0.09)

    return Analysis(
        fps=fps, n_frames=n_frames, bands=levels, freqs=freqs,
        bass=_normalize(bass, 99.0), mid=_normalize(mid, 99.0),
        high=_normalize(high, 99.0), level=level, beat=beat,
    )


def _band_mean(levels: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Средний уровень по полосам, попавшим в частотный диапазон."""
    mask = (freqs >= lo) & (freqs < hi)
    if not mask.any():  # диапазон вне банка фильтров: берём ближайшую полосу
        nearest = int(np.argmin(np.abs(freqs - np.clip(freqs.mean(), lo, hi))))
        return levels[:, nearest].copy()
    return levels[:, mask].mean(axis=1)


def slice_bands(analysis: Analysis, spec_range: str) -> tuple[int, int]:
    """Границы полос для визуализатора по имени диапазона."""
    f = analysis.freqs
    if spec_range == "bass":
        mask = f < 250.0
    elif spec_range == "mid":
        mask = (f >= 250.0) & (f < 4000.0)
    elif spec_range == "high":
        mask = f >= 4000.0
    else:
        return 0, len(f)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0, len(f)
    return int(idx[0]), int(idx[-1]) + 1
