import json
import os
import subprocess
import tempfile
from pathlib import Path


def _extract_audio(src: Path, dst: Path, start: float | None, end: float | None) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    cmd += ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst)]
    subprocess.run(cmd, check=True)


_MODELS: dict = {}


def _load_model(name: str):
    if name not in _MODELS:
        import platform

        from faster_whisper import WhisperModel

        apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
        device = os.getenv("WHISPER_DEVICE") or ("mps" if apple_silicon else "auto")
        print(f"[transcribe] loading whisper '{name}' on {device}...")
        try:
            compute = "float16" if device == "mps" else "int8"
            _MODELS[name] = WhisperModel(name, device=device, compute_type=compute)
        except Exception as e:
            print(f"[transcribe] {device} unavailable ({e}); using cpu")
            _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODELS[name]


def transcribe(
    media: Path,
    start: float | None = None,
    end: float | None = None,
    model_name: str | None = None,
    cache_dir: Path | None = None,
) -> list[dict]:
    """Return a list of words: {"word", "start", "end"} in source-video seconds."""
    model_name = model_name or os.getenv("WHISPER_MODEL", "small")
    cache = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tag = f"{media.stem}.{model_name}.{start or 0:.0f}-{end or 'end'}"
        cache = cache_dir / f"{tag}.words.json"
        if cache.exists():
            print(f"[transcribe] cached: {cache}")
            return json.loads(cache.read_text())

    from .limits import heavy_slot

    model = _load_model(model_name)
    offset = start or 0.0

    with heavy_slot("transcribe"), tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        _extract_audio(media, wav, start, end)
        segments, info = model.transcribe(
            str(wav), word_timestamps=True, vad_filter=True, beam_size=5
        )
        print(f"[transcribe] language={info.language} duration={info.duration:.0f}s")
        words = []
        for seg in segments:
            for w in seg.words or []:
                text = w.word.strip()
                if text:
                    words.append({
                        "word": text,
                        "start": round(w.start + offset, 3),
                        "end": round(w.end + offset, 3),
                    })
            print(f"\r[transcribe] {seg.end:.0f}/{info.duration:.0f}s", end="", flush=True)
        print()

    if cache:
        cache.write_text(json.dumps(words))
    return words


def to_timed_lines(words: list[dict], max_gap: float = 1.0, max_words: int = 25) -> list[dict]:
    """Group words into sentence-ish lines for feeding to the LLM."""
    lines, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > max_gap or len(cur) >= max_words):
            lines.append(cur)
            cur = []
        cur.append(w)
        if w["word"][-1:] in ".?!":
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return [
        {"start": l[0]["start"], "end": l[-1]["end"], "text": " ".join(x["word"] for x in l)}
        for l in lines
    ]
