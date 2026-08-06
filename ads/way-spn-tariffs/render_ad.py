#!/usr/bin/env python3
"""Render a vertical Way SPN overview of the regular and anti-jamming plans."""

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

WHITE = (255, 248, 232, 255)
MUTED = (205, 211, 216, 255)
INK = (5, 10, 16, 255)
BLUE = (91, 185, 255, 255)
BLUE_DARK = (28, 82, 132, 255)
TEAL = (26, 205, 161, 255)
GOLD = (244, 210, 123, 255)
GOLD_DARK = (199, 137, 45, 255)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size=size)


def symbol_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_SYMBOL, size=size)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def ease_out(value: float) -> float:
    value = clamp(value)
    return 1.0 - (1.0 - value) ** 3


def local_progress(t: float, start: float, end: float) -> float:
    return clamp((t - start) / max(end - start, 0.001))


def rounded_gradient(
    size: tuple[int, int],
    left: tuple[int, int, int],
    right: tuple[int, int, int],
    radius: int,
) -> Image.Image:
    width, height = size
    blend = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
    left_rgb = np.asarray(left, dtype=np.float32)[None, None, :]
    right_rgb = np.asarray(right, dtype=np.float32)[None, None, :]
    rgb = np.repeat(left_rgb * (1.0 - blend) + right_rgb * blend, height, axis=0).astype(np.uint8)
    image = Image.fromarray(rgb, "RGB").convert("RGBA")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.putalpha(mask)
    return image


def build_background() -> Image.Image:
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    top = np.array([10.0, 18.0, 29.0])
    bottom = np.array([4.0, 7.0, 11.0])
    mix = (y / HEIGHT)[..., None]
    rgb = top * (1.0 - mix) + bottom * mix
    glows = (
        (100, 280, 520, np.array([37.0, 94.0, 160.0])),
        (980, 770, 560, np.array([10.0, 104.0, 83.0])),
        (540, 1680, 620, np.array([109.0, 70.0, 23.0])),
    )
    for cx, cy, radius, color in glows:
        distance = ((x - cx) ** 2 + (y - cy) ** 2) / (radius * radius)
        strength = np.clip(1.0 - distance, 0.0, 1.0)[..., None] ** 2
        rgb += strength * color
    rng = np.random.default_rng(53)
    rgb += rng.normal(0.0, 1.5, (HEIGHT, WIDTH, 1))
    image = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(image)
    for gx in range(0, WIDTH, 90):
        draw.line((gx, 0, gx, HEIGHT), fill=(23, 34, 44, 255), width=1)
    for gy in range(0, HEIGHT, 90):
        draw.line((0, gy, WIDTH, gy), fill=(23, 34, 44, 255), width=1)
    return image


BACKGROUND = build_background()


def draw_wordmark(draw: ImageDraw.ImageDraw) -> None:
    draw.rounded_rectangle((72, 82, 166, 176), radius=28, fill=GOLD)
    draw.text((119, 132), "S", font=font(56, True), fill=INK, anchor="mm")
    draw.text((194, 103), "WAY SPN", font=font(38, True), fill=WHITE, anchor="la")
    draw.text((195, 151), "ДВА ВАРИАНТА", font=font(20, True), fill=GOLD, anchor="la")


def base_frame() -> Image.Image:
    frame = BACKGROUND.copy()
    draw_wordmark(ImageDraw.Draw(frame, "RGBA"))
    return frame


def plan_icon(draw: ImageDraw.ImageDraw, center: tuple[int, int], accent, shield: bool = False) -> None:
    cx, cy = center
    draw.ellipse((cx - 88, cy - 88, cx + 88, cy + 88), fill=(9, 20, 31, 230), outline=accent, width=5)
    if shield:
        points = [(cx, cy - 52), (cx + 49, cy - 33), (cx + 43, cy + 22), (cx, cy + 60), (cx - 43, cy + 22), (cx - 49, cy - 33)]
        draw.polygon(points, fill=(14, 85, 70, 255), outline=accent)
        draw.line((cx - 22, cy + 1, cx - 2, cy + 22, cx + 30, cy - 17), fill=WHITE, width=10, joint="curve")
    else:
        for offset in (-38, 0, 38):
            draw.rounded_rectangle((cx + offset - 20, cy - 49, cx + offset + 20, cy + 42), radius=10, fill=accent)
            draw.ellipse((cx + offset - 4, cy + 27, cx + offset + 4, cy + 35), fill=INK)


