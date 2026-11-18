"""Entry point for UI background jobs: runs a clipper command and records how it ended."""

import signal
import sys
import time
import traceback

from .cli import main
from .jobs import update_meta


def _terminate(*_):
    raise KeyboardInterrupt


def run() -> None:
    job_id, argv = sys.argv[1], sys.argv[2:]
    signal.signal(signal.SIGTERM, _terminate)
    status = "done"
    try:
        main(argv)
    except KeyboardInterrupt:
        status = "stopped"
        print("\n[job] stopped")
    except SystemExit as e:
        if e.code not in (0, None):
            status = "failed"
            print(f"\n[job] {e.code}")
    except Exception:
        traceback.print_exc()
        status = "failed"
    finally:
        update_meta(job_id, status=status, ended=time.strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    run()
