from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


@dataclass
class CaptionStyle:
    width: int = 1080
    height: int = 360
    font_path: str | None = None
    font_size: int = 86
    active_scale: float = 1.12
    color: tuple = (255, 255, 255, 255)
    active_color: tuple = (255, 221, 0, 255)
    stroke_color: tuple = (0, 0, 0, 255)
    stroke_width: int = 9
    max_words: int = 3
    max_chars: int = 18
    uppercase: bool = True

    def font(self, size: int) -> ImageFont.FreeTypeFont:
        path = self.font_path or next((p for p in FONT_CANDIDATES if Path(p).exists()), None)
        if not path:
            return ImageFont.load_default(size)
        return ImageFont.truetype(path, size)


def group_words(words: list[dict], style: CaptionStyle, max_gap: float = 0.6) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        text_len = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
        if cur and (
            len(cur) >= style.max_words
            or text_len > style.max_chars
            or w["start"] - cur[-1]["end"] > max_gap
        ):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w["word"][-1:] in ".?!,":
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _clean(word: str, style: CaptionStyle) -> str:
    word = word.strip().strip(",.")
    return word.upper() if style.uppercase else word


def render_chunk(chunk: list[dict], active: int, style: CaptionStyle) -> Image.Image:
    img = Image.new("RGBA", (style.width, style.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    texts = [_clean(w["word"], style) for w in chunk]

    size = style.font_size
    while True:
        fonts = [style.font(int(size * (style.active_scale if i == active else 1))) for i in range(len(texts))]
        space = draw.textlength(" ", font=fonts[0])
        widths = [draw.textlength(t, font=f) for t, f in zip(texts, fonts)]
        total = sum(widths) + space * (len(texts) - 1) + style.stroke_width * 2
        if total <= style.width * 0.9 or size <= 40:
            break
        size -= 6

    x = (style.width - total) / 2 + style.stroke_width
    cy = style.height / 2
    for i, (text, font, w) in enumerate(zip(texts, fonts, widths)):
        draw.text(
            (x, cy),
            text,
            font=font,
            fill=style.active_color if i == active else style.color,
            stroke_width=style.stroke_width,
            stroke_fill=style.stroke_color,
            anchor="lm",
        )
        x += w + space
    return img


def render_hook(text: str, style: CaptionStyle, max_width: int = 940) -> Image.Image:
    """White rounded banner with black text, wrapped to fit (the 'hook' title card)."""
    text = text.upper().strip()
    size = 64
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while True:
        font = style.font(size)
        lines, cur = [], ""
        for word in text.split():
            trial = f"{cur} {word}".strip()
            if probe.textlength(trial, font=font) <= max_width - 80 or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        if len(lines) <= 2 or size <= 40:
            break
        size -= 4

    line_h = int(size * 1.2)
    widths = [probe.textlength(l, font=font) for l in lines]
    box_w = int(max(widths) + 80)
    box_h = line_h * len(lines) + 50
    img = Image.new("RGBA", (style.width, box_h + 20), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x0 = (style.width - box_w) // 2
    draw.rounded_rectangle((x0, 10, x0 + box_w, 10 + box_h), radius=28, fill=(255, 255, 255, 245))
    for i, (line, w) in enumerate(zip(lines, widths)):
        draw.text(
            ((style.width - w) / 2, 10 + 25 + i * line_h + line_h / 2),
            line, font=font, fill=(0, 0, 0, 255), anchor="lm",
        )
    return img


def build_caption_track(
    words: list[dict], clip_start: float, clip_end: float, out_dir: Path, style: CaptionStyle
) -> Path:
    """Render caption frames and an ffmpeg concat list; returns the concat file path.

    Word times are in source seconds; output timeline starts at 0 = clip_start.
    """
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = clip_end - clip_start
    words = [
        {**w, "start": max(0.0, w["start"] - clip_start), "end": min(duration, w["end"] - clip_start)}
        for w in words
        if w["end"] > clip_start and w["start"] < clip_end
    ]

    blank = out_dir / "blank.png"
    Image.new("RGBA", (style.width, style.height), (0, 0, 0, 0)).save(blank)

    events: list[tuple[float, float, Path]] = []
    n = 0
    for chunk in group_words(words, style):
        chunk_end = min(chunk[-1]["end"] + 0.25, duration)
        for i, w in enumerate(chunk):
            t0 = w["start"]
            t1 = chunk[i + 1]["start"] if i + 1 < len(chunk) else chunk_end
            if t1 - t0 < 0.02:
                continue
            path = out_dir / f"cap_{n:05d}.png"
            render_chunk(chunk, i, style).save(path)
            events.append((t0, t1, path))
            n += 1

    lines, t = [], 0.0
    for t0, t1, path in events:
        t0 = max(t0, t)
        if t0 - t > 0.01:
            lines += [f"file '{blank}'", f"duration {t0 - t:.3f}"]
        if t1 > t0:
            lines += [f"file '{path}'", f"duration {t1 - t0:.3f}"]
            t = t1
    if duration - t > 0.01:
        lines += [f"file '{blank}'", f"duration {duration - t:.3f}"]
    # concat demuxer ignores the last entry's duration unless the file is repeated.
    lines.append(f"file '{blank}'")

    concat = out_dir / "captions.txt"
    concat.write_text("\n".join(lines) + "\n")
    return concat
