# Configuration reference

**English** · [Русский](configuration.ru.md)

[← back to README](../README.md) · [CLI](cli.md) · [Python API](api.md)

Every value below can be set in a JSON file passed with `-c`. A working
example is in `config.example.json`; to get one filled in with all the current
values:

```bash
python -m musicvideo dump-config track.mp3 -p aesthetic --dump-to my.json
```

## How settings are merged

Four layers, each overriding the one before it:

```
defaults  →  preset  →  config file (-c)  →  CLI flags
```

Merging is recursive and per-field. A partial section only changes the fields
it names:

```json
{ "visualizer": { "color": "#ff0000" } }
```

keeps `style`, `radius`, `amplitude` and everything else from the preset. This
is the useful behaviour and the one the tests pin down — a partial section is
never a replacement of the whole section.

An unknown field is an error, not a silent no-op. A misspelled `color` reads:

```
Configuration error: visualizer: unknown parameters ['colour'].
  Allowed: amplitude, baseline, cap, center, color, color2, ...
```

The list after `Allowed:` is every field the section accepts. Messages appear
in whichever language the interface is set to — see
[Language](cli.md#language).

If `preset` is set both in the file and on the command line, the command line
wins.

## Units and coordinates

- **Positions** (`position`, `center`, `baseline`) are fractions of the frame:
  `[0.5, 0.44]` is the horizontal center, 44 % of the height down from the top.
- **Sizes** (`size`, `radius`, `amplitude`, `thickness`, font sizes) are pixels
  measured on a frame whose *short side* is 1080, then scaled to the real
  frame. A setting that looks right at 1920×1080 looks the same at 1080×1920
  and at 1280×720.
- **Colors** accept `#rgb`, `#rgba`, `#rrggbb`, `#rrggbbaa` and PIL color
  names. The alpha channel works everywhere: `"#ffffffb0"` is translucent
  white.

## Top level

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `audio_file` | string or list of strings | `""` | One source track or an ordered list. The CLI accepts multiple positional files. |
| `output` | string | `"output/video.mp4"` | Where the result goes. Parent folders are created. |
| `preset` | string \| null | `null` | Preset to build on. |
| `preview` | number \| null | `null` | Render only the first N seconds. |
| `workers` | int | `0` | Render processes; `0` means auto (cores − 1, and at least ~40 frames each). |
| `renderer` | string | `"auto"` | `gpu` uses ModernGL/GLSL, `cpu` uses the Pillow backend, `auto` tries GPU and falls back if OpenGL is unavailable. |
| `gpu.texture_cache_mb` | int | `768` | Cache limit for background photo textures. The active crossfade pair can temporarily exceed it. |
| `gpu.effects` | list of strings | `wave`, `glow`, `color_shift`, `grain`, `vignette`, `audio_light` | Supported effects: `wave`, `glow`, `color_shift`, `grain`, `vignette`, `scanlines`, `ripple`, `kaleidoscope`, `glitch`, `audio_light`, `frequency_neon`. |
| `gpu.wave_strength`, `ripple_strength`, `glow_strength` | float | `0.004`, `0.006`, `0.22` | Wave/ripple distortion and soft glow strength. |
| `gpu.color_shift_strength`, `audio_light_strength`, `contrast` | float | `0.12`, `1.0`, `1.0` | Music-reactive color/light strength and GPU contrast. |
| `gpu.neon_*_gate` | float, 0–1 | `0.20` per band | Lower bound for the adaptive response threshold. A peak can leave a decaying tail. |
| `gpu.neon_*_threshold` | float, 0–1 | `1.0` per band | Maximum coverage of the channel's exclusive heat-map region. |
| `gpu.neon_*_expansion` | float, 0–1 | `1.0` per band | Power curve for how quickly coverage grows above the gate. |
| `gpu.neon_gate_softness`, `neon_threshold_softness` | float | `0.03`, `0.035` | Smoothness above the adaptive audio threshold and at heat-map edges. |
| `gpu.neon_release_half_life` | seconds | `0.10` | Glow-area release: after a peak, coverage halves every 0.1 s. |
| `gpu.neon_adaptation_margin`, `neon_adaptation_range` | float, 0–1 | `0.02`, `0.15` | Shared fallback margin/range when the corresponding channel override is `null`. |
| `gpu.neon_saturation_attack`, `neon_saturation_release` | seconds | `0.75`, `1.50` | Shared background rise/fall time constants for channel overrides set to `null`. |
| `gpu.neon_*_adaptation_margin` | float or null | `0.024`, `0.009`, `0.026` | Margins above background for bass, mid and high bands. |
| `gpu.neon_*_adaptation_range` | float or null | `0.05` each | Excess above threshold required for a full accent response. |
| `gpu.neon_*_saturation_attack` | seconds or null | `0.5`, `1.5`, `1.5` | Background rise time constants for bass, mid and high bands. |
| `gpu.neon_*_saturation_release` | seconds or null | `3.0` each | Background fall time constant. |
| `gpu.neon_steady_strength`, `neon_steady_power` | float | `0.35`, `2.0` | Settled coverage of a sustained maximum and the power curve for steady sound. |
| `gpu.neon_steady_dip_width` | float > 0 | `0.02` | Drop below background that fully suppresses steady coverage during a musical valley. |
| `gpu.neon_loudness_floor_db`, `neon_loudness_full_db` | RMS-analysis dB | `-42.0`, `-20.0` | Overall loudness coefficient's floor and full-response reference. |
| `gpu.neon_loudness_gamma` | float > 0 | `1.5` | Power curve for the overall loudness coefficient. |
| `gpu.neon_heat_blur_radius` | float | `2` | Blur radius in frame pixels for float32 heat maps before normalization and equalization. |
| `gpu.neon_blur_radius`, `neon_strength` | float | `0.1`, `1.35` | Halo radius as a fraction of the frame's shorter side and its brightness. Values above `1` remain supported as legacy pixel radii. The threshold/glow pass runs at quarter resolution. |

`requirements.txt` installs ModernGL and glcontext. The GPU backend keeps one
OpenGL context, renders frames sequentially and streams RGB through FFmpeg
stdin. With explicit `renderer: "gpu"`, a missing OpenGL context is an error;
`auto` logs when it falls back to CPU. Pillow prepares source photos and layer
textures once, NumPy keeps the audio analysis, and GLSL handles per-frame
effects and composition. Frames are read from GPU to RAM before encoding;
zero-copy is not implemented.

Set the effect list in `gpu.effects` or pass `--gpu-effects wave,glow`;
`--gpu-effects none` disables them. These effects are used only by the GPU
renderer.

`frequency_neon` builds three float32 heat maps on the GPU: dark/blue bass,
midtone and warm-color mid, and bright highs. It blurs the raw maps first
(`neon_heat_blur_radius`, 2 px by default), then calculates image-specific
threshold offsets to balance the regions. Each pixel is assigned only to the
band with the strongest adjusted score, so the maps do not overlap and each
covers about one third of the frame. Intensity is then normalized and
histogram-equalized separately inside each assigned region: zero remains for
pixels outside it, while active values span 1–255. Each band independently
tracks an adapting background. The accent threshold is `max(gate, background +
channel_margin)`. The background rises with 0.5 / 1.5 / 1.5 s time constants
for bass / mid / high and falls with a 3 s time constant. Excess above the threshold
controls accents. Strong steady sound also retains a floor:
`steady = 0.35 * relative_level^2 * dip_gate`, where
`relative_level = clip((signal - gate) / (1 - gate), 0, 1)`. On stable sound,
`dip_gate=1`; a drop of 0.02 below background smoothly closes it to zero.
Strong steady sound therefore retains coverage, while valleys between accents
can dim the mask. A sustained maximum retains 35% coverage; a band at 0.8
retains about 20%, provided the overall audio is loud enough.

The processed track's RMS envelope supplies a shared loudness coefficient:
`L = clip((rms_db + 42) / 22, 0, 1)^1.5`. This uses the internal FFT-analysis dB
scale, not LUFS or sound pressure. It scales both steady coverage and accents,
so normalization of a frequency channel cannot make a quiet passage flash at
full area. A strong accent in loud music can open almost the entire region.
The same coefficients are used for every track; there is no song-specific quota
calibration. This is a perceptual-inspired visual heuristic, not a physiological
hearing model.
`threshold` caps coverage and `expansion` controls its power curve. Coverage
attacks immediately and decays by half every 0.10 s after a peak. Release tails
below 0.005 are set to zero. Full coverage means the band's exclusive image
region, and the heat cutoff is `1 - coverage`. Scalar controls are prepared
once in NumPy; image thresholding, blur, and Screen composition stay on GPU.
A separate post-threshold blur
creates the neon halo. The maps use the
background before `audio_light`; when `frequency_neon` is active, `audio_light`
is omitted from the background pass. Bass blends blue with Screen, mid
frequencies blend red-to-green with Screen, and high frequencies blend
yellow-to-near-white with Screen.

## `video`

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `width` | int | `1920` | Frame width. Must be even. |
| `height` | int | `1080` | Frame height. Must be even. |
| `fps` | int | `30` | Frames per second. |
| `encoder` | string | `"libx264"` | `libx264` for CPU or `h264_nvenc` for an NVIDIA GPU. |
| `crf` | int | `21` | x264 quality, 0–51; lower is better and bigger. |
| `cq` | int | `22` | NVENC constant-quality target, 1–51; only used with `h264_nvenc`. |
| `preset` | string | `"medium"` | x264 speed preset: `ultrafast` … `veryslow`. |
| `nvenc_preset` | string | `"p5"` | NVENC preset from `p1` (faster) to `p7` (higher quality). |
| `pixel_format` | string | `"yuv420p"` | Output pixel format. `yuv420p` is what players and platforms expect. |
| `audio_bitrate` | string | `"320k"` | AAC bitrate of the muxed audio. |

For NVENC, FFmpeg receives `-rc vbr -cq 22 -b:v 0`: quality is controlled by
`cq`, while bitrate varies with the scene. NVENC's `cq` scale is not equivalent
to x264's `crf` scale. Choosing `h264_nvenc` requires an FFmpeg build with
NVENC, an NVIDIA driver, and a compatible GPU. There is no automatic CPU
fallback if encoding fails.

```json
{"video": {"encoder": "h264_nvenc", "cq": 22, "nvenc_preset": "p5"}}
```

The even-size rule comes from `yuv420p`: its chroma planes are half resolution,
so an odd width or height cannot be encoded. The config check rejects it
upfront rather than letting ffmpeg fail minutes in.

## `audio`

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `speed` | float | `1.0` | `0.85` = slowed, `1.15` = sped up. Pitch moves with the tempo — this *is* the "slowed" effect. |
| `reverb` | float | `0.0` | Reverb amount, 0–1. |
| `reverb_room` | int | `480` | Reverb tail length, in milliseconds. |
| `bass_boost` | float | `0.0` | Low shelf gain at 110 Hz, in dB. |
| `normalize` | bool | `true` | Loudness normalization (`loudnorm`, I = −14 LUFS). |
| `fade_in` | float | `1.0` | Audio fade-in, seconds. |
| `fade_out` | float | `3.0` | Audio fade-out, seconds. |
| `track_gap` | float | `0.0` | Gap between tracks: positive adds silence; negative sets an equal-power crossfade. |
| `start` | float | `0.0` | Skip this many seconds of the source. |
| `duration` | float \| null | `null` | Use only this many seconds of the source. |

`start` and `duration` are applied to the *source*, before `speed`. With
`speed: 0.85`, a `duration` of 40 becomes ~47 seconds of video.
For a track list, these settings apply to every track. By default each track is
normalized to −14 LUFS, and fade-in/fade-out apply to the start and end of the
whole sequence. Each middle track must be longer than twice the crossfade.

```json
{
  "audio_file": ["first.mp3", "second.mp3", "third.mp3"],
  "audio": {"track_gap": -1.5}
}
```

The filter chain ffmpeg ends up running is, in order: speed (`asetrate` +
`aresample`) → bass shelf → reverb (two cascaded `aecho` stages) → `loudnorm` →
fades → a final limiter at −0.26 dBFS. Reverb is built from echoes rather than
an impulse response so that no external IR file is needed.

## `analysis`

How the spectrum is turned into the numbers the visualizer draws. The defaults
are tuned for music; touch these only if the bands feel too twitchy or too
sluggish.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `n_bands` | int | `64` | Number of frequency bands. |
| `n_fft` | int | `4096` | FFT window size. Larger = better frequency resolution, blurrier in time. |
| `fmin` | float | `30.0` | Lowest frequency shown, Hz. |
| `fmax` | float | `14000.0` | Highest frequency shown, Hz. |
| `db_range` | float | `62.0` | How many dB map onto the full 0–1 range. |
| `attack` | float | `0.55` | How fast a band rises, 0–1. Higher = snappier. |
| `decay` | float | `0.16` | How fast a band falls, 0–1. Lower = longer trails. |
| `band_blur` | float | `0.8` | Smoothing across neighbouring bands. |
| `tilt` | float | `0.55` | High-frequency lift, 0–1, so the right half of the spectrum stays alive. |

Bands are spaced geometrically between `fmin` and `fmax` (that is, evenly by
octave, which is how hearing works), and the envelope is asymmetric: fast up,
slow down. During silence the scale is clamped to an absolute floor so the
visualizer goes dark instead of stretching itself over the noise floor.

## `background`

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `image` | string \| list \| null | `null` | An image, a folder, or a list of paths. |
| `fallback` | string | `"gradient"` | What to draw when there is no image: `gradient` (diagonal blend), `noise` (the same plus soft blotches) or `solid` (the first color, flat). |
| `colors` | list | `["#1b1033", "#07070c"]` | Colors for the procedural fallback. |
| `blur` | float | `18.0` | Blur radius in px. |
| `brightness` | float | `0.55` | Darkening, 0–1. |
| `saturation` | float | `1.05` | Saturation multiplier. |
| `zoom` | float | `1.12` | Ken Burns zoom: total magnification across the clip. |
| `pan` | list | `[0.0, 0.0]` | Pan as `[dx, dy]` in frame fractions across the clip. |
| `beat_zoom` | float | `0.018` | Extra scale on the bass, as a fraction. |
| `shake` | float | `3.0` | Camera shake on the bass, in px. |
| `crossfade` | float | `1.5` | Crossfade length between images, seconds. |
| `grain` | float | `0.035` | Film grain, 0–1. The single biggest factor in file size. |
| `grain_size` | int | `2` | Grain block size in px. `1` is per-pixel noise — incompressible and huge. |

With several images, the clip is divided evenly between them and each
transition is a `crossfade`-long dissolve.

### A note on `grain`

Grain is random, so the codec cannot predict it between frames and has to spend
bits on it every time. Measured on the demo clip at 1080p/30, CRF 21:

| Setting | Bitrate | Three-minute track |
|---------|--------:|-------------------:|
| `grain: 0` | 1.9 Mbit/s | ~43 MB |
| `grain_size: 2` (default) | 24 Mbit/s | ~540 MB |
| `grain_size: 1` | 63 Mbit/s | ~1.4 GB |

Generating the noise at half resolution and scaling it up both looks more like
film and costs roughly a third as much.

## `cover`

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | bool | `true` | Draw the cover at all. |
| `image` | string \| null | `null` | Cover file. `null` → embedded artwork → first background image. |
| `size` | int | `440` | Diameter or side, in px at a 1080 short side. |
| `shape` | string | `"circle"` | `circle`, `rounded` or `square`. |
| `radius` | int | `36` | Corner radius for `rounded`. |
| `position` | list | `[0.5, 0.44]` | Center, in frame fractions. |
| `border` | float | `3.0` | Border width in px. |
| `border_color` | string | `"#ffffffcc"` | Border color. |
| `shadow` | float | `42.0` | Drop shadow blur radius. |
| `spin` | float | `0.0` | Vinyl-style rotation, in revolutions per minute. |
| `beat_scale` | float | `0.03` | How much the cover pulses with the bass. |

## `visualizer`

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | bool | `true` | Draw the visualizer at all. |
| `style` | string | `"circle"` | `circle`, `bars`, `mirror`, `wave`, `ring`, `dots`. |
| `color` | string | `"#ffffff"` | Main color. |
| `color2` | string \| null | `"#8b5cf6"` | Second color; bands are graded between the two. `null` = flat color. |
| `opacity` | float | `0.92` | Overall opacity. |
| `glow` | float | `16.0` | Glow radius in px. `0` = off (and noticeably faster). |
| `glow_strength` | float | `0.85` | Glow intensity. |
| `amplitude` | float | `190.0` | Band height at full level, in px. |
| `thickness` | float | `7.0` | Stroke or bar width. |
| `gap` | float | `5.0` | Gap between bars. |
| `cap` | string | `"round"` | Bar end shape: `round` or `flat`. |
| `range` | string | `"full"` | Which frequencies to show: `full`, `bass`, `mid`, `high`. |

Radial styles (`circle`, `ring`, `dots`):

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `radius` | float | `250.0` | Ring radius in px. |
| `center` | list | `[0.5, 0.44]` | Ring center, in frame fractions. |
| `start_angle` | float | `-90.0` | Where band zero sits; `-90` is the top. |
| `mirror` | bool | `true` | Mirror the spectrum across the circle. |
| `inward` | bool | `false` | Draw the rays inward instead of outward. |

Linear styles (`bars`, `mirror`, `wave`):

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `baseline` | float | `0.88` | Baseline height, as a fraction of the frame. |

### Styles at a glance

| Style | Looks like |
|-------|------------|
| `circle` | Rays radiating from a ring — the classic "audio edit" look. |
| `bars` | Vertical bars standing on the baseline. |
| `mirror` | Bars growing up *and* down from the baseline. |
| `wave` | A single smooth line rippling along the baseline. |
| `ring` | A thin closed ring that deforms with the spectrum. |
| `dots` | Dots on a ring, moving out with the level. |

### Keeping the visualizer clear of the cover

A radial visualizer drawn at too small a radius will cut through the cover art.
The rule:

- round cover: `radius > cover.size / 2`
- square cover: `radius > cover.size * 0.71`

And on the outside, `radius + amplitude` has to fit within half the frame's
short side — 540 px on both 1920×1080 and 1080×1920.

## `text`

Holds `title`, `artist`, and `extra` — a list of any number of extra captions,
each with the same fields.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `text` | string | `""` | The caption. Empty = not drawn. |
| `font` | string \| null | `null` | Font file name, without the extension. |
| `size` | int | `54` | Font size in px. |
| `color` | string | `"#ffffff"` | Text color. |
| `opacity` | float | `1.0` | Opacity. |
| `position` | list | `[0.5, 0.72]` | Anchor point, in frame fractions. Vertically it is the middle of the text. |
| `align` | string | `"center"` | `left`, `center` or `right` — which side of the text the anchor pins. |
| `letter_spacing` | float | `0.0` | Extra space between letters, in px. |
| `uppercase` | bool | `false` | Force upper case. |
| `shadow` | float | `18.0` | Shadow blur radius. |
| `shadow_color` | string | `"#000000b0"` | Shadow color. |
| `fade_in` | float | `0.8` | Fade-in length, seconds. |
| `delay` | float | `0.3` | Delay before the fade starts, seconds. |

`title` and `artist` have their own defaults (bigger title at `[0.5, 0.70]`; a
smaller, translucent, uppercase artist at `[0.5, 0.762]`). The progress bar
fades in together with the title.

An extra caption:

```json
{ "text": { "extra": [
  { "text": "slowed + reverb", "size": 26, "position": [0.5, 0.93],
    "color": "#ffffff80", "uppercase": true, "letter_spacing": 4.0 }
] } }
```

### Fonts

Fonts are looked up in `assets/fonts` first, then in the system font
directories. Name the file, not the family: `"Montserrat-SemiBold"` finds
`assets/fonts/Montserrat-SemiBold.ttf`. With no `font` set, the first available
of Montserrat, Poppins, Inter, Roboto, Open Sans and the platform defaults is
used.

## `progress`

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | bool | `true` | Draw the progress bar. |
| `position` | list | `[0.5, 0.855]` | Bar center, in frame fractions. |
| `width` | float | `0.42` | Bar length, as a fraction of the frame width. |
| `thickness` | float | `4.0` | Bar thickness in px. |
| `color` | string | `"#ffffff"` | Filled part. |
| `track_color` | string | `"#ffffff33"` | Unfilled part. |
| `knob` | float | `7.0` | Radius of the moving knob. `0` = no knob. |
| `timecodes` | bool | `true` | Show elapsed and total time at the ends. |
| `timecode_size` | int | `22` | Timecode font size. |
| `timecode_color` | string | `"#ffffff99"` | Timecode color. |

## `overlay`

Full-frame effects for the composed frame, plus an optional transparent image
layer composited above them.

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `image` | string or null | `null` | Image composited over the full frame, such as a logo PNG; alpha is preserved. |
| `image_position` | list of 2 numbers | `[0.82, 0.04]` | Image top-left position as fractions of frame width and height, from 0 to 1. |
| `image_width` | float | `0.14` | Layer width as a fraction of frame width. Aspect ratio is preserved; the image can be scaled up or down. |
| `image_opacity` | float | `1.0` | Layer opacity, from 0 to 1. |
| `image_intro_duration` | float | `0.0` | If positive, show the PNG only during the opening section. |
| `image_outro_duration` | float | `0.0` | If positive, show the PNG only during the ending section. |
| `image_fade_in` | float | `1.0` | PNG fade-in duration for intro/outro sections. |
| `image_fade_out` | float | `1.0` | PNG fade-out duration for intro/outro sections. |
| `vignette` | float | `0.55` | Vignette strength, 0–1. |
| `vignette_size` | float | `0.78` | How far from the center the vignette starts. |
| `scanlines` | float | `0.0` | Horizontal scanline strength, 0–1. The VHS look. |
| `letterbox` | float | `0.0` | Black bar height as a fraction of the frame; `0.12` is cinematic. |
| `tint` | string \| null | `null` | Color grade tint, e.g. `"#ff5a3c"`. |
| `tint_strength` | float | `0.0` | Tint strength, 0–1. Needs `tint` to be set. |
| `beat_flash` | float | `0.0` | Frame brightening on a hit. |
| `fade_in` | float | `1.2` | Fade in from black, seconds. |
| `fade_out` | float | `2.5` | Fade out to black, seconds. |

The image is composited above all effects and fades with the frame. Its width
is relative to the output video size: `0.14` means 14% of the frame width.
Upscaling and downscaling preserve aspect ratio, use the LANCZOS filter and fit
inside the frame. Position coordinates are measured from the top-left corner.

When both section durations are zero, the PNG remains visible throughout the
video, preserving older configurations. For a title overlay during the first
10 seconds and last 5 seconds, with one-second fades:

```json
{
  "renderer": "gpu",
  "video": {"width": 3840, "height": 2160, "fps": 30,
            "encoder": "h264_nvenc", "cq": 22},
  "cover": {"enabled": false},
  "overlay": {"image": "input/covers/saxophone_music_4k_overlay.png",
              "image_position": [0, 0], "image_width": 1.0,
              "image_intro_duration": 10, "image_outro_duration": 5,
              "image_fade_in": 1, "image_fade_out": 1}
}
```

```json
{
  "overlay": {
    "image": "logo.png",
    "image_position": [0.82, 0.04],
    "image_width": 0.14,
    "image_opacity": 0.85
  }
}
```

The full-frame effects run in this order: tint → vignette → beat flash →
scanlines → grain → fade → letterbox. The PNG layer is composited after them.

## A complete example

```json
{
  "audio_file": "track.mp3",
  "output": "output/video.mp4",
  "preset": "aesthetic",

  "video":   { "width": 1920, "height": 1080, "fps": 30, "crf": 21 },
  "audio":   { "speed": 0.88, "reverb": 0.35, "bass_boost": 2.0 },

  "background": {
    "image": "art.jpg",
    "blur": 22.0, "brightness": 0.5, "zoom": 1.14, "grain": 0.04
  },

  "cover": { "shape": "circle", "size": 440, "position": [0.5, 0.44] },

  "visualizer": {
    "style": "circle", "radius": 262.0, "amplitude": 150.0,
    "color": "#ffffff", "color2": "#a78bfa", "glow": 18.0
  },

  "text": {
    "title":  { "text": "Way Down We Go", "size": 64 },
    "artist": { "text": "Kaleo" }
  },

  "overlay": {
    "vignette": 0.6, "beat_flash": 0.05,
    "image": "logo.png", "image_position": [0.82, 0.04], "image_width": 0.14
  }
}
```

```bash
python -m musicvideo track.mp3 -c that-file.json
```
