#!/usr/bin/env python3
"""Render a calm, Russian-first Way SPN vertical video for a Chechen audience."""

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
BUILD_ID = str(CONFIG["build_id"])

ASSETS = ROOT / "assets" / BUILD_ID
AUDIO = ROOT / "audio"
OUTPUT = ROOT / "output"
TEMP = ROOT / ".render" / BUILD_ID
VOICE_FILE = AUDIO / CONFIG["voice_file"]
OUTPUT_FILE = OUTPUT / CONFIG["output_file"]
POSTER_FILE = OUTPUT / CONFIG["poster_file"]

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_SYMBOL = "/System/Library/Fonts/SFNS.ttf"

IVORY = (248, 244, 233, 255)
MUTED = (185, 196, 199, 255)
INK = (11, 18, 22, 255)
PANEL = (17, 29, 35, 244)
PANEL_SOFT = (21, 37, 43, 222)
SAGE = (119, 197, 168, 255)
SAGE_DARK = (46, 119, 101, 255)
SKY = (116, 183, 214, 255)
AMBER = (234, 184, 103, 255)
CORAL = (225, 139, 105, 255)

SAFE_LEFT = 108
SAFE_RIGHT = 830


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size=size)


def symbol_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_SYMBOL, size=size)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def ease_out(value: float) -> float:
    value = clamp(value)
    return 1.0 - (1.0 - value) ** 3


def ease_in_out(value: float) -> float:
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def local_progress(t: float, start: float, end: float) -> float:
    return clamp((t - start) / max(end - start, 0.001))


def with_alpha(color, alpha: int):
    return color[:3] + (max(0, min(255, alpha)),)


def apply_opacity(layer: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 0.999:
        return layer
    result = layer.copy()
    alpha = result.getchannel("A").point(lambda value: int(value * clamp(opacity)))
    result.putalpha(alpha)
    return result


def scene_visibility(t: float, start: float, end: float, fade: float = 0.34) -> float:
    if t < start or t >= end:
        return 0.0
    fade_in = ease_in_out((t - start) / fade) if start > 0 else 1.0
    fade_out = ease_in_out((end - t) / fade) if end < DURATION else 1.0
    return min(fade_in, fade_out)


def build_background() -> Image.Image:
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    top = np.array([19.0, 37.0, 45.0])
    bottom = np.array([7.0, 10.0, 15.0])
    mix = (y / HEIGHT)[..., None]
    rgb = top * (1.0 - mix) + bottom * mix
    glows = (
        (70, 250, 580, np.array([30.0, 82.0, 83.0])),
        (930, 740, 610, np.array([24.0, 55.0, 82.0])),
        (250, 1540, 720, np.array([65.0, 38.0, 20.0])),
    )
    for cx, cy, radius, color in glows:
        distance = ((x - cx) ** 2 + (y - cy) ** 2) / (radius * radius)
        strength = np.clip(1.0 - distance, 0.0, 1.0)[..., None] ** 2
        rgb += strength * color
    rng = np.random.default_rng(87)
    rgb += rng.normal(0.0, 1.35, (HEIGHT, WIDTH, 1))
    image = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    glow_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow_layer, "RGBA")
    for cx, cy, radius, color in (
        (886, 285, 155, (129, 191, 198, 17)),
        (961, 1120, 245, (111, 173, 195, 14)),
        (95, 1685, 280, (236, 179, 96, 13)),
    ):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
    for index, (cx, cy) in enumerate(((65, 1590), (220, 1680), (470, 1618), (710, 1735), (940, 1645))):
        radius = 5 + (index % 3) * 2
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(241, 194, 112, 60))
    image.alpha_composite(glow_layer)
    return image.convert("RGB").convert("RGBA")


BACKGROUND = build_background()


