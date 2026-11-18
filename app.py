"""Clipper control panel. Run with: .venv/bin/streamlit run app.py"""

import hashlib
import json
import os
from pathlib import Path

import streamlit as st
from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

from clipper import config, jobs, scheduler  # noqa: E402

ENV = ROOT / ".env"
OUTPUT = ROOT / "output"
LAYOUTS = ["auto", "stream", "track", "blur", "crop"]
LAYOUT_HELP = ("auto picks per clip: stream = facecam over gameplay, track = follow the speaker, "
               "blur = full frame on blurred background, crop = plain center crop")
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3"]
NON_CHAT = ("embedding", "bge", "e5-", "gte-", "image", "tts", "stable-diffusion", "wan2",
            "reranker", "mpnet", "router:")
STATUS_COLOR = {"running": "orange", "stopping": "orange", "done": "green",
                "failed": "red", "died": "red", "stopped": "gray"}

st.set_page_config(page_title="Clipper", layout="wide")


# ---------- helpers ----------

def env() -> dict:
    return dotenv_values(ENV) if ENV.exists() else {}


def save_env(key: str, value: str) -> None:
    ENV.touch(exist_ok=True)
    set_key(str(ENV), key, value)
    os.environ[key] = value


def ai_key() -> str | None:
    e = env()
    return e.get("GRADIENT_API_KEY") or e.get("AI_KEY")


def yt_secret() -> Path:
    return ROOT / env().get("YOUTUBE_CLIENT_SECRET", "client_secret.json")


def yt_token() -> Path:
    return ROOT / env().get("YOUTUBE_TOKEN", "token.json")


def ig_ready() -> bool:
    e = env()
    return bool(e.get("IG_USER_ID") and e.get("IG_ACCESS_TOKEN"))


def status_text(status: str) -> str:
    return f":{STATUS_COLOR.get(status, 'gray')}[**{status}**]"


def show_log(job_id: str, live: bool, height: int = 320) -> None:
    if live:
        @st.fragment(run_every=3)
        def _refresh():
            meta = jobs.read_meta(job_id)
            st.markdown(f"Status: {status_text(meta['status'])}")
            st.code(jobs.log_tail(job_id)[-12000:] or "(starting...)", language=None, height=height)
        _refresh()
    else:
        st.code(jobs.log_tail(job_id)[-12000:] or "(empty log)", language=None, height=height)


def options_form(prefix: str, d: dict, live: bool = False) -> dict:
    """Clip + posting options, pre-filled with the saved defaults."""
    opts = dict(d)
    c1, c2, c3, c4 = st.columns(4)
    opts["count"] = c1.number_input("Max clips" + (" per segment" if live else " per video"),
                                    1, 30, int(d["count"]), key=f"{prefix}_count")
    opts["min_score"] = c2.number_input("Min AI score (0-100)", 0, 100, int(d["min_score"]),
                                        key=f"{prefix}_score",
                                        help="Clips the AI rates below this are skipped. 60+ keeps quality high.")
    opts["min_len"] = c3.number_input("Min length (s)", 5, 170, int(d["min_len"]), key=f"{prefix}_minlen")
    opts["max_len"] = c4.number_input("Max length (s)", 10, 180, int(d["max_len"]), key=f"{prefix}_maxlen",
                                      help="Shorts must be under 60s (up to 3 min is allowed but reaches fewer people).")
    c1, c2, c3 = st.columns([1, 2, 1])
    opts["layout"] = c1.selectbox("Layout", LAYOUTS, LAYOUTS.index(d["layout"]), key=f"{prefix}_layout",
                                  help=LAYOUT_HELP)
    opts["captions"] = c3.checkbox("Captions", d["captions"], key=f"{prefix}_caps")
    opts["hook"] = c3.checkbox("Hook text (first 3s)", d["hook"], key=f"{prefix}_hook")

    post_modes = ["Don't post (review in Clips first)", "Post immediately", "Schedule into time slots"]
    default_mode = 0 if not d["platforms"] else (2 if d.get("schedule") else 1)
    mode = c2.radio("Posting", post_modes, default_mode, key=f"{prefix}_mode")
    if mode != post_modes[0]:
        c1, c2, c3 = st.columns(3)
        opts["platforms"] = c1.multiselect("Platforms", ["youtube", "instagram"],
                                           d["platforms"] or ["youtube"], key=f"{prefix}_plat")
        opts["privacy"] = c2.selectbox("YouTube visibility", ["private", "unlisted", "public"],
                                       ["private", "unlisted", "public"].index(d["privacy"]),
                                       key=f"{prefix}_priv")
        if mode == post_modes[2]:
            opts["schedule"] = c3.text_input("Daily slots (HH:MM, comma separated)",
                                             d.get("schedule") or "12:00,19:00", key=f"{prefix}_slots")
        else:
            opts["schedule"] = ""
    else:
        opts["platforms"] = []
    return opts


