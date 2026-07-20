#!/usr/bin/env python3
"""Render the Way SPN vertical anti-jamming advertisement."""

from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "timings.json").read_text(encoding="utf-8"))
WIDTH = int(CONFIG["width"])
HEIGHT = int(CONFIG["height"])
FPS = int(CONFIG["fps"])
DURATION = float(CONFIG["duration_seconds"])

BUILD_ID = str(CONFIG.get("build_id") or "").strip()
ASSETS = ROOT / "assets" / BUILD_ID if BUILD_ID else ROOT / "assets"
AUDIO = ROOT / "audio"
OUTPUT = ROOT / "output"
TEMP = ROOT / ".render" / BUILD_ID if BUILD_ID else ROOT / ".render"
VOICE_FILE = AUDIO / str(CONFIG.get("voice_file") or "voice.mp3")
OUTPUT_FILE = OUTPUT / str(CONFIG.get("output_file") or "way-spn-antijam-story.mp4")
POSTER_FILE = OUTPUT / str(CONFIG.get("poster_file") or "way-spn-antijam-poster.jpg")

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_SYMBOL = "/System/Library/Fonts/SFNS.ttf"

WHITE = (255, 247, 230, 255)
MUTED = (208, 207, 194, 255)
GOLD = (240, 207, 122, 255)
GOLD_DARK = (199, 137, 45, 255)
TEAL = (24, 199, 154, 255)
BLUE = (90, 184, 255, 255)
RED = (255, 107, 122, 255)
INK = (6, 11, 17, 255)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size=size)


def symbol_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_SYMBOL, size=size)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def smooth(value: float) -> float:
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def ease_out(value: float) -> float:
    value = clamp(value)
    return 1.0 - (1.0 - value) ** 3


def scene_alpha(t: float, start: float, end: float) -> float:
    """Use fast editorial cuts so adjacent text scenes never overlap or flash."""
    return 1.0 if start <= t < end else 0.0


def local_progress(t: float, start: float, end: float) -> float:
    return clamp((t - start) / max(0.001, end - start))


def rounded_gradient(size: tuple[int, int], left: tuple[int, int, int], right: tuple[int, int, int], radius: int) -> Image.Image:
    w, h = size
    x = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :, None]
    l = np.asarray(left, dtype=np.float32)[None, None, :]
    r = np.asarray(right, dtype=np.float32)[None, None, :]
    rgb = np.repeat(l * (1.0 - x) + r * x, h, axis=0).astype(np.uint8)
    image = Image.fromarray(rgb, "RGB").convert("RGBA")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    image.putalpha(mask)
    return image


def build_background() -> Image.Image:
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    top = np.array([12.0, 20.0, 30.0])
    bottom = np.array([5.0, 7.0, 10.0])
    mix = (y / HEIGHT)[..., None]
    rgb = top * (1.0 - mix) + bottom * mix

    glows = [
        (130, 220, 430, np.array([36.0, 92.0, 150.0])),
        (940, 470, 520, np.array([12.0, 100.0, 79.0])),
        (520, 1680, 590, np.array([103.0, 67.0, 22.0])),
    ]
    for cx, cy, radius, color in glows:
        distance = ((x - cx) ** 2 + (y - cy) ** 2) / (radius * radius)
        strength = np.clip(1.0 - distance, 0.0, 1.0)[..., None] ** 2
        rgb += strength * color

    rng = np.random.default_rng(42)
    rgb += rng.normal(0.0, 1.7, (HEIGHT, WIDTH, 1))
    image = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(image)
    for gx in range(0, WIDTH, 90):
        draw.line((gx, 0, gx, HEIGHT), fill=(24, 35, 45, 255), width=1)
    for gy in range(0, HEIGHT, 90):
        draw.line((0, gy, WIDTH, gy), fill=(24, 35, 45, 255), width=1)
    return image


BACKGROUND = build_background()


def composite_with_alpha(frame: Image.Image, layer: Image.Image, alpha: float) -> None:
    if alpha <= 0.0:
        return
    if alpha < 0.999:
        adjusted = layer.getchannel("A").point(lambda px: int(px * alpha))
        layer = layer.copy()
        layer.putalpha(adjusted)
    frame.alpha_composite(layer)