def base_frame(t: float) -> Image.Image:
    frame = BACKGROUND.copy()
    light_layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(light_layer, "RGBA")
    for index in range(7):
        phase = index * 0.83
        cx = 880 + int(55 * math.sin(t * 0.18 + phase))
        cy = 270 + index * 225 + int(28 * math.cos(t * 0.15 + phase))
        radius = 7 + index % 3
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(178, 219, 214, 26))
    frame.alpha_composite(light_layer)
    return frame


def draw_context_label(draw: ImageDraw.ImageDraw, text: str) -> None:
    draw.rounded_rectangle((SAFE_LEFT, 226, SAFE_LEFT + 176, 274), radius=24, fill=(240, 242, 230, 24))
    draw.text((SAFE_LEFT + 88, 250), text, font=font(22, True), fill=MUTED, anchor="mm")


def draw_title(draw: ImageDraw.ImageDraw, text: str, y: int = 355) -> None:
    draw.text((SAFE_LEFT, y), text, font=font(54, True), fill=IVORY, anchor="la")


def draw_scene_home(t: float) -> Image.Image:
    start, end = 0.0, 3.2
    p = ease_out(local_progress(t, start, start + 0.8))
    shift = int((1.0 - p) * 28)
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    draw_context_label(draw, "21:47")
    draw_title(draw, "Написать домой.")

    top = 520 + shift
    draw.rounded_rectangle((SAFE_LEFT, top, SAFE_RIGHT, top + 610), radius=62, fill=PANEL, outline=(245, 247, 237, 20), width=2)
    draw.ellipse((SAFE_LEFT + 48, top + 46, SAFE_LEFT + 132, top + 130), fill=(66, 100, 104, 255))
    draw.text((SAFE_LEFT + 90, top + 88), "Д", font=font(34, True), fill=IVORY, anchor="mm")
    draw.text((SAFE_LEFT + 160, top + 74), "Дом", font=font(31, True), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT + 160, top + 116), "в сети", font=font(23), fill=SAGE, anchor="la")

    bubble = (SAFE_LEFT + 120, top + 240, SAFE_RIGHT - 52, top + 420)
    draw.rounded_rectangle(bubble, radius=42, fill=(64, 116, 101, 245))
    draw.text((bubble[0] + 42, bubble[1] + 70), "Доехал.", font=font(36, True), fill=IVORY, anchor="la")
    draw.text((bubble[0] + 42, bubble[1] + 122), "Всё хорошо.", font=font(36), fill=IVORY, anchor="la")
    draw.text((bubble[2] - 30, bubble[3] - 27), "21:47", font=font(20), fill=(219, 236, 228, 190), anchor="rm")
    draw.text((SAFE_LEFT + 48, top + 533), "маленькое сообщение, которое ждут", font=font(27), fill=MUTED, anchor="la")
    return apply_opacity(layer, scene_visibility(t, start, end))


