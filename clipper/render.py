import json
import os
import subprocess
from pathlib import Path

from .captions import CaptionStyle, build_caption_track, render_hook
from .reframe import OUT_H, Plan, frame_filter, plan_clip

HOOK_SECONDS = 3.0
_ENCODER: list[str] | None = None


def video_encoder() -> list[str]:
    """Apple's hardware H.264 encoder when available (about 2x faster, frees the CPU), else libx264."""
    global _ENCODER
    if _ENCODER is None:
        choice = os.getenv("VIDEO_ENCODER", "auto")
        if choice == "auto":
            encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                      capture_output=True, text=True).stdout
            choice = "h264_videotoolbox" if "h264_videotoolbox" in encoders else "libx264"
        if choice == "h264_videotoolbox":
            _ENCODER = ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "12M", "-bufsize", "16M"]
        else:
            _ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return _ENCODER


def probe_size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return s["width"], s["height"]


def render_clip(
    source: Path,
    start: float,
    end: float,
    out_path: Path,
    words: list[dict] | None = None,
    layout: str = "auto",
    crop_x: float = 0.5,
    caption_y: float | None = None,
    hook_text: str = "",
    style: CaptionStyle | None = None,
) -> tuple[Path, Plan]:
    from .limits import heavy_slot

    with heavy_slot("render"):
        return _render(source, start, end, out_path, words, layout, crop_x, caption_y, hook_text, style)


def _render(source, start, end, out_path, words, layout, crop_x, caption_y, hook_text, style):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    style = style or CaptionStyle()
    duration = end - start
    tmp = out_path.parent / f".{out_path.stem}_work"

    plan = plan_clip(source, start, end, layout, crop_x)
    cap_y = caption_y if caption_y is not None else plan.caption_y

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-stats",
           "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source)]
    graph = [frame_filter(plan, tmp)]
    last, n_inputs = "base", 1

    if words:
        concat = build_caption_track(words, start, end, tmp / "captions", style)
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat)]
        y = int(OUT_H * cap_y - style.height / 2)
        graph.append(f"[{n_inputs}:v]format=rgba[caps];[{last}][caps]overlay=0:{y}:eof_action=pass[vcap]")
        last, n_inputs = "vcap", n_inputs + 1

    if hook_text:
        hook_png = tmp / "hook.png"
        hook = render_hook(hook_text, style)
        hook.save(hook_png)
        cmd += ["-loop", "1", "-t", f"{HOOK_SECONDS}", "-i", str(hook_png)]
        y = int(OUT_H * plan.hook_y)
        graph.append(
            f"[{n_inputs}:v]format=rgba,fade=t=out:st={HOOK_SECONDS - 0.3}:d=0.3:alpha=1[hook];"
            f"[{last}][hook]overlay=0:{y}:eof_action=pass[vhook]"
        )
        last, n_inputs = "vhook", n_inputs + 1

    cmd += [
        "-filter_complex", ";".join(graph),
        "-map", f"[{last}]", "-map", "0:a?",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-r", "30", *video_encoder(),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out_path),
    ]
    print(f"[render] {out_path.name} ({duration:.1f}s, layout={plan.layout})")
    subprocess.run(cmd, check=True)
    return out_path, plan
