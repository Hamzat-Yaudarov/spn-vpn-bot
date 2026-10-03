#!/usr/bin/env python3
"""Build a lo-fi TikTok carousel and short video for Way SPN."""

from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "assets" / "source" / "cat-calculator-v1.png"
OUTPUT = ROOT / "output"
CAROUSEL = OUTPUT / "carousel"
TEMP = ROOT / ".render"
AUDIO = ROOT / "audio"

WIDTH = 1080
HEIGHT = 1920
FPS = 30
SLIDE_SECONDS = 2.4
DURATION = SLIDE_SECONDS * 4

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_BLACK = "/System/Library/Fonts/Supplemental/Arial Black.ttf"

WHITE = (247, 246, 242)
BLACK = (10, 11, 13)
INK = (22, 22, 22)
MUTED = (172, 176, 181)
CHAT_BG = (19, 27, 34)
CHAT_IN = (37, 49, 58)
CHAT_BLUE = (47, 120, 177)
GREEN = (101, 194, 149)


def font(size: int, bold: bool = False, black: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BLACK if black else FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(path, size=size)


def cover(image: Image.Image, size: tuple[int, int], zoom: float = 1.0, anchor_y: float = 0.5) -> Image.Image:
    target_w, target_h = size
    ratio = max(target_w / image.width, target_h / image.height) * zoom
    resized = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - target_w) // 2)
    extra_y = max(0, resized.height - target_h)
    top = round(extra_y * anchor_y)
    return resized.crop((left, top, left + target_w, top + target_h))


def add_grain(image: Image.Image, strength: float = 3.0, seed: int = 42) -> Image.Image:
    rng = np.random.default_rng(seed)
    data = np.asarray(image.convert("RGB"), dtype=np.int16)
    noise = rng.normal(0.0, strength, data.shape[:2])[..., None]
    return Image.fromarray(np.clip(data + noise, 0, 255).astype(np.uint8), "RGB")


def fit_font(text: str, max_width: int, start_size: int, min_size: int = 34, black: bool = True):
    for size in range(start_size, min_size - 1, -2):
        candidate = font(size, bold=not black, black=black)
        if candidate.getlength(text) <= max_width:
            return candidate
    return font(min_size, bold=not black, black=black)


def top_banner(image: Image.Image, lines: list[str], *, dark: bool = False) -> None:
    draw = ImageDraw.Draw(image)
    top = 118
    line_height = 84
    height = 70 + line_height * len(lines)
    fill = BLACK if dark else WHITE
    text_fill = WHITE if dark else BLACK
    draw.rectangle((0, top, WIDTH, top + height), fill=fill)
    y = top + 50
    for line in lines:
        face = fit_font(line, WIDTH - 116, 60, 42, black=True)
        draw.text((58, y), line, font=face, fill=text_fill, anchor="la")
        y += line_height


def slide_one(cat: Image.Image) -> Image.Image:
    image = cover(cat, (WIDTH, HEIGHT), zoom=1.03, anchor_y=0.56)
    image = ImageEnhance.Brightness(image).enhance(0.72)
    image = add_grain(image, 3.5, 11)
    top_banner(image, ["Я: возьму VPN", "чисто для себя"])
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((58, 1515, 666, 1611), radius=34, fill=(0, 0, 0, 165))
    draw.text((91, 1563), "ничего сложного", font=font(34, bold=True), fill=WHITE, anchor="lm")
    return image


def chat_bubble(draw: ImageDraw.ImageDraw, y: int, sender: str, text: str, *, outgoing: bool = False) -> None:
    left = 315 if outgoing else 112
    right = 905 if outgoing else 790
    fill = CHAT_BLUE if outgoing else CHAT_IN
    draw.text((left + 20, y - 26), sender, font=font(26, bold=True), fill=GREEN if not outgoing else (143, 203, 240), anchor="la")
    draw.rounded_rectangle((left, y, right, y + 148), radius=38, fill=fill)
    draw.text((left + 34, y + 54), text, font=font(35, bold=True), fill=WHITE, anchor="la")
    draw.text((right - 27, y + 119), "21:48", font=font(20), fill=(206, 213, 218), anchor="rm")


def slide_two(_: Image.Image) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), CHAT_BG)
    draw = ImageDraw.Draw(image)
    top_banner(image, ["через пять минут:"], dark=False)
    draw.rounded_rectangle((82, 390, 932, 1510), radius=58, fill=(12, 19, 24), outline=(55, 69, 78), width=3)
    draw.ellipse((128, 442, 216, 530), fill=(72, 91, 101))
    draw.text((172, 486), "С", font=font(38, bold=True), fill=WHITE, anchor="mm")
    draw.text((244, 468), "Семейный чат", font=font(34, bold=True), fill=WHITE, anchor="la")
    draw.text((244, 514), "4 участника", font=font(24), fill=MUTED, anchor="la")
    draw.line((110, 566, 904, 566), fill=(50, 61, 68), width=2)
    chat_bubble(draw, 650, "Мама", "Мне тоже подключи")
    chat_bubble(draw, 890, "Сестра", "И мне на ноутбук")
    chat_bubble(draw, 1130, "Дом", "А на телевизор можно?")
    draw.text((112, 1605), "я:", font=font(34, bold=True), fill=MUTED, anchor="la")
    draw.text((184, 1605), "…", font=font(52, bold=True), fill=WHITE, anchor="la")
    return add_grain(image, 2.2, 22)


def slide_three(cat: Image.Image) -> Image.Image:
    image = cover(cat, (WIDTH, HEIGHT), zoom=1.22, anchor_y=0.60)
    image = add_grain(image, 3.8, 33)
    top_banner(image, ["Я, считающий", "устройства:"])
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((68, 1450, 886, 1665), radius=36, fill=(0, 0, 0, 190))
    draw.text((108, 1510), "1… 2… 3… 4… 5…", font=font(49, black=True), fill=WHITE, anchor="la")
    draw.text((108, 1589), "нормально.", font=font(55, black=True), fill=(146, 225, 181), anchor="la")
    return image


