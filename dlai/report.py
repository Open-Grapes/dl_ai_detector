"""Export: <name>_report.json, <name>_report.pdf and the forensic PNGs into one folder."""

import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from . import charts
from .common import APP_NAME, APP_VERSION

LEVEL_COLOR = {"ai": "#c62828", "warn": "#d97706", "lean": "#2f7d32", "ok": "#2e7d32", "none": "#555b66"}
A4 = (1240, 1754)   # 150 dpi


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        return round(o, 4)
    return o


def series_images(imgs, dark=False):
    out = {}
    if "timeline_series" in imgs:
        s, dur = imgs["timeline_series"]
        out["timeline"] = charts.timeline(s, dur, dark=dark)
    if "flicker_series" in imgs:
        s, dur, ymax = imgs["flicker_series"]
        out["flicker"] = charts.timeline(s, dur, dark=dark, title="Temporal warp residual (spikes = jitter / popping)",
                                         ymax=ymax, bands=False, unit="")
    return out


def export(R, imgs, folder):
    folder = Path(folder)
    stem = Path(R["file"]["name"]).stem
    out = folder / f"{stem}_ai_report"
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for k in ("ela", "fft", "noise"):
        if imgs.get(k) is not None:
            p = out / f"{stem}_{k}.png"
            imgs[k].save(p)
            saved.append(p.name)
    charts_ = series_images(imgs)
    for k, im in charts_.items():
        p = out / f"{stem}_{k}.png"
        im.save(p)
        saved.append(p.name)
    if imgs.get("flicker_worst"):
        p = out / f"{stem}_flicker_worst.png"
        imgs["flicker_worst"][1].save(p)
        saved.append(p.name)
    for i, f in enumerate(imgs.get("faces") or []):
        p = out / f"{stem}_face{i + 1}.png"
        f[0].save(p)
        saved.append(p.name)
    data = _clean({"app": f"{APP_NAME} {APP_VERSION}", "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   **R, "images": saved})
    (out / f"{stem}_report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    pdf = out / f"{stem}_report.pdf"
    pages = _pdf_pages(R, imgs, charts_)
    pages[0].save(pdf, "PDF", resolution=150, save_all=True, append_images=pages[1:])
    return out


def _wrap(d, text, f, width):
    words, line, out = text.split(), "", []
    for w in words:
        t = (line + " " + w).strip()
        if d.textlength(t, font=f) > width and line:
            out.append(line)
            line = w
        else:
            line = t
    if line:
        out.append(line)
    return out


class _Pager:
    def __init__(self):
        self.pages = []
        self.new()

    def new(self):
        self.img = Image.new("RGB", A4, "white")
        self.d = ImageDraw.Draw(self.img)
        self.y = 80
        self.pages.append(self.img)

    def need(self, h):
        if self.y + h > A4[1] - 80:
            self.new()

    def text(self, s, size=22, bold=False, color="#1d2330", indent=0, gap=8):
        f = charts.font(size, bold)
        for ln in _wrap(self.d, s, f, A4[0] - 160 - indent):
            self.need(size + gap)
            self.d.text((80 + indent, self.y), ln, fill=color, font=f)
            self.y += size + gap

    def image(self, im, maxw=A4[0] - 160, maxh=640, caption=None):
        im = im.copy()
        im.thumbnail((maxw, maxh))
        self.need(im.height + 50)
        self.img.paste(im, (80, self.y))
        self.y += im.height + 8
        if caption:
            self.text(caption, 18, color="#555b66")
        self.y += 14


def _pdf_pages(R, imgs, charts_):
    P = _Pager()
    v = R["verdict"]
    P.text(f"{APP_NAME} — forensic report", 34, True)
    P.text(f"{R['file']['name']}  ·  {R['file']['kind']}  ·  {R['file']['size_mb']} MB  ·  "
           f"{time.strftime('%Y-%m-%d %H:%M')}", 20, color="#555b66")
    P.y += 20
    P.text(v["label"], 40, True, LEVEL_COLOR.get(v["level"], "#1d2330"))
    if v.get("overall") is not None:
        P.text(f"Overall AI likelihood: {v['overall'] * 100:.0f}%", 26, True)
    P.y += 10
    for ln in v["lines"]:
        P.text("• " + ln, 21)
    P.y += 16
    P.text("Breakdown", 28, True)
    m = R["metadata"]
    P.text(f"Metadata result: {m['verdict']}", 22, True)
    for ln in m["proof"] + m["hints"] + m["findings"]:
        P.text("– " + ln, 20, indent=20)
    px = R.get("pixel") or {}
    if px.get("score") is not None:
        P.text(f"AI pixel score: {px['score'] * 100:.0f}%", 22, True)
        names = {u["key"]: u["name"] for u in R["models_used"]}
        for k, s in (px.get("per_model") or {}).items():
            P.text(f"– {names.get(k, k)}: {s * 100:.0f}%", 20, indent=20)
    fc = R.get("faces") or {}
    if fc.get("max_score") is not None:
        P.text(f"Face deepfake score (max): {fc['max_score'] * 100:.0f}%  ({fc['count']} face(s))", 22, True)
    comp = (R.get("compression") or {}).get("warning")
    P.text(f"Compression: {comp or 'no heavy compression detected'}", 22, True)
    fo = R.get("forensics") or {}
    if fo:
        P.text(f"FFT peak score {fo['fft_peak_score']} · noise unevenness {fo['noise_unevenness']}", 20,
               color="#555b66")
    P.text("Scores are probabilities from machine-learning models and can be wrong, especially on new generators, "
           "heavily compressed files and screenshots. Metadata proof is strong when present; its absence proves "
           "nothing. Use the maps below to verify with your own eyes.", 18, color="#777d88")

    P.new()
    P.text("Visual forensics" + (f" (frame at {R['forensics_frame_t']} s)" if "forensics_frame_t" in R else ""), 28, True)
    caps = {"original": "Original", "ela": "Error Level Analysis — uneven bright patches can mark pasted/inpainted areas",
            "fft": "FFT spectrum — off-centre dots or grid lines = periodic artifacts (upsamplers, diffusion decoders)",
            "noise": "Noise map — unnaturally smooth or differently-textured regions stand out"}
    for k in ("original", "ela", "fft", "noise"):
        if imgs.get(k) is not None:
            P.image(imgs[k], maxh=520 if k != "fft" else 420, caption=caps[k])
    if charts_ or imgs.get("faces"):
        P.new()
    if "timeline" in charts_:
        P.text("Video timeline", 28, True)
        P.image(charts_["timeline"])
    if "flicker" in charts_:
        P.image(charts_["flicker"])
    if imgs.get("flicker_worst"):
        t, im = imgs["flicker_worst"]
        P.image(im, maxh=420, caption=f"Largest warp residual at {t:.1f} s (bright = unexplained change)")
    if imgs.get("faces"):
        P.text("Faces", 28, True)
        x0 = 80
        P.need(250)
        for f in imgs["faces"][:10]:
            crop, s = f[0], f[1]
            th = crop.copy()
            th.thumbnail((200, 200))
            if x0 + 200 > A4[0] - 80:
                P.y += 250
                P.need(250)
                x0 = 80
            P.img.paste(th, (x0, P.y))
            P.d.text((x0, P.y + 206), f"{(s or 0) * 100:.0f}%" + (f" @ {f[2]:.1f}s" if len(f) > 2 else ""),
                     fill="#1d2330", font=charts.font(18, True))
            x0 += 220
        P.y += 250
    used = ", ".join(f"{u['name']} ({u['repo']}@{u['version']})" for u in R["models_used"]) or "none"
    P.text(f"Models: {used}", 16, color="#777d88")
    return P.pages
