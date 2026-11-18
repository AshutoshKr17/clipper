import argparse
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from .captions import CaptionStyle
from .download import Source, resolve_source
from .render import render_clip
from .transcribe import to_timed_lines, transcribe

WORK = Path("work")
OUTPUT = Path("output")
WATCH_STATE = WORK / "watch_state.json"


def parse_time(value: str) -> float:
    seconds = 0.0
    for p in value.split(":"):
        seconds = seconds * 60 + float(p)
    return seconds


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "clip"


def platforms(args) -> list[str]:
    return [t.strip() for t in (args.upload or "").split(",") if t.strip()]


def publish_now(path: Path, meta: dict, args) -> dict:
    from .publish import instagram_caption, upload_instagram, upload_youtube

    results = {}
    for target in platforms(args):
        try:
            if target == "youtube":
                results["youtube"] = upload_youtube(
                    path, meta["title"], meta.get("description", ""), meta.get("tags"),
                    privacy=args.privacy, publish_at=meta.get("publish_at"),
                )
            elif target == "instagram":
                results["instagram"] = upload_instagram(path, instagram_caption(meta))
        except Exception as e:
            print(f"[publish] {target} failed: {e}")
            results[target] = f"error: {e}"
    return results


def render_one(src: Source, clip: dict, idx: int, args, full_words: list[dict] | None) -> dict:
    name = f"{src.clip_prefix}{idx:02d}_{slugify(clip['title'])}.mp4"
    out = OUTPUT / slugify(src.title or src.id) / name
    words = None
    if not args.no_captions:
        if full_words is not None:
            words = [w for w in full_words if w["end"] > clip["start"] and w["start"] < clip["end"]]
        else:
            words = transcribe(src.path, clip["start"], clip["end"], cache_dir=WORK / "transcripts")

    style = CaptionStyle(font_size=args.font_size, max_words=args.words_per_caption)
    _, plan = render_clip(
        src.path, clip["start"], clip["end"], out, words,
        layout=args.layout, crop_x=args.crop_x, caption_y=args.caption_y,
        hook_text="" if args.no_hook else clip.get("hook_text", ""), style=style,
    )
    return {**clip, "file": str(out), "layout": plan.layout, "source": src.url or str(src.path),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S")}


def distribute(clips: list[dict], args) -> None:
    targets = platforms(args)
    if not targets:
        return
    if args.schedule:
        from .scheduler import enqueue

        enqueue(clips, targets, args.schedule)
    else:
        for clip in clips:
            clip["published"] = publish_now(Path(clip["file"]), clip, args)


def write_manifest(clips: list[dict]) -> None:
    """Merge clips into the manifest.json next to them (keeps clips from earlier runs)."""
    if not clips:
        return
    path = Path(clips[0]["file"]).parent / "manifest.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    files = {c["file"] for c in clips}
    merged = [c for c in existing if c["file"] not in files] + clips
    path.write_text(json.dumps(merged, indent=2))
    print(f"\nDone. {len(clips)} clip(s) -> {path.parent}")


def process(src: Source, args, words: list[dict] | None = None,
            exclude: list[tuple[float, float]] | None = None) -> list[dict]:
    """Transcribe (unless words are given), pick highlights, render and distribute."""
    from .highlights import annotate_signals, find_highlights

    if words is None:
        words = transcribe(src.path, cache_dir=WORK / "transcripts")
    if not words:
        print("No speech detected; nothing to clip.")
        return []
    lines = annotate_signals(to_timed_lines(words), src.chat_times, src.heatmap)
    clips = find_highlights(lines, count=args.count, min_len=args.min_len, max_len=args.max_len)
    clips = [c for c in clips if c.get("score", 0) >= args.min_score]
    if exclude:
        clips = [c for c in clips if all(c["end"] <= a or c["start"] >= b for a, b in exclude)]
    if not clips:
        print("No clip-worthy moments found above the score threshold.")
        return []

    print("\nSelected highlights:")
    for i, c in enumerate(clips, 1):
        print(f"  {i}. [{c['start']:.1f}-{c['end']:.1f}] ({c.get('score', '?')}) {c['title']}"
              f"  | hook: {c.get('hook_text', '')}")
    if args.dry_run:
        return clips

    done = [render_one(src, c, i, args, words) for i, c in enumerate(clips, 1)]
    distribute(done, args)
    write_manifest(done)
    return done


def run_auto(source: str, args) -> list[dict]:
    return process(resolve_source(source, WORK / "downloads"), args)


def cmd_clip(args) -> None:
    src = resolve_source(args.source, WORK / "downloads")
    clip = {
        "start": parse_time(args.start),
        "end": parse_time(args.end),
        "title": args.title,
        "hook_text": args.hook or "",
        "description": args.description or "",
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
    }
    done = [render_one(src, clip, 1, args, None)]
    distribute(done, args)
    write_manifest(done)


def cmd_auto(args) -> None:
    for i, source in enumerate(args.sources, 1):
        print(f"\n===== [{i}/{len(args.sources)}] {source} =====")
        try:
            run_auto(source, args)
        except Exception as e:
            print(f"[auto] failed on {source}: {e}")


def watch_channel(channel: str, args) -> None:
    import yt_dlp

    state = json.loads(WATCH_STATE.read_text()) if WATCH_STATE.exists() else {}
    seen = set(state.get(channel, []))
    with yt_dlp.YoutubeDL({"extract_flat": True, "playlistend": args.latest, "quiet": True}) as ydl:
        info = ydl.extract_info(channel, download=False)
    entries = [e for e in info.get("entries", []) if e.get("id")]
    # Skip streams that are still live; the VOD gets picked up once it ends.
    new = [e for e in entries if e["id"] not in seen and e.get("live_status") not in ("is_live", "is_upcoming")]
    print(f"[watch] {channel}: {len(entries)} recent video(s), {len(new)} new")

    for entry in reversed(new):
        url = entry.get("url") or entry.get("webpage_url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={entry['id']}"
        print(f"\n[watch] processing {entry.get('title', entry['id'])}")
        try:
            run_auto(url, args)
        except Exception as e:
            print(f"[watch] failed on {url}: {e}")
            continue
        seen.add(entry["id"])
        state[channel] = sorted(seen)
        WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
        WATCH_STATE.write_text(json.dumps(state, indent=2))


def cmd_watch(args) -> None:
    watch_channel(args.channel, args)


def cmd_live(args) -> None:
    from .live import run_live

    run_live(args.url, args)


def cmd_daemon(args) -> None:
    """Long-running autopilot: post due queue items and clip new VODs from saved channels."""
    from . import config
    from .scheduler import run_due

    last_watch = 0.0
    print(f"[daemon] started; queue every {args.interval} min")
    while True:
        cfg = config.load()
        try:
            run_due(privacy=cfg["defaults"]["privacy"])
        except Exception as e:
            print(f"[daemon] queue run failed: {e}")
        if cfg["channels"] and time.time() - last_watch >= cfg["watch_every_minutes"] * 60:
            last_watch = time.time()
            for channel in cfg["channels"]:
                watch_args = build_parser().parse_args(
                    ["watch", channel, *config.options_argv(cfg["defaults"])]
                )
                try:
                    watch_channel(channel, watch_args)
                except Exception as e:
                    print(f"[daemon] watch failed for {channel}: {e}")
        time.sleep(args.interval * 60)


def cmd_upload(args) -> None:
    meta = {
        "title": args.title,
        "description": args.description or "",
        "tags": [t.strip() for t in (args.tags or "").split(",") if t.strip()],
        "publish_at": args.publish_at,
    }
    print(json.dumps(publish_now(Path(args.file), meta, args), indent=2))


def cmd_queue(args) -> None:
    from .scheduler import run_due, show

    if args.action == "run":
        run_due(privacy=args.privacy)
    else:
        show()


def cmd_models(args) -> None:
    from openai import OpenAI

    from .highlights import gradient_key

    client = OpenAI(
        base_url=os.getenv("GRADIENT_BASE_URL", "https://inference.do-ai.run/v1/"),
        api_key=gradient_key(),
    )
    for m in sorted(client.models.list().data, key=lambda m: m.id):
        print(m.id)


def cmd_auth(args) -> None:
    from .publish import youtube_channel

    print(f"[auth] connected YouTube channel: {youtube_channel()}")


def add_render_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--layout", choices=["auto", "stream", "track", "blur", "crop"], default="auto",
                   help="auto picks per clip: stream (facecam over gameplay), track (follow speaker) or blur")
    p.add_argument("--crop-x", type=float, default=0.5, help="horizontal position for --layout crop (0-1)")
    p.add_argument("--caption-y", type=float, help="caption vertical position (0-1); default depends on layout")
    p.add_argument("--font-size", type=int, default=86)
    p.add_argument("--words-per-caption", type=int, default=3)
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--no-hook", action="store_true", help="don't overlay the hook text in the first seconds")
    add_publish_args(p)


def add_publish_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--upload", help="comma-separated platforms: youtube,instagram")
    p.add_argument("--schedule", help="queue posts into daily slots instead of posting now, e.g. '12:00,19:00'")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private",
                   help="YouTube visibility (unverified Google apps are forced to private)")


