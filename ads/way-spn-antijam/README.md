# Реклама Way SPN: антиглушилка

Вертикальный ролик 1080×1920 для Telegram Stories, Reels и Shorts.

## Готовые материалы

- `output/way-spn-antijam-story-safe-v2.mp4` — версия для публикации;
- `output/way-spn-antijam-poster-safe-v2.jpg` — обложка для публикации;
- `assets/safe-v2/scene-01.png` … `scene-04.png` — сцены версии `safe-v2`;
- `audio/voice-safe-v2.mp3` — озвучка версии `safe-v2`;
- `audio/music.wav` — оригинальная фоновая музыка;
- `voice-script-safe-v2.txt` — текст диктора версии `safe-v2`;
- `timings.json` — текст, тайминги, имена выходных файлов и ссылка;
- `render_ad.py` — редактируемый генератор ролика.

Файлы без суффикса `safe-v2` оставлены как исходная версия и для новых публикаций не используются.

## Сборка

Установить `pillow`, `numpy`, `imageio-ffmpeg` и `edge-tts`, затем выполнить:

```bash
edge-tts --voice ru-RU-DmitryNeural --rate=+35% --pitch=-6Hz \
  --file voice-script-safe-v2.txt --write-media audio/voice-safe-v2.mp3
python render_ad.py
```

Перед публикацией создать отслеживаемую ссылку в Telegram-боте:

```text
/new_link ig_d01 Instagram Reels — день 01 — антиглушилка
/new_link tt_d01 TikTok — день 01 — антиглушилка
/new_link yt_d01 YouTube Shorts — день 01 — антиглушилка
```

Для каждой площадки используется собственная ссылка из ответа бота. Полный пакет текстов находится в `../organic-launch/publishing/day-01.md`.
