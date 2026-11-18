"""Local posting queue: spread clips over daily time slots and post them when due."""

import json
import traceback
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path

QUEUE_PATH = Path("work/queue.json")
MAX_ATTEMPTS = 3


def load() -> list[dict]:
    return json.loads(QUEUE_PATH.read_text()) if QUEUE_PATH.exists() else []


def save(items: list[dict]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(items, indent=2))


def next_slots(slots: str, count: int, taken: set[str]) -> list[datetime]:
    """Next `count` free datetimes (local time) matching daily HH:MM slots."""
    times = sorted(time.fromisoformat(s.strip()) for s in slots.split(",") if s.strip())
    now = datetime.now().replace(second=0, microsecond=0)
    out, day = [], now.date()
    while len(out) < count:
        for t in times:
            dt = datetime.combine(day, t)
            if dt > now and dt.isoformat() not in taken:
                out.append(dt)
                if len(out) == count:
                    break
        day += timedelta(days=1)
    return out


def enqueue(clips: list[dict], platforms: list[str], slots: str) -> list[dict]:
    items = load()
    taken = {i["publish_at"] for i in items}
    for clip, when in zip(clips, next_slots(slots, len(clips), taken)):
        item = {
            "id": uuid.uuid4().hex[:8],
            "file": str(Path(clip["file"]).resolve()),
            "title": clip["title"],
            "description": clip.get("description", ""),
            "tags": clip.get("tags", []),
            "publish_at": when.isoformat(),
            "platforms": {p: {"status": "pending", "attempts": 0} for p in platforms},
        }
        items.append(item)
        clip["scheduled"] = item["publish_at"]
        print(f"[queue] {when:%a %d %b %H:%M} -> {', '.join(platforms)}: {clip['title']}")
    save(items)
    return items


def remove(item_id: str) -> None:
    save([i for i in load() if i["id"] != item_id])


def make_due_now(item_id: str) -> None:
    items = load()
    for i in items:
        if i["id"] == item_id:
            i["publish_at"] = datetime.now().replace(second=0, microsecond=0).isoformat()
            for state in i["platforms"].values():
                if state["status"] != "done":
                    state.update(status="pending", attempts=0)
    save(items)


def run_due(privacy: str = "private") -> None:
    from .publish import instagram_caption, upload_instagram, upload_youtube

    items = load()
    now = datetime.now()
    due = [i for i in items if datetime.fromisoformat(i["publish_at"]) <= now]
    for item in due:
        for platform, state in item["platforms"].items():
            if state["status"] == "done" or state["attempts"] >= MAX_ATTEMPTS:
                continue
            state["attempts"] += 1
            try:
                path = Path(item["file"])
                if platform == "youtube":
                    state["result"] = upload_youtube(
                        path, item["title"], item["description"], item["tags"], privacy=privacy
                    )
                elif platform == "instagram":
                    state["result"] = upload_instagram(path, instagram_caption(item))
                else:
                    raise ValueError(f"unknown platform {platform}")
                state["status"] = "done"
            except Exception as e:
                state["status"] = "failed"
                state["error"] = str(e)
                traceback.print_exc()
            save(items)
    pending = sum(
        1 for i in items for s in i["platforms"].values()
        if s["status"] != "done" and s["attempts"] < MAX_ATTEMPTS
    )
    print(f"[queue] processed {len(due)} due item(s); {pending} post(s) still pending")


def show() -> None:
    items = load()
    if not items:
        print("Queue is empty.")
    for i in sorted(items, key=lambda x: x["publish_at"]):
        states = ", ".join(f"{p}:{s['status']}" for p, s in i["platforms"].items())
        print(f"{i['publish_at'][:16]}  [{states}]  {i['title']}")
