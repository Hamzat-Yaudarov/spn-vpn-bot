"""Generate the real DmitryNeural narration, word timing and original sound bed."""
import asyncio
import html
import json
import subprocess
import wave
from pathlib import Path

import edge_tts
import imageio_ffmpeg
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
OUT = ROOT / "output"
CACHE = ROOT / ".cache" / "voice"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 48000


def decode(path, speed=1.0):
    data = subprocess.check_output([
        FFMPEG, "-v", "error", "-i", str(path), "-af", f"atempo={speed}",
        "-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1",
    ])
    return np.frombuffer(data, dtype=np.float32)


def wav(path, data):
    stereo = data if data.ndim == 2 else np.stack([data, data], axis=1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((np.clip(stereo, -1, 1) * 32767).astype("<i2").tobytes())


async def synth(segment, voice):
    mp3 = CACHE / f"{segment['id']}.mp3"
    meta = CACHE / f"{segment['id']}.json"
    if not (mp3.exists() and meta.exists()):
        words = []
        communicate = edge_tts.Communicate(
            segment["text"], voice, rate="+6%", pitch="-3Hz", boundary="WordBoundary"
        )
        with mp3.open("wb") as f:
            async for event in communicate.stream():
                if event["type"] == "audio":
                    f.write(event["data"])
                elif event["type"] == "WordBoundary":
                    words.append({
                        "text": html.unescape(event["text"]),
                        "start": event["offset"] / 10_000_000,
                        "end": (event["offset"] + event["duration"]) / 10_000_000,
                    })
        if not words:
            raise RuntimeError("DmitryNeural returned no word timing; do not fabricate sync")
        meta.write_text(json.dumps(words, ensure_ascii=False, indent=2))
    words = json.loads(meta.read_text())
    available = segment["end"] - segment["start"]
    speed = max(1.0, (words[-1]["end"] + 0.2) / available)
    if speed > 1.3:
        raise RuntimeError(f"Voice too fast for scene {segment['id']}: {speed:.2f}")
    audio = decode(mp3, speed)
    adjusted = [dict(w, start=w["start"] / speed + segment["start"],
                     end=w["end"] / speed + segment["start"]) for w in words]
    print(f"{segment['id']}: {len(audio)/SR:.2f}s, tempo {speed:.3f}, {len(words)} words", flush=True)
    return audio, adjusted, {"scene": segment["id"], "tempo": speed, "seconds": len(audio) / SR}


def caption_groups(words):
    groups, group = [], []
    for word in words:
        if group and (len(group) >= 4 or len(" ".join(w["text"] for w in group + [word])) > 27
                      or word["start"] - group[-1]["end"] > 0.4):
            groups.append(group)
            group = []
        group.append(word)
    if group:
        groups.append(group)
    return [{"start": g[0]["start"], "end": g[-1]["end"] + 0.10, "words": g} for g in groups]


def stamp(t):
    ms = round(t * 1000)
    return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"


async def main():
    for path in (PUBLIC, OUT, CACHE):
        path.mkdir(parents=True, exist_ok=True)
    content = json.loads((ROOT / "content.json").read_text())
    length = round(content["duration"] * SR)
    voice = np.zeros(length, dtype=np.float32)
    words, reports = [], []
    for segment in content["segments"]:
        clip, timing, report = await synth(segment, content["voice"])
        start = round(segment["start"] * SR)
        end = min(start + len(clip), length)
        voice[start:end] += clip[:end-start]
        words.extend(timing)
        reports.append(report)
    voice *= 0.80 / max(float(np.max(np.abs(voice))), 0.001)
    wav(PUBLIC / "voice.wav", voice)

    # Original minimal electronic bed, generated here; no third-party music samples.
    t = np.arange(length) / SR
    music = np.zeros(length, dtype=np.float64)
    for i, root in enumerate((65.406, 55.0, 73.416, 61.735, 65.406)):
        a, b = i * 8 * SR, min((i + 1) * 8 * SR, length)
        u = np.arange(b-a) / SR
        env = np.minimum(u / 1.0, 1) * np.minimum((8-u) / 1.4, 1)
        chord = sum(np.sin(2*np.pi*root*r*u + k*0.7)/(k+1)
                    for k, r in enumerate((1, 2, 3, 5)))
        music[a:b] += 0.013 * chord * env
    # A sparse high pluck is deliberately far below narration.
    for beat in np.arange(0.6, 39.0, 1.2):
        a = int(beat * SR)
        n = min(int(0.32 * SR), length - a)
        u = np.arange(n) / SR
        music[a:a+n] += 0.010 * np.sin(2*np.pi*523.25*u) * np.exp(-u*18)
    fx = np.zeros(length)
    rng = np.random.default_rng(41)
    for moment in (4, 12, 20, 30):
        n = int(.22 * SR)
        a = int((moment-.11) * SR)
        noise = rng.standard_normal(n)
        noise = np.convolve(noise, np.ones(18)/18, mode="same")
        fx[a:a+n] += noise * np.sin(np.linspace(0, np.pi, n))**2 * .12
    for moment in (0.4, 1.25, 5.2, 6.4, 7.6, 8.8, 13.5, 15.2, 17.3, 23.1, 26.5, 32.6, 36.2):
        n = int(.10 * SR)
        a = int(moment * SR)
        u = np.arange(n) / SR
        fx[a:a+n] += .035 * np.sin(2*np.pi*(1100*u - 1900*u*u)) * np.exp(-u*58)
    mix = voice + music + fx
    fade = np.clip((content["duration"] - t) / .5, 0, 1)
    mix *= np.minimum(t / .12, 1) * fade
    wav(CACHE / "mix-raw.wav", mix)
    subprocess.run([
        FFMPEG, "-y", "-v", "warning", "-i", str(CACHE / "mix-raw.wav"),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=7", "-ar", str(SR),
        str(PUBLIC / "mix.wav"),
    ], check=True)
    captions = caption_groups(words)
    (PUBLIC / "captions.json").write_text(json.dumps(captions, ensure_ascii=False, indent=2))
    (OUT / "source-code-pilot.srt").write_text("\n\n".join(
        f"{i+1}\n{stamp(c['start'])} --> {stamp(c['end'])}\n" + " ".join(w['text'] for w in c['words'])
        for i, c in enumerate(captions)
    ) + "\n")
    (OUT / "audio-report.json").write_text(json.dumps(reports, indent=2))
    print(f"Ready: {len(words)} timed words, {len(captions)} subtitles, 40s stereo mix", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
