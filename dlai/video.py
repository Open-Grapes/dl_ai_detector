"""Video: frame sampling, face finding helper and the temporal flicker/warp check."""

import cv2
import numpy as np
from PIL import Image


def probe(path):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError("Could not open the video (unsupported codec?)")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    dur = n / fps if fps else 0
    return {"fps": round(fps, 3), "frames": n, "width": w, "height": h, "duration": round(dur, 2)}


def sample(path, max_frames=24, cancel=None, progress=None):
    """Evenly spaced key frames (≈1 per second, capped at max_frames, at least 8 when the clip allows),
    each with the frame right after it for the temporal check.
    Yields (time_s, PIL_frame, PIL_next_frame_or_None)."""
    info = probe(path)
    n, fps = info["frames"], info["fps"] or 25
    if n <= 0:
        n = int((info["duration"] or 10) * fps)
    want = int(min(max_frames, max(8, info["duration"]))) if info["duration"] else max_frames
    want = max(1, min(want, n))
    idxs = sorted(set(int(i) for i in np.linspace(0, max(n - 2, 0), want)))
    cap = cv2.VideoCapture(str(path))
    out = []
    try:
        for k, i in enumerate(idxs):
            if cancel and cancel.is_set():
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, a = cap.read()
            if not ok:
                continue
            ok2, b = cap.read()
            fa = Image.fromarray(cv2.cvtColor(a, cv2.COLOR_BGR2RGB))
            fb = Image.fromarray(cv2.cvtColor(b, cv2.COLOR_BGR2RGB)) if ok2 else None
            out.append((i / fps, fa, fb))
            if progress:
                progress(k + 1, len(idxs))
    finally:
        cap.release()
    if not out:
        raise RuntimeError("No frames could be decoded from this video")
    return info, out


def _gray(img, width=480):
    g = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    s = width / g.shape[1]
    return cv2.resize(g, (width, max(1, int(g.shape[0] * s))), interpolation=cv2.INTER_AREA)


def flicker(a, b):
    """Motion-compensated residual between two consecutive frames.
    Optical flow explains real motion; what's left over (texture that pops, edges that warp, details that
    re-draw themselves) is the residual. Returns (score, heatmap_PIL) or (None, None) on a scene cut."""
    ga, gb = _gray(a), _gray(b)
    ha = cv2.calcHist([ga], [0], None, [32], [0, 256]).ravel()
    hb = cv2.calcHist([gb], [0], None, [32], [0, 256]).ravel()
    if cv2.compareHist(ha.astype(np.float32), hb.astype(np.float32), cv2.HISTCMP_BHATTACHARYYA) > 0.35:
        return None, None
    flow = cv2.calcOpticalFlowFarneback(gb, ga, None, 0.5, 3, 21, 3, 5, 1.1, 0)
    h, w = ga.shape
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    warped = cv2.remap(ga, gx + flow[..., 0], gy + flow[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    res = np.abs(gb.astype(np.float32) - warped.astype(np.float32))
    # focus on detail: residual weighted by local edge strength of the target frame
    edges = cv2.magnitude(cv2.Sobel(gb, cv2.CV_32F, 1, 0), cv2.Sobel(gb, cv2.CV_32F, 0, 1))
    edges = edges / max(np.percentile(edges, 99), 1e-3)
    detail = res * np.clip(edges, 0, 1)
    m = 8                                   # ignore borders, where flow is unreliable
    core = detail[m:-m, m:-m] if h > 2 * m and w > 2 * m else detail
    score = float(np.percentile(core, 99))
    vis = np.clip(cv2.GaussianBlur(detail, (0, 0), 2) / max(np.percentile(detail, 99.5), 1e-3) * 255, 0, 255)
    heat = cv2.applyColorMap(vis.astype(np.uint8), cv2.COLORMAP_INFERNO)
    base = cv2.cvtColor(gb, cv2.COLOR_GRAY2BGR)
    over = cv2.addWeighted(base, 0.45, heat, 0.75, 0)
    return round(score, 2), Image.fromarray(cv2.cvtColor(over, cv2.COLOR_BGR2RGB))


class FaceFinder:
    def __init__(self, model_path):
        self.det = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), 0.8, 0.3, 50)

    def find(self, img, min_size=48, max_faces=6):
        """-> list of (box(x,y,w,h), score, crop_PIL) on the original image."""
        rgb = np.asarray(img.convert("RGB"))
        H, W = rgb.shape[:2]
        s = min(1.0, 1280 / max(H, W))
        small = cv2.resize(rgb, (int(W * s), int(H * s))) if s < 1 else rgb
        bgr = cv2.cvtColor(small, cv2.COLOR_RGB2BGR)
        self.det.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, faces = self.det.detect(bgr)
        out = []
        if faces is None:
            return out
        for f in sorted(faces, key=lambda f: -f[2] * f[3])[:max_faces]:
            x, y, w, h = (f[:4] / s).tolist()
            if min(w, h) < min_size:
                continue
            side = max(w, h) * 1.35
            cx, cy = x + w / 2, y + h / 2
            x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
            x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
            out.append(((int(x), int(y), int(w), int(h)), float(f[-1]), img.crop((x0, y0, x1, y1)).convert("RGB")))
        return out
