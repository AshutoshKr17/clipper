"""Face-aware 9:16 reframing: picks a layout per clip and builds the ffmpeg filtergraph."""

import statistics
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

OUT_W, OUT_H = 1080, 1920
CAM_H = 700  # top panel height for the "stream" (facecam over gameplay) layout
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
YUNET_PATH = Path.home() / ".cache" / "clipper" / "face_detection_yunet_2023mar.onnx"


@dataclass
class Plan:
    layout: str  # "blur" | "track" | "stream" | "crop"
    src_w: int
    src_h: int
    track: list[tuple[float, float]] = field(default_factory=list)  # (t, crop x) for "track"
    cam_box: tuple[int, int, int, int] | None = None  # x, y, w, h for "stream"
    crop_x: float = 0.5

    @property
    def caption_y(self) -> float:
        return (CAM_H / OUT_H) if self.layout == "stream" else 0.70

    @property
    def hook_y(self) -> float:
        return 0.42 if self.layout == "stream" else 0.15


def _detector(w: int, h: int):
    import cv2

    if not YUNET_PATH.exists():
        YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
        print("[reframe] downloading face model...")
        urllib.request.urlretrieve(YUNET_URL, YUNET_PATH)
    return cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (w, h), 0.75, 0.3, 50)


def sample_faces(source: Path, start: float, end: float, fps: float = 3.0):
    """Return (src_w, src_h, [(t_rel, [(x, y, w, h, score), ...]), ...]) in source pixels."""
    import cv2

    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    cap = cv2.VideoCapture(str(source))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # Facecam overlays are small, so detect at a fairly high resolution.
    dw = min(src_w, 1280)
    scale = dw / src_w
    dh = int(src_h * scale)
    det = _detector(dw, dh)

    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    samples, next_t = [], 0.0
    while True:
        if not cap.grab():
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 - start
        if t > end - start:
            break
        if t < next_t:
            continue
        next_t = t + 1 / fps
        ok, frame = cap.retrieve()
        if not ok:
            break
        _, faces = det.detect(cv2.resize(frame, (dw, dh)))
        boxes = []
        for f in faces if faces is not None else []:
            x, y, w, h = (float(v) / scale for v in f[:4])
            boxes.append((x, y, w, h, float(f[-1])))
        samples.append((max(0.0, t), boxes))
    cap.release()
    return src_w, src_h, samples


def _smooth_track(points: list[tuple[float, float | None]], src_w: int, crop_w: int) -> list[tuple[float, float]]:
    # Fill gaps by holding the last known position (or the first known one at the start).
    known = [c for _, c in points if c is not None]
    if not known:
        return []
    last = known[0]
    filled = []
    for t, c in points:
        last = c if c is not None else last
        filled.append((t, last))

    # Median filter kills detector jitter, then a dead-zone camera only moves for real shifts.
    centers = [c for _, c in filled]
    med = [statistics.median(centers[max(0, i - 2): i + 3]) for i in range(len(centers))]
    cam, out = med[0], []
    dead_zone = crop_w * 0.12
    for (t, _), target in zip(filled, med):
        if abs(target - cam) > dead_zone:
            cam += (target - cam) * 0.35
        x = min(max(cam - crop_w / 2, 0), src_w - crop_w)
        out.append((t, x))
    return out


