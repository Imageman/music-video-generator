"""Командный интерфейс генератора музыкальных видео."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from tqdm import tqdm

from .config import Config, ConfigError, load_config
from .i18n import LANGUAGES, set_language, tr
from .presets import PRESETS, describe


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """RawDescription + переведённое слово «usage:» перед строкой вызова."""

    def add_usage(self, usage, actions, groups, prefix=None):
        # Только None означает «префикс не задан». Пустую строку argparse
        # передаёт намеренно, когда собирает prog подкоманды из строки вызова
        # родителя: подставить туда слово «usage» — значит вписать его в prog.
        if prefix is None:
            prefix = tr("cli.help.usage")
        super().add_usage(usage, actions, groups, prefix)


def _localize(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Переводит то, что argparse печатает сам.

    Заголовки групп задаются в конструкторе argparse и снаружи доступны
    только через `_positionals`/`_optionals`; если в будущем они исчезнут,
    справка останется английской, но ничего не сломается.
    """
    for attr, key in (("_positionals", "cli.help.positionals"),
                      ("_optionals", "cli.help.options")):
        group = getattr(p, attr, None)
        if group is not None:
            group.title = tr(key)

    p.add_argument("-h", "--help", action="help", help=tr("cli.help.help"))
    return p


def _subparser(sub, name: str, help_key: str) -> argparse.ArgumentParser:
    return _localize(sub.add_parser(
        name, help=tr(help_key), add_help=False, formatter_class=_Formatter,
    ))


def build_parser() -> argparse.ArgumentParser:
    p = _localize(argparse.ArgumentParser(
        prog="musicvideo",
        description=tr("cli.description"),
        formatter_class=_Formatter,
        epilog=_epilog(),
        add_help=False,
    ))
    sub = p.add_subparsers(dest="command")

    render = _subparser(sub, "render", "cli.cmd.render")
    _add_render_args(render)

    still = _subparser(sub, "still", "cli.cmd.still")
    _add_render_args(still)
    still.add_argument("--at", type=float, default=None, help=tr("cli.arg.at"))

    _add_lang_arg(_subparser(sub, "presets", "cli.cmd.presets"))

    dump = _subparser(sub, "dump-config", "cli.cmd.dump")
    _add_render_args(dump)
    dump.add_argument("--dump-to", default="-", help=tr("cli.arg.dump_to"))
    return p


#: Подкоманды; всё остальное считается аргументами `render`.
COMMANDS = ("render", "still", "presets", "dump-config")


def normalize_argv(argv: list[str]) -> list[str]:
    """Позволяет вызывать `musicvideo track.mp3 ...` без слова `render`."""
    if argv and (argv[0] in COMMANDS or argv[0] in ("-h", "--help")):
        return argv
    return ["render", *argv]


def language_from_argv(argv: list[str]) -> str | None:
    """Достаёт `--lang` до построения парсера.

    Тексты справки берутся из каталога в момент создания парсера, поэтому
    язык должен быть известен раньше, чем argparse доберётся до аргументов.
    """
    for i, arg in enumerate(argv):
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
    return None


def _add_lang_arg(p: argparse.ArgumentParser) -> None:
    """`--lang` есть у каждой подкоманды: язык выбирают до всего остального."""
    p.add_argument("--lang", default=None, choices=list(LANGUAGES),
                   help=tr("cli.arg.lang"))


