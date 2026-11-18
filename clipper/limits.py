"""Cross-process cap on CPU/GPU-heavy steps (transcription, rendering).

Many live sessions and batch jobs run as separate processes; without a cap they all transcribe
and encode at once and every one of them slows down.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path("work/locks")


@contextmanager
def heavy_slot(name: str = ""):
    slots = max(1, int(os.getenv("MAX_PARALLEL_HEAVY", "3")))
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    waited = False
    while True:
        for i in range(slots):
            f = open(LOCK_DIR / f"slot{i}.lock", "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
            return
        if not waited:
            print(f"[limits] waiting for a free processing slot ({name})")
            waited = True
        time.sleep(1)