def plan_card(
    layer: Image.Image,
    box: tuple[int, int, int, int],
    plan: dict,
    accent,
    secondary,
    progress: float,
    shield: bool,
) -> None:
    draw = ImageDraw.Draw(layer, "RGBA")
    left, top, right, bottom = box
    shift = int((1.0 - ease_out(progress)) * 90)
    left += shift
    right += shift
    alpha = int(238 * ease_out(progress))
    draw.rounded_rectangle((left, top, right, bottom), radius=48, fill=(12, 20, 29, alpha), outline=accent[:3] + (alpha,), width=4)
    draw.rounded_rectangle((left + 28, top + 28, right - 28, top + 92), radius=28, fill=accent[:3] + (52,))
    draw.text((left + 52, top + 60), plan["title"], font=font(31, True), fill=accent, anchor="lm")
    plan_icon(draw, (right - 125, top + 190), accent, shield=shield)
    draw.text((left + 52, top + 230), plan["price"], font=symbol_font(108), fill=WHITE, anchor="lm")
    draw.rounded_rectangle((left + 52, top + 304, left + 258, top + 360), radius=24, fill=secondary)
    draw.text((left + 155, top + 333), plan["period"], font=font(24, True), fill=INK, anchor="mm")
    bullets = ((plan["devices"], "в базовом тарифе"), (plan["traffic"], plan["traffic_note"]))
    for index, (title, note) in enumerate(bullets):
        y = top + 445 + index * 165
        draw.ellipse((left + 52, y - 28, left + 108, y + 28), fill=accent)
        draw.line((left + 65, y, left + 76, y + 12, left + 96, y - 14), fill=INK, width=7, joint="curve")
        draw.text((left + 132, y - 14), title, font=font(35, True), fill=WHITE, anchor="lm")
        draw.text((left + 132, y + 38), note, font=font(27), fill=MUTED, anchor="lm")