def draw_scene_work(t: float) -> Image.Image:
    start, end = 3.2, 6.5
    p = ease_out(local_progress(t, start, start + 0.75))
    shift = int((1.0 - p) * 28)
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    draw_context_label(draw, "ПО ДЕЛАМ")
    draw_title(draw, "Ответить по работе.")

    top = 535 + shift
    draw.rounded_rectangle((SAFE_LEFT, top, SAFE_RIGHT, top + 560), radius=58, fill=PANEL, outline=(245, 247, 237, 20), width=2)
    draw.rounded_rectangle((SAFE_LEFT + 48, top + 48, SAFE_RIGHT - 48, top + 142), radius=29, fill=(29, 47, 54, 255))
    draw.ellipse((SAFE_LEFT + 78, top + 78, SAFE_LEFT + 110, top + 110), fill=SKY)
    draw.text((SAFE_LEFT + 136, top + 96), "Работа", font=font(29, True), fill=IVORY, anchor="lm")
    draw.text((SAFE_RIGHT - 76, top + 96), "сейчас", font=font(21), fill=MUTED, anchor="rm")

    draw.rounded_rectangle((SAFE_LEFT + 48, top + 210, SAFE_RIGHT - 48, top + 410), radius=38, fill=(32, 51, 58, 245))
    draw.rounded_rectangle((SAFE_LEFT + 84, top + 252, SAFE_LEFT + 164, top + 332), radius=20, fill=(106, 158, 184, 70))
    draw.line((SAFE_LEFT + 108, top + 278, SAFE_LEFT + 140, top + 278), fill=SKY, width=5)
    draw.line((SAFE_LEFT + 108, top + 294, SAFE_LEFT + 144, top + 294), fill=SKY, width=5)
    draw.line((SAFE_LEFT + 108, top + 310, SAFE_LEFT + 132, top + 310), fill=SKY, width=5)
    draw.text((SAFE_LEFT + 196, top + 270), "Файл отправлен", font=font(34, True), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT + 196, top + 326), "можно продолжать", font=font(26), fill=MUTED, anchor="la")
    draw.ellipse((SAFE_RIGHT - 112, top + 274, SAFE_RIGHT - 62, top + 324), fill=SAGE)
    draw.line((SAFE_RIGHT - 100, top + 299, SAFE_RIGHT - 90, top + 309, SAFE_RIGHT - 72, top + 288), fill=INK, width=6, joint="curve")
    draw.text((SAFE_LEFT + 48, top + 488), "без лишней суеты", font=font(27), fill=MUTED, anchor="la")
    return apply_opacity(layer, scene_visibility(t, start, end))


def draw_scene_road(t: float) -> Image.Image:
    start, end = 6.5, 10.0
    p = ease_out(local_progress(t, start, start + 0.8))
    shift = int((1.0 - p) * 28)
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    draw_context_label(draw, "В ДОРОГЕ")
    draw_title(draw, "Остаться на связи.")

    top = 525 + shift
    draw.rounded_rectangle((SAFE_LEFT, top, SAFE_RIGHT, top + 625), radius=58, fill=PANEL, outline=(245, 247, 237, 20), width=2)
    points = [(SAFE_LEFT + 92, top + 420), (SAFE_LEFT + 205, top + 295), (SAFE_LEFT + 342, top + 350), (SAFE_LEFT + 476, top + 210), (SAFE_LEFT + 620, top + 262)]
    for index in range(len(points) - 1):
        draw.line((*points[index], *points[index + 1]), fill=(75, 103, 111, 255), width=18, joint="curve")
    travelled = max(1, int((len(points) - 1) * clamp((t - start) / 1.8)))
    for index in range(travelled):
        draw.line((*points[index], *points[index + 1]), fill=SAGE, width=10, joint="curve")
    for index, (cx, cy) in enumerate(points):
        radius = 22 if index in (0, len(points) - 1) else 10
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=IVORY if index == 0 else SAGE)
    draw.rounded_rectangle((SAFE_LEFT + 72, top + 64, SAFE_LEFT + 338, top + 138), radius=31, fill=(49, 90, 79, 220))
    draw.ellipse((SAFE_LEFT + 98, top + 88, SAFE_LEFT + 124, top + 114), fill=SAGE)
    draw.text((SAFE_LEFT + 145, top + 101), "на связи", font=font(27, True), fill=IVORY, anchor="lm")
    draw.text((SAFE_LEFT + 52, top + 545), "даже когда день проходит не дома", font=font(27), fill=MUTED, anchor="la")
    return apply_opacity(layer, scene_visibility(t, start, end))


def draw_tariff_row(draw: ImageDraw.ImageDraw, top: int, plan: dict, accent, progress: float) -> None:
    shift = -int((1.0 - ease_out(progress)) * 34)
    left, right = SAFE_LEFT + shift, SAFE_RIGHT + shift
    draw.rounded_rectangle((left, top, right, top + 236), radius=44, fill=PANEL_SOFT, outline=with_alpha(accent, 72), width=3)
    draw.rounded_rectangle((left + 28, top + 34, left + 37, top + 202), radius=5, fill=accent)
    draw.text((left + 70, top + 54), plan["title"], font=font(31, True), fill=accent, anchor="la")
    draw.text((left + 70, top + 113), plan["price"], font=symbol_font(37), fill=IVORY, anchor="la")
    draw.text((left + 70, top + 177), plan["details"], font=font(28), fill=MUTED, anchor="la")