def manifests() -> list[Path]:
    return sorted(OUTPUT.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def update_manifest(path: Path, file: str, new: dict | None) -> None:
    clips = json.loads(path.read_text())
    out = []
    for c in clips:
        if c["file"] == file:
            if new is None:
                continue
            c = new
        out.append(c)
    path.write_text(json.dumps(out, indent=2))


def need_ai_key() -> bool:
    if not ai_key():
        st.warning("Add your DigitalOcean Gradient key in **Settings → AI** first.")
        return True
    return False


# ---------- pages ----------

def page_create():
    st.title("Create clips")
    st.caption("Paste YouTube or Twitch VOD/video links, one per line. They are processed one after another. "
               "For streams that are live right now, use the **Live** page.")
    cfg = config.load()
    links = st.text_area("Links", height=140, placeholder=(
        "https://www.youtube.com/watch?v=...\nhttps://www.twitch.tv/videos/123456789"))
    with st.expander("Clip & posting options", expanded=True):
        opts = options_form("create", cfg["defaults"])
    dry = st.checkbox("Preview only: show what the AI would pick, without rendering or posting")

    if st.button("Start", type="primary", disabled=need_ai_key()):
        urls = [l.strip() for l in links.splitlines() if l.strip()]
        if not urls:
            st.error("Paste at least one link.")
        else:
            argv = ["auto", *urls, *config.options_argv(opts)] + (["--dry-run"] if dry else [])
            label = urls[0] if len(urls) == 1 else f"{len(urls)} links"
            st.session_state["create_job"] = jobs.start("auto", argv, label + (" (preview)" if dry else ""))

    job_id = st.session_state.get("create_job")
    if job_id and (jobs.JOBS_DIR / job_id).exists():
        st.subheader("Progress")
        meta = jobs.read_meta(job_id)
        show_log(job_id, live=meta["status"] in ("running", "stopping"))
        if meta["status"] == "running" and st.button("Stop this job"):
            jobs.stop(job_id)
            st.rerun()


def page_live():
    st.title("Live clipping")
    st.caption("Records a live Twitch or YouTube stream in segments and clips each one as soon as it finishes, "
               "so clips can go out while the stream is still running. Twitch chat activity is used as a hype signal.")
    cfg = config.load()
    url = st.text_input("Live stream URL",
                        placeholder="https://www.twitch.tv/CHANNEL  or  https://www.youtube.com/watch?v=LIVE_ID")
    c1, c2 = st.columns(2)
    seg = c1.slider("Segment length (minutes)", 2, 15, int(cfg["defaults"]["segment_minutes"]),
                    help="Shorter = clips come out faster; each segment is analysed together with the previous one.")
    keep = c2.checkbox("Keep the raw recording on disk", help="Uses several GB per hour of stream.")
    with st.expander("Clip & posting options", expanded=True):
        opts = options_form("live", cfg["defaults"], live=True)

    if st.button("Start live session", type="primary", disabled=need_ai_key()):
        if not url.strip():
            st.error("Enter a live stream URL.")
        else:
            argv = ["live", url.strip(), "--segment-minutes", str(seg), *config.options_argv(opts)]
            if keep:
                argv.append("--keep-recording")
            jobs.start("live", argv, url.strip())
            st.rerun()

    sessions = jobs.running("live")
    st.subheader(f"Active sessions ({len(sessions)})")
    if not sessions:
        st.info("No live sessions running.")
    for j in sessions:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            c1.markdown(f"**{j['label']}** · started {j['started']}")
            if c2.button("Stop", key=f"stop_{j['id']}", type="primary"):
                jobs.stop(j["id"])
                st.rerun()
            show_log(j["id"], live=True, height=260)


def page_jobs():
    st.title("Jobs")

    @st.fragment(run_every=5)
    def _list():
        all_jobs = jobs.list_jobs()
        if not all_jobs:
            st.info("No jobs yet.")
            return
        for j in all_jobs[:40]:
            active = j["status"] in ("running", "stopping")
            header = f"{j['kind']} · {j['label']} · {j['started']} · {j['status']}"
            with st.expander(header, expanded=active and j["kind"] != "daemon"):
                st.markdown(f"Status: {status_text(j['status'])}")
                st.caption("clipper " + " ".join(j["argv"]))
                st.code(jobs.log_tail(j["id"])[-10000:] or "(empty log)", language=None, height=260)
                c1, c2, c3, _ = st.columns([1, 1, 1, 4])
                if active and c1.button("Stop", key=f"js_{j['id']}"):
                    jobs.stop(j["id"])
                    st.rerun()
                if active and c2.button("Force kill", key=f"jk_{j['id']}"):
                    jobs.kill(j["id"])
                    st.rerun()
                if not active and c3.button("Delete", key=f"jd_{j['id']}"):
                    jobs.delete(j["id"])
                    st.rerun()
    _list()


def page_clips():
    st.title("Clips")
    paths = manifests()
    if not paths:
        st.info("No clips yet. Create some on the **Create clips** or **Live** page.")
        return
    cfg = config.load()
    names = [p.parent.name for p in paths]
    choice = st.selectbox("Source", ["All"] + names)
    selected = paths if choice == "All" else [paths[names.index(choice)]]

    for mpath in selected:
        clips = [c for c in json.loads(mpath.read_text()) if Path(c["file"]).exists()]
        if not clips:
            continue
        st.subheader(mpath.parent.name)
        cols = st.columns(3)
        for i, clip in enumerate(sorted(clips, key=lambda c: c.get("score", 0), reverse=True)):
            key = hashlib.md5(clip["file"].encode()).hexdigest()[:10]
            with cols[i % 3].container(border=True):
                st.video(clip["file"])
                st.markdown(f"**{clip['title']}**")
                info = [f"score {clip.get('score', '?')}", f"{clip['end'] - clip['start']:.0f}s",
                        clip.get("layout", "")]
                if clip.get("scheduled"):
                    info.append(f"scheduled {clip['scheduled'][:16].replace('T', ' ')}")
                if clip.get("published"):
                    info.append("posted: " + ", ".join(clip["published"].keys()))
                st.caption(" · ".join(x for x in info if x))
                with st.expander("Edit & post"):
                    title = st.text_input("Title", clip["title"], key=f"t_{key}", max_chars=90)
                    desc = st.text_area("Description", clip.get("description", ""), key=f"d_{key}", height=80)
                    tags = st.text_input("Tags (comma separated)", ", ".join(clip.get("tags", [])), key=f"g_{key}")
                    plats = st.multiselect("Platforms", ["youtube", "instagram"],
                                           cfg["defaults"]["platforms"] or ["youtube"], key=f"p_{key}")
                    edited = {**clip, "title": title, "description": desc,
                              "tags": [t.strip() for t in tags.split(",") if t.strip()]}
                    b1, b2, b3 = st.columns(3)
                    if b1.button("Post now", key=f"pn_{key}", disabled=not plats):
                        update_manifest(mpath, clip["file"], {**edited, "published": {p: "requested" for p in plats}})
                        jobs.start("upload", [
                            "upload", clip["file"], "--title", title, "--description", desc,
                            "--tags", ",".join(edited["tags"]), "--upload", ",".join(plats),
                            "--privacy", cfg["defaults"]["privacy"],
                        ], f"post: {title}")
                        st.success("Upload started; see the Jobs page.")
                    if b2.button("Queue", key=f"q_{key}", disabled=not plats):
                        scheduler.enqueue([edited], plats, cfg["defaults"]["schedule"] or "12:00,19:00")
                        update_manifest(mpath, clip["file"], edited)
                        st.rerun()
                    if b3.button("Delete", key=f"x_{key}"):
                        Path(clip["file"]).unlink(missing_ok=True)
                        update_manifest(mpath, clip["file"], None)
                        st.rerun()


def page_queue():
    st.title("Posting queue")
    cfg = config.load()
    daemon = jobs.running("daemon")
    if not daemon:
        st.warning("Autopilot is off, so scheduled posts won't go out on their own. "
                   "Start it on the **Autopilot** page, or use the button below.")
    if st.button("Post everything that's due now", type="primary"):
        jobs.start("queue", ["queue", "run", "--privacy", cfg["defaults"]["privacy"]], "post due clips")
        st.success("Started; see the Jobs page.")

    items = sorted(scheduler.load(), key=lambda i: i["publish_at"])
    if not items:
        st.info("The queue is empty.")
    for item in items:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 4, 1, 1])
            c1.markdown(f"**{item['publish_at'][:16].replace('T', ' ')}**")
            states = " · ".join(f"{p}: {status_text(s['status'])}" for p, s in item["platforms"].items())
            errors = [s.get("error") for s in item["platforms"].values() if s.get("error")]
            c2.markdown(f"{item['title']}  \n{states}")
            if errors:
                c2.caption("Last error: " + errors[-1][:200])
            if c3.button("Post now", key=f"qn_{item['id']}"):
                scheduler.make_due_now(item["id"])
                jobs.start("queue", ["queue", "run", "--privacy", cfg["defaults"]["privacy"]], f"post: {item['title']}")
                st.rerun()
            if c4.button("Remove", key=f"qr_{item['id']}"):
                scheduler.remove(item["id"])
                st.rerun()


