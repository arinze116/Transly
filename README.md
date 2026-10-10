# Transly

Transly is an async Telegram bot that accepts audio, video, and supported public media URLs, extracts speech with FFmpeg, and returns timestamped English transcripts directly in Telegram. It uses Groq Whisper Large v3 for transcription and Groq LLaMA for translation.

## Requirements

- Python 3.11+
- `ffmpeg` on the system path
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A Groq API key from [console.groq.com](https://console.groq.com) (free, no card needed)

## Local setup

```bash
sudo apt install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Set `TELEGRAM_BOT_TOKEN` (or the alias `BOT_TOKEN`) and `GROQ_API_KEY` in `.env`.

```env
BOT_TOKEN=your_telegram_bot_token
GROQ_API_KEY=gsk_your_groq_key
GROQ_MODEL=whisper-large-v3
DOWNLOAD_DIR=/tmp/transly
MAX_FILE_SIZE_MB=20
```

Telegram Bot API downloads are limited to 20 MB by default; larger media should be sent as a URL.

## Usage

- `/start` — welcome message and quick guide
- `/help` — supported formats and limits
- `/transcribe <URL>` — process a URL explicitly
- `/status` — check Groq API availability
- Audio/video files and URLs can also be sent directly without a command

## How it works

1. URL preflight rejects private/non-global addresses, direct and X-media HTTP fetches disable redirects and enforce a download-size cap. `yt-dlp` performs additional network requests internally, and DNS preflight alone cannot prevent DNS rebinding; deploy the downloader with network egress restrictions that block loopback, private, link-local, and cloud-metadata ranges.
2. Video audio is extracted and normalised with `ffmpeg`
3. Audio is transcribed to English via Groq Whisper Large v3
4. Non-English segments are translated via Groq LLaMA
5. Temporary job directories are deleted after processing

## Deployment (PM2)

```bash
pm2 start ecosystem.config.js
pm2 save
pm2 logs transly
```

The bot uses a persistent SQLite job queue. `DATABASE_URL` is abstracted so PostgreSQL can be introduced without changing Telegram handlers.