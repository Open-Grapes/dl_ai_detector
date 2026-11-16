"""The forensic pipeline: metadata -> pixel models -> faces -> forensics -> (video) timeline -> verdict."""

import statistics
import time
from pathlib import Path

from PIL import Image, ImageOps

from . import charts, forensics, metadata, models
from .common import fmt_t, kind_of, log

LOW, HIGH = 0.25, 0.75   # score bands


def _views(img):
    """Whole image + a centre square crop at higher effective resolution (detail matters to the detectors)."""
    w, h = img.size
    s = min(w, h)
    crop = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    if s > 768:
        c = 768
        crop = img.crop(((w - c) // 2, (h - c) // 2, (w - c) // 2 + c, (h - c) // 2 + c))
    return [img, crop]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def analyze(path, max_frames=24, progress=None, cancel=None):
    t0 = time.time()
    path = Path(path)
    prog = progress or (lambda f, s: None)
    kind = kind_of(path)
    if kind is None:
        try:
            Image.open(path).close()
            kind = "image"
        except Exception:
            kind = "video"
    R = {"file": {"name": path.name, "path": str(path), "size_mb": round(path.stat().st_size / 1e6, 2), "kind": kind},
         "models_used": [], "warnings": []}
    imgs = {}

    prog(0.03, "Reading metadata…")
    R["metadata"] = metadata.inspect(path, kind)

    gen_models = models.installed("image")
    face_models = models.installed("face")
    finder = None
    if face_models and models.face_detector_path():
        from .video import FaceFinder
        finder = FaceFinder(models.face_detector_path())
    clfs = []
    for i, m in enumerate(gen_models + face_models):
        prog(0.08 + 0.12 * i / max(1, len(gen_models + face_models)), f"Loading {m['name']}…")
        try:
            clfs.append((m, models.get_classifier(m["key"])))
            R["models_used"].append({"key": m["key"], "name": m["name"], "repo": m["repo"], "kind": m["kind"],
                                     "version": (models.manifest(m["key"]) or {}).get("sha", "")[:10]})
        except Exception as e:
            R["warnings"].append(f"{m['name']} failed to load: {e}")
            log(f"load {m['key']} failed: {e!r}")
    gclf = [(m, c) for m, c in clfs if m["kind"] == "image"]
    fclf = [(m, c) for m, c in clfs if m["kind"] == "face"]

    if kind == "image":
        _image(path, R, imgs, gclf, fclf, finder, prog)
    else:
        _video(path, R, imgs, gclf, fclf, finder, prog, max_frames, cancel)
    if cancel and cancel.is_set():
        return None, None

    prog(0.97, "Writing summary…")
    R["verdict"] = verdict(R)
    R["elapsed_s"] = round(time.time() - t0, 1)
    R["device"] = models.device() if clfs else "—"
    prog(1.0, "Done")
    return R, imgs


def _faces(img, finder, fclf):
    if not finder or not fclf:
        return []
    out = []
    for box, conf, crop in finder.find(img):
        scores = {m["key"]: c.predict([crop])[0] for m, c in fclf}
        out.append({"box": box, "detector_conf": round(conf, 3), "scores": scores, "score": _mean(scores.values()),
                    "crop": crop})
    return out


def _forensics(img, R, imgs, prog, base=0.85):
    prog(base, "Error Level Analysis…")
    imgs["original"] = img
    imgs["ela"] = forensics.ela(img)
    prog(base + 0.04, "Frequency spectrum…")
    imgs["fft"], peak = forensics.fft_spectrum(img)
    imgs["noise"], noise_cv = forensics.noise_map(img)
    R["forensics"] = {"fft_peak_score": peak, "noise_unevenness": noise_cv}


def _image(path, R, imgs, gclf, fclf, finder, prog):
    try:
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    except Exception as e:
        raise RuntimeError(f"Could not decode this image: {e}")
    views = _views(img)
    per = {}
    for i, (m, c) in enumerate(gclf):
        prog(0.25 + 0.4 * i / max(1, len(gclf)), f"Scoring with {m['name']}…")
        per[m["key"]] = _mean(c.predict(views))
    R["pixel"] = {"per_model": per, "score": _mean(per.values())}
    prog(0.7, "Looking for faces…")
    faces = _faces(img, finder, fclf)
    R["faces"] = {"enabled": bool(finder and fclf), "count": len(faces),
                  "max_score": max((f["score"] for f in faces), default=None),
                  "items": [{k: v for k, v in f.items() if k != "crop"} for f in faces]}
    imgs["faces"] = [(f["crop"], f["score"]) for f in faces]
    warn, det = forensics.compression_check(path, img, R["metadata"]["fields"], "image")
    R["compression"] = {"warning": warn, **det}
    _forensics(img, R, imgs, prog)


def _video(path, R, imgs, gclf, fclf, finder, prog, max_frames, cancel):
    from . import video
    prog(0.2, "Sampling frames…")
    info, frames = video.sample(path, max_frames, cancel,
                                lambda k, n: prog(0.2 + 0.15 * k / n, f"Sampling frames… {k}/{n}"))
    if cancel and cancel.is_set():
        return
    pil = [f for _, f, _ in frames]
    times = [t for t, _, _ in frames]
    per_frame = {}
    for i, (m, c) in enumerate(gclf):
        prog(0.36 + 0.3 * i / max(1, len(gclf)), f"Scoring {len(pil)} frames with {m['name']}…")
        per_frame[m["key"]] = c.predict(pil)
    frame_scores = [_mean([per_frame[k][j] for k in per_frame]) for j in range(len(pil))] if per_frame else []
    face_scores, all_faces = [], []
    for j, f in enumerate(pil):
        if cancel and cancel.is_set():
            return
        if finder and fclf:
            prog(0.66 + 0.12 * j / len(pil), f"Faces in frame {j + 1}/{len(pil)}…")
        fs = _faces(f, finder, fclf)
        face_scores.append(max((x["score"] for x in fs), default=None))
        for x in fs:
            all_faces.append((times[j], x))
    prog(0.8, "Temporal flicker check…")
    flick, worst = [], (None, -1, None)
    for t, a, b in frames:
        if b is None:
            flick.append((t, None))
            continue
        s, heat = video.flicker(a, b)
        flick.append((t, s))
        if s is not None and s > worst[1]:
            worst = (t, s, heat)
    fl = [s for _, s in flick if s is not None]
    med = statistics.median(fl) if fl else None
    spikes = [t for t, s in flick if s is not None and med and s > 2.5 * med and s > 12]

    gs = frame_scores
    hi_t = [times[j] for j, s in enumerate(gs) if s is not None and s >= HIGH]
    lo_n = sum(1 for s in gs if s is not None and s <= LOW)
    hybrid = bool(gs) and len(hi_t) >= max(1, 0.15 * len(gs)) and lo_n >= max(1, 0.15 * len(gs))
    score = None
    if gs:
        score = 0.5 * _mean(gs) + 0.5 * _pct(gs, 0.8)
    R["pixel"] = {"per_model": {k: _mean(v) for k, v in per_frame.items()}, "score": score,
                  "frame_mean": _mean(gs), "frame_p80": _pct(gs, 0.8), "frame_max": max(gs) if gs else None,
                  "hybrid": hybrid, "high_frames_at": [round(t, 2) for t in hi_t]}
    R["video"] = {**info, "sampled": len(frames),
                  "timeline": [{"t": round(t, 2), "ai": s, "face": fsc,
                                **{k: per_frame[k][j] for k in per_frame}}
                               for j, (t, s, fsc) in enumerate(zip(times, gs or [None] * len(times), face_scores))],
                  "flicker": [{"t": round(t, 2), "residual": s} for t, s in flick],
                  "flicker_median": med, "flicker_spikes_at": [round(t, 2) for t in spikes]}
    fmax = max((x["score"] for _, x in all_faces), default=None)
    R["faces"] = {"enabled": bool(finder and fclf), "count": len(all_faces), "max_score": fmax,
                  "frame_mean": _mean(face_scores),
                  "items": [{"t": round(t, 2), **{k: v for k, v in x.items() if k != "crop"}} for t, x in all_faces]}
    top = sorted(all_faces, key=lambda tx: -(tx[1]["score"] or 0))[:12]
    imgs["faces"] = [(x["crop"], x["score"], t) for t, x in top]

    fsize = Path(path).stat().st_size
    bpp = (fsize * 8 / info["duration"] / (info["width"] * info["height"] * (info["fps"] or 25))
           if info["duration"] and info["width"] else None)
    warn, det = forensics.compression_check(path, pil[0], R["metadata"]["fields"], "video",
                                            {"width": info["width"], "height": info["height"],
                                             "bits_per_pixel": bpp})
    R["compression"] = {"warning": warn, **det}

    dur = info["duration"] or (times[-1] if times else 1)
    series = []
    if gs:
        series.append(("AI pixel score", "#e5484d", list(zip(times, gs))))
    if any(s is not None for s in face_scores):
        series.append(("Face deepfake", "#3b82f6", list(zip(times, face_scores))))
    imgs["timeline_series"] = (series, dur)
    if fl:
        imgs["flicker_series"] = ([("Warp residual", "#f59e0b", flick)], dur, max(fl) * 1.15)
    if worst[2] is not None:
        imgs["flicker_worst"] = (worst[0], worst[2])
    rep = max(range(len(pil)), key=lambda j: gs[j] or 0) if gs else 0
    R["forensics_frame_t"] = round(times[rep], 2)
    _forensics(pil[rep], R, imgs, prog, 0.88)


def verdict(R):
    meta = R["metadata"]
    px = (R.get("pixel") or {}).get("score")
    per = (R.get("pixel") or {}).get("per_model") or {}
    face = (R.get("faces") or {}).get("max_score")
    lines, level = [], "none"
    vals = [v for v in per.values() if v is not None]
    disagree = len(vals) >= 2 and max(vals) - min(vals) > 0.5

    if meta["proof"]:
        label, level = "AI-generated — confirmed by metadata", "ai"
        lines += meta["proof"]
    elif px is None and face is None:
        label = "No AI models installed — metadata & forensics only"
        level = "warn" if meta["hints"] else "none"
    else:
        s = px if px is not None else 0
        if face is not None:
            s = max(s, 0.9 * face)      # a convincing face swap can sit in an otherwise real frame
        if s >= HIGH:
            label, level = "Likely AI-generated", "ai"
        elif s >= 0.5:
            label, level = "Possibly AI — inconclusive", "warn"
        elif s >= LOW:
            label, level = "Probably real (weak AI signal)", "lean"
        else:
            label, level = "Likely real", "ok"
        if meta["hints"] and level in ("ok", "lean"):
            label, level = "Inconclusive — metadata hints at AI", "warn"
        if disagree and level in ("ai", "ok"):
                level = "warn" if level == "ai" else "lean"
                label = "Possibly AI — models disagree" if level == "warn" else "Probably real (models disagree)"
    if disagree:
        lines.append(f"Pixel models disagree ({min(vals) * 100:.0f}% – {max(vals) * 100:.0f}%), treat that score with caution")
    if px is not None:
        lines.append(f"AI pixel score {px * 100:.0f}% (average of {len(per)} model{'s' * (len(per) != 1)})")
    if face is not None:
        lines.append(f"Highest face deepfake score {face * 100:.0f}% across {R['faces']['count']} face(s)")
    elif (R.get("faces") or {}).get("enabled"):
        lines.append("No faces found for the deepfake scanner")
    pv = R.get("pixel") or {}
    if pv.get("hybrid"):
        at = ", ".join(fmt_t(t) for t in pv["high_frames_at"][:8])
        lines.append(f"Mixed video: some frames score high ({at}) while others look real — possible spliced AI scenes")
    vid = R.get("video") or {}
    if vid.get("flicker_spikes_at"):
        lines.append("Temporal warp spikes at " + ", ".join(fmt_t(t) for t in vid["flicker_spikes_at"][:8]) +
                     " (texture/edges changing more than the motion explains — check those moments)")
    lines += meta["hints"]
    if R.get("compression", {}).get("warning"):
        lines.append(R["compression"]["warning"])
    if meta["camera"] and not meta["proof"]:
        lines.append("Has camera EXIF (supports a real photo, but EXIF can be faked or copied)")
    overall = None if px is None and face is None else max(px or 0, 0.9 * (face or 0))
    if meta["proof"]:
        overall = 1.0     # an AI declaration inside the file outweighs any pixel guess
    return {"label": label, "level": level, "overall": overall, "lines": lines}
