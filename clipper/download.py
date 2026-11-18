import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp


@dataclass
class Source:
    path: Path
    id: str
    title: str = ""
    url: str | None = None
    chat_times: list[float] = field(default_factory=list)
    heatmap: list[dict] = field(default_factory=list)
    clip_prefix: str = ""


def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", source))


def _find_offsets(node, out: list[float]) -> None:
    """Collect chat message offsets from any yt-dlp rechat JSON shape."""
    if isinstance(node, dict):
        for key in ("content_offset_seconds", "contentOffsetSeconds"):
            if isinstance(node.get(key), (int, float)):
                out.append(float(node[key]))
        for v in node.values():
            _find_offsets(v, out)
    elif isinstance(node, list):
        for v in node:
            _find_offsets(v, out)


def _load_chat(work_dir: Path, video_id: str) -> list[float]:
    times: list[float] = []
    for f in work_dir.glob(f"{video_id}*.rechat*.json"):
        try:
            _find_offsets(json.loads(f.read_text()), times)
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(times)


def download(url: str, work_dir: Path) -> Source:
    """Download a YouTube / Twitch VOD (or anything yt-dlp supports) as mp4, plus audience signals."""
    work_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "no_warnings": True,
        "writesubtitles": True,
        "subtitleslangs": ["rechat"],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        video_id = info["id"]
        path = work_dir / f"{video_id}.mp4"
        info_path = work_dir / f"{video_id}.info.json"
        if path.exists():
            print(f"[download] cached: {path}")
        else:
            ydl.download([url])
        info_path.write_text(json.dumps(ydl.sanitize_info(info)))

    src = Source(
        path=path,
        id=video_id,
        title=info.get("title", ""),
        url=url,
        chat_times=_load_chat(work_dir, video_id),
        heatmap=info.get("heatmap") or [],
    )
    print(f"[download] chat messages: {len(src.chat_times)}, heatmap points: {len(src.heatmap)}")
    return src


def resolve_source(source: str, work_dir: Path) -> Source:
    if is_url(source):
        return download(source, work_dir)
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return Source(path=path, id=path.stem, title=path.stem)
