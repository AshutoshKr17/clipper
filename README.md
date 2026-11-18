# clipper

Long Twitch/YouTube videos in, captioned vertical Shorts/Reels out, posted on a schedule.

Pipeline: `yt-dlp` download (+ Twitch chat replay and YouTube "most replayed" heatmap) →
`faster-whisper` word timestamps → LLM picks hook/build/payoff moments in two passes (select, then
refine endings + write titles and on-screen hooks) → face-aware 9:16 reframing → burned-in
word-highlight captions + hook banner → YouTube / Instagram upload, now or queued into daily slots.

Layouts are picked per clip (`--layout auto`):

| Layout | When | Look |
|---|---|---|
| `stream` | small, static face (webcam overlay) | facecam on top, gameplay below, captions on the seam |
| `track` | large face (talking head, podcast) | crop that follows the speaker |
| `blur` | no face | full frame over a blurred background |

## Setup

```bash
brew install ffmpeg
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # add GRADIENT_API_KEY
```

## App (recommended)

```bash
.venv/bin/streamlit run app.py   # opens http://localhost:8501
```

Pages: **Create clips** (paste many links), **Live** (clip a running Twitch/YouTube stream),
**Jobs** (progress + logs, stop), **Clips** (preview, edit titles, post or queue), **Queue**,
**Autopilot** (watch channels + auto-post in the background), **Settings** (Gradient key, model,
YouTube connection, Instagram token, defaults). Jobs keep running if you close the browser tab.

Heavy steps (transcription, rendering) are capped at `MAX_PARALLEL_HEAVY` (default 3) across all
jobs, so many streams can run at once without slowing each other to a crawl.

## CLI

```bash
# Clip a stream that is live right now (Ctrl+C to stop; the last segment is still processed)
.venv/bin/python -m clipper live "https://www.twitch.tv/NAME" --segment-minutes 5 --upload youtube --schedule "12:00,19:00"

# Let the AI find and render the best clips (dry run first to see its picks)
.venv/bin/python -m clipper auto "https://www.twitch.tv/videos/123456789" --dry-run
.venv/bin/python -m clipper auto "https://www.youtube.com/watch?v=..." --count 6 --min-score 60

# Render and queue posts into two daily slots
.venv/bin/python -m clipper auto URL --upload youtube,instagram --schedule "12:00,19:00"

# Manual clip
.venv/bin/python -m clipper clip URL --start 1:02:10 --end 1:02:55 --title "..." --hook "NO WAY"

# Queue
.venv/bin/python -m clipper queue list
.venv/bin/python -m clipper queue run
```

Outputs land in `output/<video-title>/` with a `manifest.json` (titles, descriptions, tags, scores).

## Autopilot (cron)

`crontab -e`, adjust the path:

```cron
# Every hour: clip new VODs from the channel and queue them
0 * * * * cd /Users/akushwaha/Documents/clipper && .venv/bin/python -m clipper watch "https://www.twitch.tv/NAME/videos?filter=archives" --count 4 --min-score 65 --upload youtube,instagram --schedule "12:00,19:00" >> work/watch.log 2>&1
# Every 10 minutes: post whatever is due
*/10 * * * * cd /Users/akushwaha/Documents/clipper && .venv/bin/python -m clipper queue run --privacy public >> work/queue.log 2>&1
```

The machine must be awake for cron to fire; a small always-on box or a DigitalOcean droplet works too.

## Publishing setup

**YouTube:** Google Cloud Console → new project → enable *YouTube Data API v3* → OAuth consent screen
(add yourself as a test user) → Credentials → OAuth client ID → *Desktop app* → save as
`client_secret.json`. The first upload opens a browser to log in; the token is cached in `token.json`.
Until the app passes Google's audit, API uploads are forced to **private**. The default quota
(10,000 units/day, ~1,600 per upload) allows about 6 uploads a day.

**Instagram:** needs an Instagram Business/Creator account linked to a Facebook Page and a Meta app
with `instagram_content_publish`. Put `IG_USER_ID` and a long-lived `IG_ACCESS_TOKEN` in `.env`.
Instagram's API has no scheduling, which is why posts go through the local queue.