def _add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("audio", nargs="*", help=tr("cli.arg.audio"))
    p.add_argument("-i", "--image", action="append", default=None,
                   metavar="PATH", help=tr("cli.arg.image"))
    p.add_argument("-o", "--output", default=None, help=tr("cli.arg.output"))
    p.add_argument("-c", "--config", default=None, help=tr("cli.arg.config"))
    p.add_argument("-p", "--preset", default=None, choices=sorted(PRESETS),
                   help=tr("cli.arg.preset"))
    _add_lang_arg(p)

    p.add_argument("--title", default=None, help=tr("cli.arg.title"))
    p.add_argument("--artist", default=None, help=tr("cli.arg.artist"))
    p.add_argument("--cover", default=None, help=tr("cli.arg.cover"))
    p.add_argument("--no-cover", action="store_true", help=tr("cli.arg.no_cover"))
    p.add_argument("--overlay-image", default=None, help=tr("cli.arg.overlay_image"))
    p.add_argument("--overlay-width", type=float, default=None, metavar="0..1",
                   help=tr("cli.arg.overlay_width"))
    p.add_argument("--overlay-position", type=float, nargs=2, default=None,
                   metavar=("X", "Y"), help=tr("cli.arg.overlay_position"))
    p.add_argument("--overlay-opacity", type=float, default=None, metavar="0..1",
                   help=tr("cli.arg.overlay_opacity"))
    p.add_argument("--overlay-intro", type=float, default=None, metavar="SEC",
                   help=tr("cli.arg.overlay_intro"))
    p.add_argument("--overlay-outro", type=float, default=None, metavar="SEC",
                   help=tr("cli.arg.overlay_outro"))
    p.add_argument("--overlay-fade-in", type=float, default=None, metavar="SEC",
                   help=tr("cli.arg.overlay_fade_in"))
    p.add_argument("--overlay-fade-out", type=float, default=None, metavar="SEC",
                   help=tr("cli.arg.overlay_fade_out"))

    p.add_argument("--style", default=None,
                   choices=["circle", "bars", "mirror", "wave", "ring", "dots"],
                   help=tr("cli.arg.style"))
    p.add_argument("--color", default=None, help=tr("cli.arg.color"))
    p.add_argument("--color2", default=None, help=tr("cli.arg.color2"))
    p.add_argument("--no-visualizer", action="store_true",
                   help=tr("cli.arg.no_visualizer"))
    p.add_argument("--no-progress", action="store_true",
                   help=tr("cli.arg.no_progress"))
    p.add_argument("--grain", type=float, default=None, metavar="0..1",
                   help=tr("cli.arg.grain"))

    p.add_argument("--speed", type=float, default=None, metavar="X",
                   help=tr("cli.arg.speed"))
    p.add_argument("--reverb", type=float, default=None, metavar="0..1",
                   help=tr("cli.arg.reverb"))
    p.add_argument("--bass", type=float, default=None, metavar="dB",
                   help=tr("cli.arg.bass"))
    p.add_argument("--start", type=float, default=None, help=tr("cli.arg.start"))
    p.add_argument("--length", type=float, default=None, help=tr("cli.arg.length"))
    p.add_argument("--track-gap", type=float, default=None, metavar="СЕК",
                   help=tr("cli.arg.track_gap"))

    p.add_argument("--size", default=None, metavar="WxH", help=tr("cli.arg.size"))
    p.add_argument("--fps", type=int, default=None, help=tr("cli.arg.fps"))
    p.add_argument("--encoder", default=None,
                   choices=["libx264", "h264_nvenc"], help=tr("cli.arg.encoder"))
    p.add_argument("--crf", type=int, default=None, help=tr("cli.arg.crf"))
    p.add_argument("--cq", type=int, default=None, metavar="1..51",
                   help=tr("cli.arg.cq"))
    p.add_argument("--nvenc-preset", default=None,
                   choices=[f"p{i}" for i in range(1, 8)],
                   help=tr("cli.arg.nvenc_preset"))
    p.add_argument("--audio-bitrate", default=None, metavar="RATE",
                   help=tr("cli.arg.audio_bitrate"))
    p.add_argument("--preview", type=float, default=None, metavar="SEC",
                   help=tr("cli.arg.preview"))
    p.add_argument("--workers", type=int, default=None, help=tr("cli.arg.workers"))
    p.add_argument("--renderer", choices=["cpu", "gpu", "auto"], default=None,
                   help=tr("cli.arg.renderer"))
    p.add_argument("--gpu-effects", default=None, metavar="LIST",
                   help=tr("cli.arg.gpu_effects"))
    p.add_argument("--quiet", action="store_true", help=tr("cli.arg.quiet"))