def draw_scene_plans(t: float) -> Image.Image:
    start, end = 10.0, DURATION
    p = ease_out(local_progress(t, start, start + 0.8))
    shift = int((1.0 - p) * 24)
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")

    draw.text((SAFE_LEFT, 232 + shift), "WAY SPN", font=font(25, True), fill=SAGE, anchor="la")
    draw.text((SAFE_LEFT, 318 + shift), "Два режима —", font=font(52, True), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT, 382 + shift), "по ситуации.", font=font(52, True), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT, 456 + shift), "Спокойно посмотрите, что подходит вам", font=font(27), fill=MUTED, anchor="la")

    regular_progress = local_progress(t, 10.45, 11.15)
    bypass_progress = local_progress(t, 10.85, 11.55)
    draw_tariff_row(draw, 560, CONFIG["regular"], SKY, regular_progress)
    draw_tariff_row(draw, 830, CONFIG["bypass"], SAGE, bypass_progress)
    draw.text((SAFE_LEFT + 2, 1108), "* по условиям тарифа", font=font(22), fill=(185, 196, 199, 165), anchor="la")

    cta = ease_in_out(local_progress(t, 15.25, 16.15))
    cta_shift = int((1.0 - cta) * 22)
    draw.text((SAFE_LEFT, 1260 + cta_shift), "Если пригодится —", font=font(29), fill=with_alpha(MUTED, int(255 * cta)), anchor="la")
    draw.text((SAFE_LEFT, 1330 + cta_shift), CONFIG["bot_handle"], font=font(44, True), fill=with_alpha(IVORY, int(255 * cta)), anchor="la")
    draw.line((SAFE_LEFT, 1395, SAFE_RIGHT, 1395), fill=(243, 245, 235, int(32 * cta)), width=2)
    draw.text((SAFE_LEFT, 1442), "подробности в Telegram", font=font(24), fill=(185, 196, 199, int(205 * cta)), anchor="la")
    return apply_opacity(layer, scene_visibility(t, start, end))


def render_frame(t: float) -> Image.Image:
    frame = base_frame(t)
    for scene in (draw_scene_home, draw_scene_work, draw_scene_road, draw_scene_plans):
        frame.alpha_composite(scene(t))
    return frame.convert("RGB")


def render_cover() -> Image.Image:
    cover = base_frame(0.0)
    draw = ImageDraw.Draw(cover, "RGBA")
    draw.text((SAFE_LEFT, 252), "WAY SPN", font=font(25, True), fill=SAGE, anchor="la")
    draw.text((SAFE_LEFT, 350), "Быть на связи —", font=font(55, True), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT, 421), "простая вещь.", font=font(55, True), fill=IVORY, anchor="la")
    draw.rounded_rectangle((SAFE_LEFT, 560, SAFE_RIGHT, 1125), radius=58, fill=PANEL, outline=(245, 247, 237, 20), width=2)
    draw.ellipse((SAFE_LEFT + 48, 608, SAFE_LEFT + 132, 692), fill=(66, 100, 104, 255))
    draw.text((SAFE_LEFT + 90, 650), "Д", font=font(34, True), fill=IVORY, anchor="mm")
    draw.text((SAFE_LEFT + 160, 636), "Дом", font=font(31, True), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT + 160, 680), "в сети", font=font(23), fill=SAGE, anchor="la")
    bubble = (SAFE_LEFT + 120, 792, SAFE_RIGHT - 52, 976)
    draw.rounded_rectangle(bubble, radius=42, fill=(64, 116, 101, 245))
    draw.text((bubble[0] + 42, bubble[1] + 72), "Доехал.", font=font(36, True), fill=IVORY, anchor="la")
    draw.text((bubble[0] + 42, bubble[1] + 126), "Всё хорошо.", font=font(36), fill=IVORY, anchor="la")
    draw.text((SAFE_LEFT, 1252), "два режима под разные ситуации", font=font(29), fill=MUTED, anchor="la")
    return cover.convert("RGB")