def page_autopilot():
    st.title("Autopilot")
    st.caption("Autopilot runs in the background: every few minutes it posts queued clips that are due, and it "
               "checks your channels for new VODs/videos, clips them with your default settings, then posts or "
               "queues them. Your Mac must stay awake; it stops on restart until you start it again.")
    cfg = config.load()
    channels = st.text_area(
        "Channels to watch (one per line)", "\n".join(cfg["channels"]), height=120,
        placeholder="https://www.twitch.tv/CHANNEL/videos?filter=archives\nhttps://www.youtube.com/@CHANNEL/videos")
    every = st.number_input("Check channels every (minutes)", 10, 1440, int(cfg["watch_every_minutes"]))
    if st.button("Save channels"):
        cfg["channels"] = [c.strip() for c in channels.splitlines() if c.strip()]
        cfg["watch_every_minutes"] = int(every)
        config.save(cfg)
        st.success("Saved. The running autopilot picks this up on its next cycle.")

    st.divider()
    running = jobs.running("daemon")
    if running:
        j = running[0]
        st.markdown(f"Autopilot is :green[**running**] since {j['started']}.")
        if st.button("Stop autopilot", type="primary"):
            jobs.stop(j["id"])
            st.rerun()
        show_log(j["id"], live=True, height=300)
    else:
        st.markdown("Autopilot is :gray[**off**].")
        d = cfg["defaults"]
        if not d["platforms"]:
            st.info("Default posting is set to *Don't post*, so autopilot will only create clips. "
                    "Change it in **Settings → Defaults** to post automatically.")
        if st.button("Start autopilot", type="primary", disabled=need_ai_key()):
            jobs.start("daemon", ["daemon"], "autopilot")
            st.rerun()


