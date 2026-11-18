"""Non-secret settings shared by the CLI, the daemon and the UI. Secrets live in .env."""

import json
from pathlib import Path

CONFIG_PATH = Path("work/config.json")
ENV_PATH = Path(".env")

DEFAULTS = {
    "count": 5,
    "min_len": 20,
    "max_len": 59,
    "min_score": 60,
    "layout": "auto",
    "platforms": [],
    "schedule": "12:00,19:00",
    "privacy": "private",
    "captions": True,
    "hook": True,
    "segment_minutes": 5,
}


def load() -> dict:
    data = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    return {
        "defaults": {**DEFAULTS, **data.get("defaults", {})},
        "channels": data.get("channels", []),
        "watch_every_minutes": data.get("watch_every_minutes", 60),
    }


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def options_argv(opts: dict, use_schedule: bool = True) -> list[str]:
    """Turn a defaults/options dict into CLI flags for auto/watch/live."""
    argv = [
        "--count", str(opts["count"]),
        "--min-len", str(opts["min_len"]),
        "--max-len", str(opts["max_len"]),
        "--min-score", str(opts["min_score"]),
        "--layout", opts["layout"],
        "--privacy", opts["privacy"],
    ]
    if opts.get("platforms"):
        argv += ["--upload", ",".join(opts["platforms"])]
        if use_schedule and opts.get("schedule"):
            argv += ["--schedule", opts["schedule"]]
    if not opts.get("captions", True):
        argv.append("--no-captions")
    if not opts.get("hook", True):
        argv.append("--no-hook")
    return argv