def draw_wordmark(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle((72, 92, 166, 186), radius=28, fill=GOLD)
    draw.text((119, 142), "S", font=font(56, True), fill=INK, anchor="mm")
    draw.text((194, 114), "WAY SPN", font=font(38, True), fill=WHITE, anchor="la")
    draw.text((195, 161), "АНТИГЛУШИЛКА", font=font(20, True), fill=GOLD, anchor="la")


def add_top_and_bottom(frame: Image.Image, t: float) -> None:
    draw = ImageDraw.Draw(frame, "RGBA")
    draw_wordmark(draw)
    if t < 9.0:
        draw.text((WIDTH // 2, 1630), CONFIG["bot_handle"], font=font(30, True), fill=(255, 247, 230, 185), anchor="mm")


def draw_signal(draw: ImageDraw.ImageDraw, center: tuple[int, int], color: tuple[int, int, int, int], broken: bool = False) -> None:
    cx, cy = center
    for index, radius in enumerate((205, 145, 86)):
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        draw.arc(box, start=220, end=320, fill=color, width=22)
        if broken and index < 2:
            cut_x = cx + (45 if index == 0 else 15)
            draw.line((cut_x, cy - radius // 2, cut_x + 55, cy - radius // 2 - 8), fill=(11, 17, 24, 255), width=30)
    dot_color = RED if broken else color
    draw.ellipse((cx - 27, cy + 95, cx + 27, cy + 149), fill=dot_color)


def draw_scene_one(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    p = ease_out(local_progress(t, 0.0, 2.5) / 0.45)
    icon_y = int(530 + (1.0 - p) * 70)
    draw_signal(draw, (WIDTH // 2, icon_y), (255, 132, 145, 255), broken=True)

    jitter = int(math.sin(t * 31.0) * 9)
    draw.text((WIDTH // 2 + jitter, 890), "ИНТЕРНЕТ СНОВА", font=font(58, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2 - jitter, 990), "НЕСТАБИЛЕН?", font=font(92, True), fill=GOLD, anchor="mm")
    draw.text((WIDTH // 2, 1110), "Обрывы. Помехи. Ограничения.", font=font(34), fill=MUTED, anchor="mm")

    rng = np.random.default_rng(int(t * FPS) + 7)
    for _ in range(8):
        y = int(rng.integers(340, 1180))
        x = int(rng.integers(40, 850))
        width = int(rng.integers(80, 330))
        draw.rounded_rectangle((x, y, x + width, y + int(rng.integers(4, 11))), radius=4, fill=(90, 184, 255, int(rng.integers(25, 75))))
    return layer


def shield_points(cx: int, cy: int, scale: float) -> list[tuple[int, int]]:
    points = [(-150, -160), (0, -215), (150, -160), (138, 35), (92, 145), (0, 215), (-92, 145), (-138, 35)]
    return [(int(cx + x * scale), int(cy + y * scale)) for x, y in points]


def draw_scene_two(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    p = ease_out(local_progress(t, 2.5, 5.5) / 0.5)
    pulse = 1.0 + 0.035 * math.sin((t - 2.5) * math.tau * 1.5)
    cx, cy = WIDTH // 2, 570

    for radius, opacity in ((300, 24), (245, 38)):
        r = int(radius * (0.92 + 0.08 * p))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(24, 199, 154, opacity), width=4)

    points = shield_points(cx, cy, p * pulse)
    draw.polygon(points, fill=(14, 82, 69, 235), outline=TEAL)
    draw_signal(draw, (cx, cy - 30), TEAL, broken=False)

    draw.text((WIDTH // 2, 910), "РЕЖИМ С", font=font(52, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 1005), "АНТИГЛУШИЛКОЙ", font=font(74, True), fill=GOLD, anchor="mm")
    draw.text((WIDTH // 2, 1150), "ОТДЕЛЬНЫЙ РЕЖИМ", font=font(40, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 1210), "для сетевых ограничений", font=font(40), fill=MUTED, anchor="mm")
    return layer


def feature_card(layer: Image.Image, y: int, accent: tuple[int, int, int, int], title: str, caption: str, progress: float) -> None:
    if progress <= 0.0:
        return
    draw = ImageDraw.Draw(layer, "RGBA")
    x = int(74 + (1.0 - ease_out(progress)) * 120)
    right = WIDTH - 74
    alpha = int(235 * smooth(progress))
    draw.rounded_rectangle((x, y, right, y + 210), radius=38, fill=(20, 24, 30, alpha), outline=accent[:3] + (int(alpha * 0.65),), width=3)
    draw.rounded_rectangle((x + 28, y + 45, x + 44, y + 165), radius=8, fill=accent[:3] + (alpha,))
    draw.text((x + 82, y + 70), title, font=font(52, True), fill=WHITE[:3] + (alpha,), anchor="la")
    draw.text((x + 82, y + 145), caption, font=font(30), fill=MUTED[:3] + (alpha,), anchor="la")


def draw_scene_three(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    draw.text((WIDTH // 2, 370), "ВСЁ НЕОБХОДИМОЕ", font=font(48, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 445), "В ОДНОЙ ПОДПИСКЕ", font=font(59, True), fill=GOLD, anchor="mm")
    p = local_progress(t, 5.5, 9.0)
    feature_card(layer, 560, BLUE, "150 ГБ", "трафика на 30 дней", clamp((p - 0.00) / 0.22))
    feature_card(layer, 825, TEAL, "ДО 3 УСТРОЙСТВ", "в одной подписке", clamp((p - 0.18) / 0.22))
    feature_card(layer, 1090, GOLD, "ПОНЯТНЫЕ ШАГИ", "в инструкции по подключению", clamp((p - 0.36) / 0.22))
    return layer


def draw_scene_four(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    p = ease_out(local_progress(t, 9.0, 13.0) / 0.45)
    price_y = int(565 + (1.0 - p) * 75)
    draw.text((WIDTH // 2, 390), "ТАРИФ С АНТИГЛУШИЛКОЙ", font=font(35, True), fill=GOLD, anchor="mm")
    draw.text((WIDTH // 2, price_y - 95), "ОТ", font=font(30, True), fill=MUTED, anchor="mm")
    draw.text((WIDTH // 2, price_y), "300 ₽", font=symbol_font(138), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, price_y + 105), "ЗА 30 ДНЕЙ", font=font(34, True), fill=TEAL, anchor="mm")

    draw.text((WIDTH // 2, 900), "ПОДКЛЮЧАЙТЕСЬ", font=font(54, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 980), "ПРЯМО СЕЙЧАС", font=font(64, True), fill=GOLD, anchor="mm")

    button = rounded_gradient((880, 142), GOLD[:3], GOLD_DARK[:3], 44)
    button_draw = ImageDraw.Draw(button)
    button_draw.text((440, 71), "ПОДКЛЮЧИТЬ В TELEGRAM", font=font(40, True), fill=INK, anchor="mm")
    layer.alpha_composite(button, (100, 1140))
    draw.text((WIDTH // 2, 1340), CONFIG["bot_handle"], font=font(42, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 1410), "Ссылка — в профиле или описании", font=font(29), fill=MUTED, anchor="mm")
    return layer


def render_frame(t: float) -> Image.Image:
    frame = BACKGROUND.copy()
    scenes = (
        (draw_scene_one, 0.0, 2.5),
        (draw_scene_two, 2.5, 5.5),
        (draw_scene_three, 5.5, 9.0),
        (draw_scene_four, 9.0, 13.0),
    )
    for renderer, start, end in scenes:
        alpha = scene_alpha(t, start, end)
        if alpha > 0.001:
            composite_with_alpha(frame, renderer(t), alpha)
    add_top_and_bottom(frame, t)
    return frame.convert("RGB")


def render_stills() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    still_times = (1.25, 4.0, 7.7, 11.0)
    for index, t in enumerate(still_times, start=1):
        render_frame(t).save(ASSETS / f"scene-{index:02d}.png", optimize=True)
    render_frame(11.0).save(POSTER_FILE, quality=94, subsampling=0)


def generate_music() -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    sample_rate = 44_100
    count = int(DURATION * sample_rate)
    timeline = np.arange(count, dtype=np.float64) / sample_rate
    music = np.zeros(count, dtype=np.float64)

    chords = (
        (0.0, (73.42, 110.00, 146.83)),
        (2.5, (82.41, 123.47, 164.81)),
        (5.5, (98.00, 146.83, 196.00)),
        (9.0, (110.00, 164.81, 220.00)),
    )
    for start, frequencies in chords:
        end = next((point for point, _ in chords if point > start), DURATION)
        mask = (timeline >= start) & (timeline < end)
        local = timeline[mask] - start
        envelope = np.minimum(local / 0.22, 1.0) * np.minimum((end - start - local) / 0.28, 1.0)
        pad = sum(np.sin(math.tau * frequency * local + index * 0.8) for index, frequency in enumerate(frequencies)) / len(frequencies)
        music[mask] += 0.15 * pad * np.clip(envelope, 0.0, 1.0)

    for beat in np.arange(0.0, DURATION, 0.5):
        start = int(beat * sample_rate)
        length = int(0.16 * sample_rate)
        local = np.arange(length) / sample_rate
        pulse = np.sin(math.tau * (64.0 - 24.0 * local) * local) * np.exp(-local * 22.0)
        music[start:min(start + length, count)] += 0.19 * pulse[: max(0, min(length, count - start))]

    for moment in (2.5, 5.5, 9.0):
        start = int((moment - 0.35) * sample_rate)
        length = int(0.7 * sample_rate)
        local = np.arange(length) / sample_rate
        sweep = np.sin(math.tau * (260.0 * local + 420.0 * local * local)) * np.sin(np.pi * local / 0.7) ** 2
        music[start:start + length] += 0.055 * sweep

    music *= np.linspace(0.65, 1.0, count)
    peak = max(1e-6, float(np.max(np.abs(music))))
    stereo = np.column_stack((music / peak * 0.65, music / peak * 0.62))
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(AUDIO / "music.wav"), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def render_video(ffmpeg: str) -> Path:
    TEMP.mkdir(parents=True, exist_ok=True)
    silent_video = TEMP / "video-no-audio.mp4"
    command = [
        ffmpeg,
        "-y",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(silent_video),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    total_frames = int(round(DURATION * FPS))
    assert process.stdin is not None
    for frame_number in range(total_frames):
        t = frame_number / FPS
        process.stdin.write(np.asarray(render_frame(t), dtype=np.uint8).tobytes())
        if frame_number % FPS == 0:
            print(f"Rendered {frame_number // FPS:02d}/{int(DURATION):02d} seconds", flush=True)
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("Video rendering failed")
    return silent_video


def mix_audio(ffmpeg: str, silent_video: Path) -> Path:
    voice = VOICE_FILE
    if not voice.exists():
        raise FileNotFoundError(f"{voice} is missing; generate the voice track first")
    target = OUTPUT_FILE
    filter_graph = (
        f"[1:a]volume=1.18,aformat=channel_layouts=stereo,apad=pad_dur={DURATION}[voice];"
        f"[2:a]volume=0.18[music];"
        f"[voice][music]amix=inputs=2:duration=longest:dropout_transition=0,"
        f"atrim=0:{DURATION},afade=t=out:st=12.45:d=0.55,"
        f"loudnorm=I=-16:LRA=7:TP=-1.5[audio]"
    )
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(silent_video),
        "-i",
        str(voice),
        "-i",
        str(AUDIO / "music.wav"),
        "-filter_complex",
        filter_graph,
        "-map",
        "0:v:0",
        "-map",
        "[audio]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-t",
        str(DURATION),
        "-movflags",
        "+faststart",
        str(target),
    ]
    subprocess.run(command, check=True)
    return target


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    generate_music()
    render_stills()
    silent_video = render_video(ffmpeg)
    target = mix_audio(ffmpeg, silent_video)
    print(f"Created {target}")


if __name__ == "__main__":
    main()
