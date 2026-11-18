"""Background jobs for the UI: each job is a detached `python -m clipper.jobrun` process with its own log."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

JOBS_DIR = Path("work/jobs")


def _meta_path(job_id: str) -> Path:
    return JOBS_DIR / job_id / "meta.json"


def read_meta(job_id: str) -> dict:
    return json.loads(_meta_path(job_id).read_text())


def update_meta(job_id: str, **fields) -> None:
    meta = read_meta(job_id)
    meta.update(fields)
    _meta_path(job_id).write_text(json.dumps(meta, indent=2))


def start(kind: str, argv: list[str], label: str) -> str:
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    meta = {"id": job_id, "kind": kind, "label": label, "argv": argv,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "running"}
    _meta_path(job_id).write_text(json.dumps(meta, indent=2))
    log = open(job_dir / "log.txt", "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "clipper.jobrun", job_id, *argv],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=Path.cwd(),
    )
    update_meta(job_id, pid=proc.pid)
    return job_id


def _alive(pid: int) -> bool:
    try:
        # Reap the process if it is our finished child, so it doesn't linger as a zombie.
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def list_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for d in sorted(JOBS_DIR.iterdir(), reverse=True):
        if not (d / "meta.json").exists():
            continue
        meta = json.loads((d / "meta.json").read_text())
        if meta.get("status") in ("running", "stopping") and meta.get("pid") and not _alive(meta["pid"]):
            # jobrun writes the final status itself; if it couldn't, the process died hard.
            meta = read_meta(meta["id"])
            if meta.get("status") in ("running", "stopping"):
                meta["status"] = "died"
                update_meta(meta["id"], status="died")
        jobs.append(meta)
    return jobs


def running(kind: str | None = None) -> list[dict]:
    return [j for j in list_jobs() if j["status"] in ("running", "stopping") and (kind is None or j["kind"] == kind)]


def stop(job_id: str) -> None:
    meta = read_meta(job_id)
    pid = meta.get("pid")
    if pid and _alive(pid):
        try:
            os.killpg(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        update_meta(job_id, status="stopping")


def kill(job_id: str) -> None:
    pid = read_meta(job_id).get("pid")
    if pid:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    update_meta(job_id, status="stopped")


def delete(job_id: str) -> None:
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)


def log_tail(job_id: str, max_bytes: int = 30000) -> str:
    path = JOBS_DIR / job_id / "log.txt"
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        text = f.read().decode(errors="ignore")
    # Progress bars rewrite the line with \r; keep only the latest state of each line.
    return "\n".join(line.split("\r")[-1] for line in text.split("\n"))