def page_settings():
    st.title("Settings")
    tab_ai, tab_yt, tab_ig, tab_def = st.tabs(["AI", "YouTube", "Instagram", "Defaults"])
    e = env()

    with tab_ai:
        st.markdown("Highlight detection uses **DigitalOcean Gradient** serverless inference. "
                    "Only transcript text is sent, never video.")
        current = ai_key()
        st.caption(f"Current key: {'set (…' + current[-4:] + ')' if current else 'not set'}")
        new_key = st.text_input("Gradient model access key", type="password",
                                placeholder="leave empty to keep the current key")
        models = st.session_state.get("models", [])
        current_model = e.get("LLM_MODEL", "anthropic-claude-sonnet-5.5")
        if st.button("Test key & load models"):
            try:
                from openai import OpenAI

                client = OpenAI(base_url="https://inference.do-ai.run/v1/", api_key=new_key or current)
                models = sorted(m.id for m in client.models.list().data if not any(x in m.id for x in NON_CHAT))
                st.session_state["models"] = models
                st.success(f"Key works: {len(models)} chat models available.")
            except Exception as ex:
                st.error(f"Key check failed: {ex}")
        options = models or [current_model]
        if current_model not in options:
            options = [current_model] + options
        model = st.selectbox("Model", options, options.index(current_model),
                             help="anthropic-claude-sonnet-5.5 picks the best clips; deepseek-v4.1-flash is much cheaper.")
        wm = e.get("WHISPER_MODEL", "small")
        whisper = st.selectbox("Transcription model (runs locally)", WHISPER_MODELS, WHISPER_MODELS.index(wm),
                               help="small is a good balance on a Mac; medium/large are more accurate but slower.")
        if st.button("Save AI settings", type="primary"):
            if new_key:
                save_env("AI_KEY", new_key)
            save_env("LLM_PROVIDER", "gradient")
            save_env("LLM_MODEL", model)
            save_env("WHISPER_MODEL", whisper)
            st.success("Saved.")

    with tab_yt:
        st.markdown(
            "YouTube uploads need an **OAuth client** (a plain API key can't upload videos):\n"
            "1. Open [Google Cloud Console](https://console.cloud.google.com/), create a project.\n"
            "2. **APIs & Services → Library** → enable **YouTube Data API v3**.\n"
            "3. **OAuth consent screen** → External → add your Google account as a **test user**.\n"
            "4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON.\n"
            "5. Upload it below, then click **Connect YouTube** and approve in the browser window."
        )
        secret = yt_secret()
        st.caption(f"Client secret: {'uploaded' if secret.exists() else 'missing'} · "
                   f"Channel: {'connected' if yt_token().exists() else 'not connected'}")
        up = st.file_uploader("client_secret.json", type="json")
        if up is not None:
            try:
                data = json.loads(up.getvalue())
                if "installed" not in data:
                    st.error("This isn't a Desktop app OAuth client. Create one with type **Desktop app**.")
                else:
                    secret.write_bytes(up.getvalue())
                    st.success("Saved client_secret.json.")
            except json.JSONDecodeError:
                st.error("Not a valid JSON file.")
        c1, c2 = st.columns(2)
        if c1.button("Connect YouTube", type="primary", disabled=not secret.exists()):
            st.session_state["auth_job"] = jobs.start("auth", ["auth"], "connect YouTube")
            st.info("A browser window opens for Google sign-in. Approve access, then come back here.")
        if c2.button("Disconnect", disabled=not yt_token().exists()):
            yt_token().unlink(missing_ok=True)
            st.rerun()
        if st.session_state.get("auth_job"):
            job = st.session_state["auth_job"]
            show_log(job, live=jobs.read_meta(job)["status"] == "running", height=120)
        st.info("Until Google verifies your app, YouTube forces API uploads to **private**. "
                "You can make them public in YouTube Studio, or submit the app for verification.")

    with tab_ig:
        st.markdown(
            "Instagram Reels publishing needs an **Instagram Business or Creator account** linked to a "
            "Facebook Page, and a Meta app with the `instagram_content_publish` permission. "
            "Generate a long-lived access token in the [Meta developer dashboard](https://developers.facebook.com/apps/)."
        )
        st.caption(f"Status: {'configured' if ig_ready() else 'not configured'}")
        ig_user = st.text_input("Instagram user ID", e.get("IG_USER_ID", ""))
        ig_token = st.text_input("Access token", type="password", placeholder="leave empty to keep the current token")
        if st.button("Save Instagram settings", type="primary"):
            save_env("IG_USER_ID", ig_user.strip())
            if ig_token:
                save_env("IG_ACCESS_TOKEN", ig_token.strip())
            st.success("Saved.")

    with tab_def:
        st.markdown("Defaults are pre-filled on every page and used by **Autopilot**.")
        cfg = config.load()
        opts = options_form("defaults", cfg["defaults"])
        opts["segment_minutes"] = st.slider("Live segment length (minutes)", 2, 15,
                                            int(cfg["defaults"]["segment_minutes"]))
        if st.button("Save defaults", type="primary"):
            cfg["defaults"] = opts
            config.save(cfg)
            st.success("Saved.")