def slide_four(cat: Image.Image) -> Image.Image:
    background = cover(cat, (WIDTH, HEIGHT), zoom=1.10, anchor_y=0.58).filter(ImageFilter.GaussianBlur(13))
    background = ImageEnhance.Brightness(background).enhance(0.25)
    image = background.convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 0, WIDTH, HEIGHT), fill=(5, 7, 9, 115))
    top_banner(image, ["короче, всем хватило"])
    draw.text((78, 535), "до 5", font=font(112, black=True), fill=WHITE, anchor="la")
    draw.text((78, 665), "устройств", font=font(86, black=True), fill=WHITE, anchor="la")
    draw.rounded_rectangle((78, 805, 910, 1165), radius=44, fill=(10, 14, 17, 210))
    draw.text((122, 872), "обычный Way SPN", font=font(40, bold=True), fill=(145, 224, 180), anchor="la")
    draw.text((122, 948), "200 рублей / 30 дней", font=font(38, bold=True), fill=WHITE, anchor="la")
    draw.text((122, 1024), "без заданного лимита трафика*", font=font(29), fill=MUTED, anchor="la")
    draw.text((78, 1280), "если пригодится:", font=font(31), fill=MUTED, anchor="la")
    draw.text((78, 1360), "@WaySPN_robot", font=font(49, bold=True), fill=WHITE, anchor="la")
    draw.text((78, 1480), "* по условиям тарифа", font=font(23), fill=(168, 173, 178), anchor="la")
    return add_grain(image, 3.0, 44)


def build_slides() -> list[Image.Image]:
    cat = Image.open(SOURCE).convert("RGB")
    return [slide_one(cat), slide_two(cat), slide_three(cat), slide_four(cat)]


def save_slides(slides: list[Image.Image]) -> None:
    CAROUSEL.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(slides, start=1):
        image.save(CAROUSEL / f"slide-{index:02d}.jpg", quality=94, subsampling=0)
    slides[0].save(OUTPUT / "way-spn-family-meme-cover-v1.jpg", quality=94, subsampling=0)


def frame_from_slide(slide: Image.Image, progress: float, index: int) -> Image.Image:
    zoom = 1.0 + (0.016 if index in (0, 2) else 0.006) * progress
    if zoom <= 1.0001:
        return slide
    scaled = slide.resize((round(WIDTH * zoom), round(HEIGHT * zoom)), Image.Resampling.LANCZOS)
    left = (scaled.width - WIDTH) // 2
    top = (scaled.height - HEIGHT) // 2
    return scaled.crop((left, top, left + WIDTH, top + HEIGHT))


def render_video(slides: list[Image.Image], ffmpeg: str) -> Path:
    TEMP.mkdir(parents=True, exist_ok=True)
    silent = TEMP / "silent.mp4"
    command = [
        ffmpeg, "-y", "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264",
        "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    frame_count = round(DURATION * FPS)
    fade = 0.16
    for frame_number in range(frame_count):
        t = frame_number / FPS
        index = min(3, int(t // SLIDE_SECONDS))
        local = (t - index * SLIDE_SECONDS) / SLIDE_SECONDS
        frame = frame_from_slide(slides[index], local, index)
        if local > 1.0 - fade and index < 3:
            blend = (local - (1.0 - fade)) / fade
            next_frame = frame_from_slide(slides[index + 1], 0.0, index + 1)
            frame = Image.blend(frame, next_frame, blend)
        process.stdin.write(np.asarray(frame, dtype=np.uint8).tobytes())
    process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError("Video render failed")
    return silent


def generate_placeholder_audio() -> Path:
    AUDIO.mkdir(parents=True, exist_ok=True)
    target = AUDIO / "original-placeholder.wav"
    sample_rate = 44_100
    count = round(DURATION * sample_rate)
    timeline = np.arange(count, dtype=np.float64) / sample_rate
    signal = np.zeros(count, dtype=np.float64)
    notes = (110.0, 146.83, 164.81)
    pad = sum(np.sin(math.tau * note * timeline + i * 0.7) for i, note in enumerate(notes)) / len(notes)
    signal += 0.045 * pad
    for moment in (0.0, 2.4, 4.8, 7.2):
        start = round((moment + 0.08) * sample_rate)
        length = min(round(0.13 * sample_rate), count - start)
        local = np.arange(length, dtype=np.float64) / sample_rate
        click = np.sin(math.tau * 410.0 * local) * np.exp(-local * 29.0)
        signal[start:start + length] += 0.11 * click
    fade = np.minimum(timeline / 0.35, 1.0) * np.minimum((DURATION - timeline) / 0.45, 1.0)
    signal *= np.clip(fade, 0.0, 1.0)
    peak = max(1e-6, float(np.max(np.abs(signal))))
    stereo = np.column_stack((signal / peak * 0.52, signal / peak * 0.48))
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return target


def mix_audio(silent: Path, audio: Path, ffmpeg: str) -> Path:
    target = OUTPUT / "way-spn-family-meme-v1.mp4"
    command = [
        ffmpeg, "-y", "-i", str(silent), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-t", str(DURATION), "-movflags", "+faststart", str(target),
    ]
    subprocess.run(command, check=True)
    return target


def main() -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    slides = build_slides()
    save_slides(slides)
    target = mix_audio(render_video(slides, ffmpeg), generate_placeholder_audio(), ffmpeg)
    print(f"Created {target}")


if __name__ == "__main__":
    main()