def _epilog() -> str:
    lines = [tr("cli.epilog.presets")]
    lines += [f"  {name:<10} {describe(name)}" for name in sorted(PRESETS)]
    lines += [
        "",
        tr("cli.epilog.examples"),
        "  musicvideo track.mp3 -i art.jpg -p aesthetic",
        "  musicvideo track.mp3 -i pics/ --style bars --speed 0.85 --reverb 0.4",
        "  musicvideo still track.mp3 -i art.jpg -p lofi --at 30",
        "  musicvideo track.mp3 -i art.jpg -p shorts -o output/short.mp4",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  CLI -> словарь переопределений
# --------------------------------------------------------------------------- #
def _set(target: dict, path: str, value: Any) -> None:
    if value is None:
        return
    node = target
    parts = path.split(".")
    for key in parts[:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value


def overrides_from_args(args: argparse.Namespace) -> dict:
    o: dict[str, Any] = {}
    if args.audio:
        _set(o, "audio_file", args.audio[0] if len(args.audio) == 1 else args.audio)
    _set(o, "output", args.output)
    _set(o, "preview", args.preview)
    _set(o, "workers", args.workers)

    if args.image:
        _set(o, "background.image", args.image if len(args.image) > 1 else args.image[0])
    _set(o, "background.grain", args.grain)

    _set(o, "text.title.text", args.title)
    _set(o, "text.artist.text", args.artist)
    _set(o, "cover.image", args.cover)
    _set(o, "overlay.image", args.overlay_image)
    _set(o, "overlay.image_width", args.overlay_width)
    _set(o, "overlay.image_position", args.overlay_position)
    _set(o, "overlay.image_opacity", args.overlay_opacity)
    _set(o, "overlay.image_intro_duration", args.overlay_intro)
    _set(o, "overlay.image_outro_duration", args.overlay_outro)
    _set(o, "overlay.image_fade_in", args.overlay_fade_in)
    _set(o, "overlay.image_fade_out", args.overlay_fade_out)
    _set(o, "renderer", args.renderer)
    if args.gpu_effects is not None:
        effects = [] if args.gpu_effects.strip().lower() in ("", "none", "off") else [
            item.strip() for item in args.gpu_effects.split(",") if item.strip()
        ]
        _set(o, "gpu.effects", effects)
    if args.no_cover:
        _set(o, "cover.enabled", False)

    _set(o, "visualizer.style", args.style)
    _set(o, "visualizer.color", args.color)
    _set(o, "visualizer.color2", args.color2)
    if args.no_visualizer:
        _set(o, "visualizer.enabled", False)
    if args.no_progress:
        _set(o, "progress.enabled", False)

    _set(o, "audio.speed", args.speed)
    _set(o, "audio.reverb", args.reverb)
    _set(o, "audio.bass_boost", args.bass)
    _set(o, "audio.start", args.start)
    _set(o, "audio.duration", args.length)
    _set(o, "audio.track_gap", args.track_gap)

    _set(o, "video.fps", args.fps)
    _set(o, "video.encoder", args.encoder)
    _set(o, "video.crf", args.crf)
    _set(o, "video.cq", args.cq)
    _set(o, "video.nvenc_preset", args.nvenc_preset)
    _set(o, "video.audio_bitrate", args.audio_bitrate)
    if args.size:
        try:
            w, h = (int(v) for v in args.size.lower().replace("х", "x").split("x"))
        except ValueError:
            raise ConfigError(tr("err.bad_size", value=args.size))
        _set(o, "video.width", w)
        _set(o, "video.height", h)
    return o


def _fill_metadata(cfg: Config) -> None:
    """Подставляет название и исполнителя из тегов файла, если их не задали."""
    if cfg.text.title.text and cfg.text.artist.text:
        return
    from . import ffmpeg
    audio = cfg.audio_file[0] if isinstance(cfg.audio_file, list) else cfg.audio_file
    try:
        info = ffmpeg.probe(audio)
    except Exception:
        return
    if not cfg.text.title.text:
        cfg.text.title.text = info.title or Path(audio).stem
    if not cfg.text.artist.text and info.artist:
        cfg.text.artist.text = info.artist


# --------------------------------------------------------------------------- #
#  Прогресс
# --------------------------------------------------------------------------- #
class Progress:
    """ASCII-индикатор рендера с редким обновлением и ETA."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._bar = None

    def __call__(self, done: int, total: int, elapsed: float) -> None:
        if not self.enabled:
            return
        if self._bar is None:
            self._bar = tqdm(
                total=total, desc=tr("progress.video_frames"), unit="frame",
                ascii=True, mininterval=2.0, dynamic_ncols=True, file=sys.stderr,
            )
        if done > self._bar.n:
            self._bar.update(done - self._bar.n)
        if done >= total:
            self._bar.close()


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- #
#  Точка входа
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Язык выбирается до парсера: иначе справка успеет собраться на другом.
    set_language(language_from_argv(raw))

    parser = build_parser()
    args = parser.parse_args(normalize_argv(raw))
    command = args.command or "render"

    if command == "presets":
        for name in sorted(PRESETS):
            print(f"  {name:<10} {describe(name)}")
        return 0

    try:
        cfg = load_config(args.config, overrides_from_args(args), args.preset)
    except ConfigError as e:
        print(tr("err.prefix.config", message=e), file=sys.stderr)
        return 2

    if not cfg.audio_file:
        parser.error(tr("err.no_audio"))

    if command == "dump-config":
        _fill_metadata(cfg)
        payload = cfg.to_dict()
        import json
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.dump_to == "-":
            print(text)
        else:
            Path(args.dump_to).write_text(text, encoding="utf-8")
            print(tr("cli.config.written", path=args.dump_to))
        return 0

    log = (lambda _msg: None) if args.quiet else (lambda msg: print(msg, flush=True))
    _fill_metadata(cfg)

    from . import ffmpeg
    from .render import render_still, render_video

    try:
        if command == "still":
            at = args.at
            if at is None:
                sources = cfg.audio_file if isinstance(cfg.audio_file, list) else [cfg.audio_file]
                at = ffmpeg.expected_audio_duration(sources, cfg.audio) * 0.25
            image = render_still(cfg, at)
            out = Path(cfg.output)
            if out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                out = out.with_suffix(".png")
            out.parent.mkdir(parents=True, exist_ok=True)
            image.save(out)
            log(tr("cli.done", path=out))
            return 0

        started = time.monotonic()
        path = render_video(cfg, Progress(not args.quiet), log)
        size_mb = path.stat().st_size / (1024 * 1024)
        log(tr("cli.done.video", path=path, size=size_mb,
              elapsed=_fmt(time.monotonic() - started)))
        return 0

    except KeyboardInterrupt:
        print("\n" + tr("cli.interrupted"), file=sys.stderr)
        return 130
    except (ffmpeg.FFmpegError, ConfigError, FileNotFoundError, ValueError) as e:
        print(tr("err.prefix.generic", message=e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