def render_stills() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for index, t in enumerate((1.6, 4.8, 8.1, 16.7), start=1):
        render_frame(t).save(ASSETS / f"scene-{index:02d}.png", optimize=True)
    render_cover().save(POSTER_FILE, quality=94, subsampling=0)


def generate_music() -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    sample_rate = 44_100
    count = int(DURATION * sample_rate)
    timeline = np.arange(count, dtype=np.float64) / sample_rate
    music = np.zeros(count, dtype=np.float64)
    chords = (
        (0.0, 3.2, (73.42, 110.00, 146.83)),
        (3.2, 6.5, (82.41, 123.47, 164.81)),
        (6.5, 10.0, (65.41, 98.00, 146.83)),
        (10.0, DURATION, (73.42, 110.00, 164.81)),
    )
    for start, end, frequencies in chords:
        mask = (timeline >= start) & (timeline < end)
        local = timeline[mask] - start
        envelope = np.minimum(local / 0.65, 1.0) * np.minimum((end - start - local) / 0.9, 1.0)
        pad = sum(
            np.sin(math.tau * frequency * local + index * 0.9)
            for index, frequency in enumerate(frequencies)
        ) / len(frequencies)
        music[mask] += 0.11 * pad * np.clip(envelope, 0.0, 1.0)
    for moment, note in ((0.7, 293.66), (4.2, 329.63), (7.6, 246.94), (11.1, 293.66), (15.6, 329.63)):
        start = int(moment * sample_rate)
        length = min(int(1.15 * sample_rate), count - start)
        local = np.arange(length) / sample_rate
        tone = np.sin(math.tau * note * local) * np.exp(-local * 3.5)
        music[start:start + length] += 0.035 * tone
    peak = max(1e-6, float(np.max(np.abs(music))))
    stereo = np.column_stack((music / peak * 0.52, music / peak * 0.49))
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
        ffmpeg, "-y", "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264",
        "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(silent_video),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    for frame_number in range(int(round(DURATION * FPS))):
        process.stdin.write(np.asarray(render_frame(frame_number / FPS), dtype=np.uint8).tobytes())
        if frame_number % FPS == 0:
            print(f"Rendered {frame_number // FPS:02d}/{int(DURATION):02d} seconds", flush=True)
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("Video rendering failed")
    return silent_video


def mix_audio(ffmpeg: str, silent_video: Path) -> Path:
    if not VOICE_FILE.exists():
        raise FileNotFoundError(f"{VOICE_FILE} is missing; generate the voice track first")
    filter_graph = (
        f"[1:a]volume=1.10,adelay=280|280,aformat=channel_layouts=stereo,apad=pad_dur={DURATION}[voice];"
        "[2:a]volume=0.105[music];"
        f"[voice][music]amix=inputs=2:duration=longest:dropout_transition=0,atrim=0:{DURATION},"
        "afade=t=out:st=18.4:d=0.8,loudnorm=I=-16:LRA=7:TP=-1.5[audio]"
    )
    command = [
        ffmpeg, "-y", "-i", str(silent_video), "-i", str(VOICE_FILE), "-i", str(AUDIO / "music.wav"),
        "-filter_complex", filter_graph, "-map", "0:v:0", "-map", "[audio]", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", str(DURATION),
        "-movflags", "+faststart", str(OUTPUT_FILE),
    ]
    subprocess.run(command, check=True)
    return OUTPUT_FILE


def main() -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    generate_music()
    render_stills()
    target = mix_audio(ffmpeg, render_video(ffmpeg))
    print(f"Created {target}")


if __name__ == "__main__":
    main()