def draw_scene_one(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    p = ease_out(local_progress(t, 0.0, 1.7) / 0.45)
    title_y = int(485 + (1.0 - p) * 75)
    draw.text((WIDTH // 2, title_y), "КАКОЙ ТАРИФ", font=font(66, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, title_y + 92), "ПОДОЙДЁТ ВАМ?", font=font(76, True), fill=GOLD, anchor="mm")
    draw.text((WIDTH // 2, title_y + 200), "Два варианта под разные сценарии", font=font(32), fill=MUTED, anchor="mm")

    regular = rounded_gradient((350, 250), BLUE[:3], BLUE_DARK[:3], 42)
    rdraw = ImageDraw.Draw(regular)
    rdraw.text((175, 76), "ОБЫЧНЫЙ", font=font(32, True), fill=INK, anchor="mm")
    rdraw.text((175, 160), "200 ₽", font=symbol_font(64), fill=WHITE, anchor="mm")
    rdraw.text((175, 218), "30 дней", font=font(25, True), fill=INK, anchor="mm")
    layer.alpha_composite(regular, (100, 920))

    bypass = rounded_gradient((350, 250), TEAL[:3], GOLD_DARK[:3], 42)
    bdraw = ImageDraw.Draw(bypass)
    bdraw.text((175, 67), "АНТИГЛУШИЛКА", font=font(25, True), fill=INK, anchor="mm")
    bdraw.text((175, 154), "300 ₽", font=symbol_font(64), fill=WHITE, anchor="mm")
    bdraw.text((175, 215), "200 ГБ", font=font(27, True), fill=INK, anchor="mm")
    layer.alpha_composite(bypass, (550, 920))
    draw.text((WIDTH // 2, 1270), "Сейчас сравним", font=font(34, True), fill=WHITE, anchor="mm")
    return layer


def draw_scene_two(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    p = local_progress(t, 1.7, 7.27)
    draw = ImageDraw.Draw(layer, "RGBA")
    draw.text((96, 300), "ВАРИАНТ 1", font=font(25, True), fill=BLUE, anchor="la")
    plan_card(layer, (96, 350, 900, 1250), CONFIG["regular"], BLUE, GOLD, clamp(p / 0.2), False)
    draw.text((WIDTH // 2, 1350), "Для привычного ежедневного использования", font=font(30), fill=MUTED, anchor="mm")
    return layer


def draw_scene_three(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    p = local_progress(t, 7.27, 11.84)
    draw = ImageDraw.Draw(layer, "RGBA")
    draw.text((96, 300), "ВАРИАНТ 2", font=font(25, True), fill=TEAL, anchor="la")
    plan_card(layer, (96, 350, 900, 1250), CONFIG["bypass"], TEAL, GOLD, clamp(p / 0.2), True)
    draw.text((WIDTH // 2, 1340), "Отдельный режим для сценариев", font=font(30), fill=MUTED, anchor="mm")
    draw.text((WIDTH // 2, 1385), "с сетевыми ограничениями", font=font(30, True), fill=WHITE, anchor="mm")
    return layer


def compact_plan(draw: ImageDraw.ImageDraw, y: int, title: str, price: str, detail: str, accent) -> None:
    draw.rounded_rectangle((96, y, 900, y + 235), radius=42, fill=(12, 20, 29, 235), outline=accent, width=4)
    draw.rounded_rectangle((124, y + 34, 144, y + 201), radius=10, fill=accent)
    draw.text((180, y + 62), title, font=font(31, True), fill=accent, anchor="la")
    draw.text((842, y + 70), price, font=symbol_font(62), fill=WHITE, anchor="rm")
    detail_lines = detail.split(" • ", 1)
    draw.text((180, y + 142), detail_lines[0], font=font(24), fill=MUTED, anchor="la")
    if len(detail_lines) > 1:
        draw.text((180, y + 184), detail_lines[1], font=font(24), fill=MUTED, anchor="la")


def draw_scene_four(t: float) -> Image.Image:
    layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer, "RGBA")
    draw.text((WIDTH // 2, 345), "ВЫБЕРИТЕ СВОЙ", font=font(55, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 425), "ВАРИАНТ", font=font(70, True), fill=GOLD, anchor="mm")
    compact_plan(draw, 535, "ОБЫЧНЫЙ", "200 ₽", "до 5 устройств включено • без заданного лимита*", BLUE)
    compact_plan(draw, 805, "С АНТИГЛУШИЛКОЙ", "300 ₽", "до 3 устройств включено • 200 ГБ на 30 дней", TEAL)
    button = rounded_gradient((760, 136), GOLD[:3], GOLD_DARK[:3], 44)
    ImageDraw.Draw(button).text((380, 68), "ОТКРЫТЬ TELEGRAM", font=font(40, True), fill=INK, anchor="mm")
    layer.alpha_composite(button, (140, 1145))
    draw.text((WIDTH // 2, 1365), CONFIG["bot_handle"], font=font(43, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 1430), "Ссылка — в профиле или описании", font=font(28), fill=MUTED, anchor="mm")
    draw.text((WIDTH // 2, 1540), "* без лимита трафика по условиям тарифа", font=font(23), fill=(205, 211, 216, 185), anchor="mm")
    return layer


def render_frame(t: float) -> Image.Image:
    frame = base_frame()
    if 0.0 <= t < 1.7:
        frame.alpha_composite(draw_scene_one(t))
    elif t < 7.27:
        frame.alpha_composite(draw_scene_two(t))
    elif t < 11.84:
        frame.alpha_composite(draw_scene_three(t))
    else:
        frame.alpha_composite(draw_scene_four(t))
    return frame.convert("RGB")


def render_cover() -> Image.Image:
    cover = base_frame()
    draw = ImageDraw.Draw(cover, "RGBA")
    draw.text((WIDTH // 2, 380), "ДВА ТАРИФА", font=font(68, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 470), "WAY SPN", font=font(84, True), fill=GOLD, anchor="mm")
    draw.text((WIDTH // 2, 555), "Какой вариант выбрать?", font=font(32), fill=MUTED, anchor="mm")
    compact_plan(draw, 690, "ОБЫЧНЫЙ", "200 ₽", "до 5 устройств включено • без заданного лимита*", BLUE)
    compact_plan(draw, 965, "С АНТИГЛУШИЛКОЙ", "300 ₽", "до 3 устройств включено • 200 ГБ на 30 дней", TEAL)
    draw.rounded_rectangle((140, 1305, 900, 1435), radius=42, fill=GOLD)
    draw.text((520, 1370), "СРАВНЕНИЕ ЗА 15 СЕКУНД", font=font(35, True), fill=INK, anchor="mm")
    draw.text((WIDTH // 2, 1530), CONFIG["bot_handle"], font=font(39, True), fill=WHITE, anchor="mm")
    draw.text((WIDTH // 2, 1605), "* по условиям тарифа", font=font(23), fill=MUTED, anchor="mm")
    return cover.convert("RGB")


def render_stills() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for index, t in enumerate((0.9, 4.5, 9.2, 13.3), start=1):
        render_frame(t).save(ASSETS / f"scene-{index:02d}.png", optimize=True)
    render_cover().save(POSTER_FILE, quality=94, subsampling=0)


def generate_music() -> None:
    AUDIO.mkdir(parents=True, exist_ok=True)
    sample_rate = 44_100
    count = int(DURATION * sample_rate)
    timeline = np.arange(count, dtype=np.float64) / sample_rate
    music = np.zeros(count, dtype=np.float64)
    chords = (
        (0.0, (73.42, 110.00, 146.83)),
        (1.7, (82.41, 123.47, 164.81)),
        (7.27, (98.00, 146.83, 196.00)),
        (11.84, (110.00, 164.81, 220.00)),
    )
    for start, frequencies in chords:
        end = next((point for point, _ in chords if point > start), DURATION)
        mask = (timeline >= start) & (timeline < end)
        local = timeline[mask] - start
        envelope = np.minimum(local / 0.22, 1.0) * np.minimum((end - start - local) / 0.28, 1.0)
        pad = sum(np.sin(math.tau * frequency * local + i * 0.8) for i, frequency in enumerate(frequencies)) / len(frequencies)
        music[mask] += 0.15 * pad * np.clip(envelope, 0.0, 1.0)
    for beat in np.arange(0.0, DURATION, 0.5):
        start = int(beat * sample_rate)
        length = min(int(0.16 * sample_rate), count - start)
        local = np.arange(length) / sample_rate
        pulse = np.sin(math.tau * (64.0 - 24.0 * local) * local) * np.exp(-local * 22.0)
        music[start:start + length] += 0.18 * pulse
    for moment in (1.7, 7.27, 11.84):
        start = int((moment - 0.35) * sample_rate)
        length = min(int(0.7 * sample_rate), count - start)
        local = np.arange(length) / sample_rate
        sweep = np.sin(math.tau * (260.0 * local + 420.0 * local * local)) * np.sin(np.pi * local / 0.7) ** 2
        music[start:start + length] += 0.05 * sweep
    peak = max(1e-6, float(np.max(np.abs(music))))
    stereo = np.column_stack((music / peak * 0.64, music / peak * 0.61))
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
        f"[1:a]volume=1.16,aformat=channel_layouts=stereo,apad=pad_dur={DURATION}[voice];"
        "[2:a]volume=0.17[music];"
        f"[voice][music]amix=inputs=2:duration=longest:dropout_transition=0,atrim=0:{DURATION},"
        "afade=t=out:st=14.4:d=0.6,loudnorm=I=-16:LRA=7:TP=-1.5[audio]"
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