def plan_clip(source: Path, start: float, end: float, layout: str = "auto", crop_x: float = 0.5) -> Plan:
    if layout in ("blur", "crop"):
        from .render import probe_size

        w, h = probe_size(source)
        return Plan(layout, w, h, crop_x=crop_x)

    src_w, src_h, samples = sample_faces(source, start, end)
    primary = [(t, max(b, key=lambda f: f[2] * f[3]) if b else None) for t, b in samples]
    with_face = [f for _, f in primary if f]
    face_ratio = len(with_face) / max(1, len(primary))

    if layout == "auto" and face_ratio < 0.4:
        print(f"[reframe] faces in {face_ratio:.0%} of frames -> blur layout")
        return Plan("blur", src_w, src_h)
    if not with_face:
        return Plan("blur", src_w, src_h)

    rel_w = statistics.median(f[2] for f in with_face) / src_w
    cxs = [f[0] + f[2] / 2 for f in with_face]
    cys = [f[1] + f[3] / 2 for f in with_face]

    def mad(values: list[float]) -> float:
        m = statistics.median(values)
        return statistics.median(abs(v - m) for v in values)

    # Median absolute deviation ignores stray detections (faces in gameplay, overlays, etc.).
    spread = (mad(cxs) + mad(cys)) / src_w

    if layout == "stream" or (layout == "auto" and rel_w < 0.10 and spread < 0.04):
        fw = statistics.median(f[2] for f in with_face)
        fh = statistics.median(f[3] for f in with_face)
        cx, cy = statistics.median(cxs), statistics.median(cys)
        aspect = OUT_W / CAM_H
        ch = min(fh * 3.2, src_h)
        cw = min(ch * aspect, src_w)
        ch = cw / aspect
        x = int(min(max(cx - cw / 2, 0), src_w - cw))
        y = int(min(max(cy - ch * 0.45, 0), src_h - ch))
        print(f"[reframe] small static face ({rel_w:.0%} width) -> stream layout (facecam over gameplay)")
        return Plan("stream", src_w, src_h, cam_box=(x, y, int(cw) // 2 * 2, int(ch) // 2 * 2))

    crop_w = int(src_h * 9 / 16) // 2 * 2
    points = [(t, (f[0] + f[2] / 2) if f else None) for t, f in primary]
    print(f"[reframe] speaker face ({rel_w:.0%} width) -> tracked crop")
    return Plan("track", src_w, src_h, track=_smooth_track(points, src_w, crop_w))


def frame_filter(plan: Plan, work_dir: Path) -> str:
    """Filtergraph turning [0:v] into a 1080x1920 [base] stream."""
    W, H = plan.src_w, plan.src_h
    if plan.layout == "crop":
        return (
            f"[0:v]scale=-2:{OUT_H},"
            f"crop={OUT_W}:{OUT_H}:(iw-{OUT_W})*{plan.crop_x}:0,setsar=1[base]"
        )
    if plan.layout == "blur":
        return (
            "[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W // 4}:{OUT_H // 4},boxblur=12:2,eq=brightness=-0.08,"
            f"scale={OUT_W}:{OUT_H}[bg];"
            f"[fgsrc]scale={OUT_W}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
    if plan.layout == "stream":
        x, y, w, h = plan.cam_box
        game_h = OUT_H - CAM_H
        gw = min(int(H * OUT_W / game_h) // 2 * 2, W)
        gx = (W - gw) // 2
        return (
            "[0:v]split=2[cam][game];"
            f"[cam]crop={w}:{h}:{x}:{y},scale={OUT_W}:{CAM_H},setsar=1[top];"
            f"[game]crop={gw}:{H}:{gx}:0,scale={OUT_W}:{game_h},setsar=1[bot];"
            "[top][bot]vstack[base]"
        )
    if plan.layout == "track":
        crop_w = int(H * 9 / 16) // 2 * 2
        cmds = work_dir / "track.cmd"
        work_dir.mkdir(parents=True, exist_ok=True)
        # Interpolate the sampled positions to 15 updates/s for smooth pans.
        lines, pts = [], plan.track
        for (t0, x0), (t1, x1) in zip(pts, pts[1:]):
            steps = max(1, int((t1 - t0) * 15))
            for s in range(steps):
                t = t0 + (t1 - t0) * s / steps
                x = x0 + (x1 - x0) * s / steps
                lines.append(f"{t:.3f} crop@trk x {int(x)};")
        if pts:
            lines.append(f"{pts[-1][0]:.3f} crop@trk x {int(pts[-1][1])};")
        cmds.write_text("\n".join(lines) + "\n")
        x0 = int(pts[0][1]) if pts else (W - crop_w) // 2
        return (
            f"[0:v]sendcmd=f='{cmds.resolve()}',"
            f"crop@trk=w={crop_w}:h={H}:x={x0}:y=0,"
            f"scale={OUT_W}:{OUT_H},setsar=1[base]"
        )
    raise ValueError(f"unknown layout: {plan.layout}")