def add_auto_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--count", type=int, default=5, help="max clips per video (per segment when live)")
    p.add_argument("--min-len", type=float, default=20)
    p.add_argument("--max-len", type=float, default=59)
    p.add_argument("--min-score", type=int, default=0, help="drop clips the AI scores below this")
    p.add_argument("--dry-run", action="store_true", help="only print the selected highlights")
    add_render_args(p)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipper", description="Long video -> vertical Shorts/Reels")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("clip", help="cut one clip at given timestamps")
    p.add_argument("source", help="YouTube/Twitch URL or local video file")
    p.add_argument("--start", required=True, help="seconds or HH:MM:SS")
    p.add_argument("--end", required=True, help="seconds or HH:MM:SS")
    p.add_argument("--title", default="Clip")
    p.add_argument("--hook", help="on-screen hook text for the first 3 seconds")
    p.add_argument("--description")
    p.add_argument("--tags", help="comma-separated")
    add_render_args(p)
    p.set_defaults(func=cmd_clip)

    p = sub.add_parser("auto", help="transcribe, let the AI pick highlights, render (and post) them")
    p.add_argument("sources", nargs="+", help="one or more YouTube/Twitch URLs or local video files")
    add_auto_args(p)
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("live", help="clip a live Twitch/YouTube stream while it is running")
    p.add_argument("url", help="e.g. https://www.twitch.tv/NAME or a YouTube live URL")
    p.add_argument("--segment-minutes", type=float, default=5,
                   help="clip the stream in chunks of this length (each analysed with the previous one)")
    p.add_argument("--keep-recording", action="store_true", help="keep raw recorded segments on disk")
    add_auto_args(p)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("watch", help="clip every new video/VOD on a channel")
    p.add_argument("channel", help="e.g. https://www.twitch.tv/NAME/videos?filter=archives or https://www.youtube.com/@NAME/videos")
    p.add_argument("--latest", type=int, default=1, help="how many recent videos to consider each run")
    add_auto_args(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("daemon", help="autopilot loop: post due clips and clip new VODs from saved channels")
    p.add_argument("--interval", type=float, default=5, help="minutes between queue checks")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("upload", help="upload an already rendered clip")
    p.add_argument("file")
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--tags", help="comma-separated")
    p.add_argument("--publish-at", help="YouTube scheduled publish time, ISO 8601 UTC")
    add_publish_args(p)
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("queue", help="show the posting queue or post everything that is due")
    p.add_argument("action", choices=["list", "run"])
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("models", help="list models available to your DigitalOcean Gradient key")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("auth", help="connect your YouTube channel (opens a browser)")
    p.set_defaults(func=cmd_auth)
    return parser


def main(argv: list[str] | None = None) -> None:
    load_dotenv(override=True)
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
