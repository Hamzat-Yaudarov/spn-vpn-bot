#!/bin/bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/way-spn-contest.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT
export CLANG_MODULE_CACHE_PATH="${CLANG_MODULE_CACHE_PATH:-$work/swift-cache}"
mkdir -p "$CLANG_MODULE_CACHE_PATH"

if [[ -n "${FFMPEG_BIN:-}" ]]; then
    ffmpeg_bin="$FFMPEG_BIN"
elif command -v ffmpeg >/dev/null; then
    ffmpeg_bin="$(command -v ffmpeg)"
else
    composer="$root/../source-code-pilot/node_modules/@remotion/compositor-darwin-$(uname -m)"
    if [[ "$(uname -m)" == x86_64 ]]; then
        composer="$root/../source-code-pilot/node_modules/@remotion/compositor-darwin-x64"
    fi
    ffmpeg_bin="$composer/ffmpeg"
    export DYLD_LIBRARY_PATH="$composer${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi

if [[ ! -x "$ffmpeg_bin" ]]; then
    printf 'FFmpeg with libx264 and AAC support is required. Set FFMPEG_BIN.\n' >&2
    exit 1
fi

swiftc -O -module-cache-path "$CLANG_MODULE_CACHE_PATH" "$root/render.swift" -o "$work/renderer"
content="${CONTEST_COPY:-$root/content.json}"
destination="${CONTEST_OUTPUT:-$root/output}"
mkdir -p "$destination"
"$work/renderer" "$content" audio "$work/score.wav"
"$work/renderer" "$content" stills "$work/stills"
"$work/renderer" "$content" list > "$work/variants"
while IFS= read -r variant; do
    name="way-spn-contest-$variant"
    "$work/renderer" "$content" frames "$variant" |
        "$ffmpeg_bin" -hide_banner -loglevel error -y \
            -f image2pipe -vcodec mjpeg -framerate 30 -i - \
            -i "$work/score.wav" -map 0:v:0 -map 1:a:0 \
            -c:v libx264 -preset fast -threads 4 -crf 18 -pix_fmt yuv420p \
            -c:a aac -b:a 128k -movflags +faststart -shortest "$work/$name.mp4"
    mv -- "$work/$name.mp4" "$destination/$name.mp4"
    cp -- "$work/stills/$variant-1.jpg" "$destination/$name.jpg"
    printf 'Rendered %s\n' "$destination/$name.mp4"
done < "$work/variants"
