"""Live stream clipping.

The stream is recorded in fixed-length segments. Every finished segment is analysed together
with the previous one (so moments that straddle a boundary are not lost), clips that overlap
already-produced ones are skipped, and the rest are rendered and posted/queued immediately.
"""

import csv
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import yt_dlp

from .download import Source
from .transcribe import transcribe

WORK = Path("work")


def resolve_stream(url: str) -> dict:
    opts = {"format": "bv*[height<=1080]+ba/b[height<=1080]/b", "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info.get("is_live"):
        raise RuntimeError(f"{url} is not live right now")
    return info


def _input_args(info: dict) -> list[str]:
    """ffmpeg inputs + stream maps; YouTube live serves video and audio as separate HLS streams."""
    formats = info.get("requested_formats") or [info]
    args = []
    for f in formats:
        args += ["-i", f["url"]]
    if len(formats) == 2:
        return args + ["-map", "0:v:0", "-map", "1:a:0"]
    return args + ["-map", "0:v:0?", "-map", "0:a:0?"]


class TwitchChat(threading.Thread):
    """Anonymous Twitch IRC reader that records when each chat message arrived."""

    def __init__(self, channel: str, t0: float):
        super().__init__(daemon=True)
        self.channel = channel.lower()
        self.t0 = t0
        self.times: list[float] = []
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=360)
                sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
                sock.sendall(f"JOIN #{self.channel}\r\n".encode())
                buf = ""
                while not self.stopped.is_set():
                    data = sock.recv(4096).decode(errors="ignore")
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split("\r\n")
                    for line in lines:
                        if line.startswith("PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif " PRIVMSG " in line:
                            self.times.append(time.time() - self.t0)
            except OSError:
                time.sleep(5)


class Recorder(threading.Thread):
    """Records a live HLS stream into mpegts segments, reconnecting if ffmpeg drops."""

    def __init__(self, url: str, out_dir: Path, segment_seconds: float, t0: float):
        super().__init__(daemon=True)
        self.url = url
        self.out_dir = out_dir
        self.segment_seconds = segment_seconds
        self.t0 = t0
        self.parts: list[dict] = []  # {"csv": Path, "wall": seconds since t0}
        self.proc: subprocess.Popen | None = None
        self.stopping = threading.Event()
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                try:
                    info = resolve_stream(self.url)
                except Exception as e:
                    print(f"[live] stream unavailable: {e}")
                    break
                n = len(self.parts)
                part_csv = self.out_dir / f"part{n}.csv"
                cmd = [
                    "ffmpeg", "-loglevel", "error", "-y", *_input_args(info), "-c", "copy",
                    "-f", "segment", "-segment_time", str(self.segment_seconds),
                    "-segment_format", "mpegts", "-reset_timestamps", "1",
                    "-segment_list", str(part_csv), "-segment_list_type", "csv",
                    str(self.out_dir / f"part{n}_%05d.ts"),
                ]
                self.parts.append({"csv": part_csv, "wall": time.time() - self.t0})
                print(f"[live] recording part {n} ({info.get('height', '?')}p)")
                self.proc = subprocess.Popen(cmd)
                self.proc.wait()
                time.sleep(2)
                if not self.stopping.is_set():
                    print("[live] recorder dropped; reconnecting...")
        finally:
            self.finished.set()

    def stop(self) -> None:
        self.stopping.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def segments(self) -> list[dict]:
        """All completed segments, in order, with absolute (session) times."""
        out = []
        for p_idx, part in enumerate(self.parts):
            if not part["csv"].exists():
                continue
            with open(part["csv"], newline="") as f:
                for row in csv.reader(f):
                    if len(row) < 3:
                        continue
                    start, end = float(row[1]), float(row[2])
                    out.append({
                        "file": self.out_dir / row[0],
                        "part": p_idx,
                        "duration": end - start,
                        "abs_start": part["wall"] + start,
                    })
        return out


def _concat(files: list[Path], out: Path) -> None:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", "-movflags", "+faststart", str(out)],
        check=True,
    )
    lst.unlink(missing_ok=True)


def run_live(url: str, args) -> None:
    from .cli import process

    t0 = time.time()
    info = resolve_stream(url)
    channel = info.get("uploader_id") or info.get("uploader") or info.get("id", "stream")
    session = f"{re.sub(r'[^A-Za-z0-9]+', '-', channel).strip('-')}-live-{time.strftime('%Y%m%d-%H%M')}"
    rec_dir = WORK / "live" / session
    rec_dir.mkdir(parents=True, exist_ok=True)
    print(f"[live] session {session}: {info.get('title', '')}")

    chat = None
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
    if m:
        chat = TwitchChat(m.group(1), t0)
        chat.start()
        print(f"[live] reading Twitch chat #{m.group(1)}")

    recorder = Recorder(url, rec_dir, args.segment_minutes * 60, t0)
    recorder.start()

    done_files: set[Path] = set()
    produced: list[tuple[float, float]] = []
    words_cache: dict[Path, list[dict]] = {}
    prev: dict | None = None

    def handle(seg: dict) -> None:
        nonlocal prev
        seg_words = transcribe(seg["file"], cache_dir=rec_dir / "transcripts")
        words_cache[seg["file"]] = seg_words

        window = [seg]
        if prev and prev["part"] == seg["part"]:
            window = [prev, seg]
        win_start = window[0]["abs_start"]
        words, offset = [], 0.0
        for s in window:
            words += [{**w, "start": w["start"] + offset, "end": w["end"] + offset}
                      for w in words_cache.get(s["file"], [])]
            offset += s["duration"]

        idx = len(done_files)
        win_file = rec_dir / f"window_{idx:04d}.mp4"
        _concat([s["file"] for s in window], win_file)

        src = Source(
            path=win_file,
            id=f"{session}_{idx}",
            title=session,
            url=url,
            chat_times=[t - win_start for t in (chat.times if chat else []) if win_start <= t <= win_start + offset + 15],
            clip_prefix=f"{idx:04d}_",
        )
        exclude = [(a - win_start, b - win_start) for a, b in produced]
        print(f"\n[live] analysing {offset / 60:.1f} min window (segment {idx})")
        clips = process(src, args, words=words, exclude=exclude)
        produced.extend((c["start"] + win_start, c["end"] + win_start) for c in clips)

        win_file.unlink(missing_ok=True)
        if prev and not args.keep_recording:
            # The previous segment is no longer needed as context after this window.
            for s in recorder.segments():
                if s["file"] in done_files and s["file"] != seg["file"]:
                    s["file"].unlink(missing_ok=True)
        prev = seg

    def drain() -> None:
        for seg in recorder.segments():
            if seg["file"] in done_files or not seg["file"].exists():
                continue
            try:
                handle(seg)
            except Exception as e:
                print(f"[live] segment failed: {e}")
            done_files.add(seg["file"])

    try:
        while True:
            drain()
            if recorder.finished.is_set():
                drain()
                print("[live] stream ended")
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[live] stopping; processing the last finished segment...")
        recorder.stop()
        time.sleep(1)
        drain()
    finally:
        recorder.stop()
        if chat:
            chat.stopped.set()
        if not args.keep_recording:
            shutil.rmtree(rec_dir, ignore_errors=True)
        print(f"[live] session finished: {len(produced)} clip(s)")