def sidebar():
    with st.sidebar:
        st.markdown("### Status")
        items = [
            ("AI key", bool(ai_key())),
            ("YouTube", yt_token().exists()),
            ("Instagram", ig_ready()),
            ("Autopilot", bool(jobs.running("daemon"))),
        ]
        for name, ok in items:
            st.markdown(f"{name}: {':green[on]' if ok else ':gray[off]'}")
        active = jobs.running()
        st.markdown(f"Running jobs: **{len([j for j in active if j['kind'] != 'daemon'])}**")
        queued = [i for i in scheduler.load()
                  if any(s["status"] != "done" for s in i["platforms"].values())]
        st.markdown(f"Queued posts: **{len(queued)}**")


pages = [
    st.Page(page_create, title="Create clips", icon=":material/content_cut:", default=True),
    st.Page(page_live, title="Live", icon=":material/sensors:"),
    st.Page(page_jobs, title="Jobs", icon=":material/list_alt:"),
    st.Page(page_clips, title="Clips", icon=":material/video_library:"),
    st.Page(page_queue, title="Queue", icon=":material/schedule_send:"),
    st.Page(page_autopilot, title="Autopilot", icon=":material/autorenew:"),
    st.Page(page_settings, title="Settings", icon=":material/settings:"),
]
nav = st.navigation(pages)
sidebar()
nav.run()
