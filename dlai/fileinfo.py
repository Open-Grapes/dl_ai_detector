"""Quick file facts shown as soon as a file is loaded (before any analysis)."""

import datetime
from pathlib import Path

from .common import fmt_size, fmt_t  # noqa: F401


CODECS = {"h264": "H.264 / AVC", "hevc": "H.265 / HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
          "prores": "Apple ProRes", "mpeg4": "MPEG-4 Part 2", "mjpeg": "Motion JPEG", "dnxhd": "Avid DNxHD/HR"}


def _ratio(w, h):
    from math import gcd
    g = gcd(w, h) or 1
    a, b = w // g, h // g
    for name, (x, y) in {"16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3), "3:4": (3, 4), "1:1": (1, 1),
                         "21:9": (21, 9), "4:5": (4, 5), "3:2": (3, 2), "2:3": (2, 3)}.items():
        if abs(w / h - x / y) < 0.01:
            return name
    return f"{a}:{b}" if a < 100 else f"{w / h:.2f}:1"


def info(path, kind):
    """-> list of (label, value)."""
    p = Path(path)
    st = p.stat()
    mb = st.st_size / 1e6
    rows = [("Filename", p.name), ("File size", f"{mb:.1f} MB" if mb < 1000 else fmt_size(mb))]
    if kind == "video":
        rows += _video(p)
    else:
        rows += _image(p)
    rows.append(("Modified", datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")))
    rows.append(("Folder", str(p.parent)))
    return rows


def _video(p):
    rows = []
    try:
        import av
        with av.open(str(p)) as c:
            fmt = (c.format.name or "").split(",")
            ext = p.suffix.lstrip(".").upper()
            rows.append(("Container", ext if ext.lower() in fmt or ext in ("MP4", "MOV", "M4V") else
                         (c.format.long_name or c.format.name)))
            dur = c.duration / 1e6 if c.duration else None
            v = next((s for s in c.streams if s.type == "video"), None)
            a = next((s for s in c.streams if s.type == "audio"), None)
            if v is not None and dur is None and v.duration and v.time_base:
                dur = float(v.duration * v.time_base)
            if dur:
                rows.insert(0, ("Duration", fmt_t(dur)))
            if v is not None:
                cc = v.codec_context
                w, h = cc.width, cc.height
                rows.append(("Resolution", f"{w} × {h}  ({_ratio(w, h)}{', portrait' if h > w else ''})"))
                fr = v.average_rate or v.guessed_rate
                if fr:
                    rows.append(("Frame rate", f"{float(fr):.3g} FPS"))
                if v.frames:
                    rows.append(("Frames", f"{v.frames:,}"))
                prof = f" ({cc.profile})" if getattr(cc, "profile", None) else ""
                rows.append(("Video codec", CODECS.get(cc.name, cc.name.upper()) + prof))
                if v.bit_rate:
                    rows.append(("Video bit rate", f"{v.bit_rate / 1e6:.2f} Mb/s"))
                pf = getattr(cc, "pix_fmt", None) or getattr(getattr(cc, "format", None), "name", None)
                if pf:
                    rows.append(("Pixel format", pf))
            if c.bit_rate:
                rows.append(("Total bit rate", f"{c.bit_rate / 1e6:.2f} Mb/s"))
            if a is not None:
                ac = a.codec_context
                ch = getattr(getattr(ac, "layout", None), "name", "") or f"{ac.channels} ch"
                rows.append(("Audio", f"{ac.name.upper()} · {ac.sample_rate / 1000:g} kHz · {ch}"))
            else:
                rows.append(("Audio", "none"))
            enc = (c.metadata or {}).get("encoder") or ((v.metadata or {}).get("encoder") if v is not None else "")
            if enc:
                rows.append(("Encoder tag", enc))
            ct = (c.metadata or {}).get("creation_time")
            if ct:
                rows.append(("Created (tag)", ct.replace("T", " ").replace(".000000Z", " UTC")))
    except Exception as e:
        rows.append(("Error", f"could not read container: {e}"))
    # keep the order people expect: duration first
    order = ["Duration"]
    return sorted(rows, key=lambda r: 0 if r[0] in order else 1)


def _image(p):
    rows = []
    try:
        from PIL import Image
        with Image.open(p) as im:
            rows.append(("Format", im.format or p.suffix.lstrip(".").upper()))
            rows.append(("Resolution", f"{im.width} × {im.height}  ({_ratio(im.width, im.height)}) · "
                                       f"{im.width * im.height / 1e6:.1f} MP"))
            rows.append(("Colour mode", im.mode + (" + ICC profile" if im.info.get("icc_profile") else "")))
            n = getattr(im, "n_frames", 1)
            if n > 1:
                rows.append(("Frames", str(n)))
            dpi = im.info.get("dpi")
            if dpi:
                rows.append(("DPI", f"{float(dpi[0]):.0f}"))
    except Exception as e:
        rows.append(("Error", f"could not read image: {e}"))
    return rows
