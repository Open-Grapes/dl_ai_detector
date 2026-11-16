"""Visual forensics for human verification: ELA, FFT spectrum, noise map, plus a compression estimate."""

import io

import cv2
import numpy as np
from PIL import Image

MAX_SIDE = 1600

# libjpeg standard luminance quantisation table (quality 50), zig-zag order irrelevant for the estimate
_STD_LUM = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99], dtype=np.float64)


def _fit(img, side=MAX_SIDE):
    if max(img.size) > side:
        img = img.copy()
        img.thumbnail((side, side), Image.LANCZOS)
    return img


def _colorize(gray_u8, cmap=cv2.COLORMAP_INFERNO):
    return Image.fromarray(cv2.cvtColor(cv2.applyColorMap(gray_u8, cmap), cv2.COLOR_BGR2RGB))


def ela(img, quality=90):
    """Error Level Analysis: re-save as JPEG and amplify the difference. Regions pasted / inpainted /
    generated separately often re-compress differently from their surroundings."""
    img = _fit(img.convert("RGB"))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    re = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    diff = np.abs(np.asarray(img, np.int16) - np.asarray(re, np.int16)).max(axis=2).astype(np.float32)
    hi = max(np.percentile(diff, 99.5), 1.0)
    g = np.clip(diff / hi * 255, 0, 255).astype(np.uint8)
    return _colorize(g)


def fft_spectrum(img):
    """Log-magnitude 2D spectrum of the high-pass residual. Bright off-centre dots / grid lines = periodic
    patterns (upsampling layers of diffusion/GAN decoders, AI upscalers; also JPEG's 8x8 grid).
    Returns (image, peak_score). peak_score ~1-3 for camera photos, higher with strong periodic artifacts."""
    g = np.asarray(_fit(img.convert("L"), 1024), np.float32)
    h, w = g.shape
    n = min(h, w)
    g = g[(h - n) // 2:(h - n) // 2 + n, (w - n) // 2:(w - n) // 2 + n]
    res = g - cv2.GaussianBlur(g, (0, 0), 2)
    win = np.outer(np.hanning(n), np.hanning(n)).astype(np.float32)
    f = np.fft.fftshift(np.fft.fft2(res * win))
    mag = np.log1p(np.abs(f))
    lo, hi = np.percentile(mag, 1), np.percentile(mag, 99.9)
    vis = np.clip((mag - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)

    # peak score: strongest local maxima in the outer band vs. a smooth radial baseline, ignoring JPEG-grid bins
    yy, xx = np.mgrid[:n, :n]
    c = n / 2
    r = np.hypot(yy - c, xx - c)
    band = (r > n * 0.12) & (r < n * 0.48)
    step = n / 8
    near_grid = (np.abs(((yy - c) / step) - np.round((yy - c) / step)) * step < 2) & \
                (np.abs(((xx - c) / step) - np.round((xx - c) / step)) * step < 2)
    band &= ~near_grid
    base = cv2.blur(mag, (31, 31))
    excess = (mag - base)[band]
    peak = float(np.percentile(excess, 99.95) / max(np.std(excess), 1e-6)) if excess.size else 0.0
    return _colorize(vis, cv2.COLORMAP_VIRIDIS), round(peak, 2)


def noise_map(img):
    """Local noise level (residual after denoising). Real sensors leave fairly even grain; generated or
    retouched areas are often unnaturally smooth or have a different noise texture."""
    g = np.asarray(_fit(img.convert("L")), np.float32)
    res = g - cv2.medianBlur(g.astype(np.uint8), 3).astype(np.float32)
    var = cv2.blur(res * res, (15, 15))
    sd = np.sqrt(var)
    hi = max(np.percentile(sd, 99), 1e-3)
    vis = np.clip(sd / hi * 255, 0, 255).astype(np.uint8)
    # uniformity: coefficient of variation of the noise level across 32px blocks
    bs = 32
    hh, ww = (sd.shape[0] // bs) * bs, (sd.shape[1] // bs) * bs
    blocks = sd[:hh, :ww].reshape(hh // bs, bs, ww // bs, bs).mean(axis=(1, 3)) if hh and ww else sd
    cv = float(blocks.std() / max(blocks.mean(), 1e-6))
    return _colorize(vis, cv2.COLORMAP_MAGMA), round(cv, 2)


def jpeg_quality(path):
    """Estimated JPEG quality (1-100) from the luminance quantisation table, or None if not a JPEG."""
    try:
        with Image.open(path) as im:
            q = getattr(im, "quantization", None)
            if not q or im.format != "JPEG":
                return None
            tbl = np.array(q[0], dtype=np.float64)
    except Exception:
        return None
    best, err = None, 1e18
    for quality in range(1, 101):
        scale = 5000 / quality if quality < 50 else 200 - 2 * quality
        std = np.clip(np.floor((_STD_LUM * scale + 50) / 100), 1, 255)
        e = np.abs(np.sort(std) - np.sort(tbl)).sum()
        if e < err:
            best, err = quality, e
    return best


def compression_check(path, img, meta_fields, kind, video_info=None):
    """Returns (warning_text or None, details dict)."""
    notes, det = [], {}
    if kind == "image":
        q = jpeg_quality(path)
        det["jpeg_quality"] = q
        w, h = img.size
        det["resolution"] = f"{w}x{h}"
        no_exif = not any(k.startswith("EXIF") for k in meta_fields)
        fmt = (meta_fields.get("Format") or "").split(" ")[0]
        if q is not None and q < 80:
            notes.append(f"heavy JPEG compression (quality ≈{q})")
        if fmt == "WEBP":
            notes.append("WebP re-encode (typical of web/social downloads)")
        if max(w, h) <= 1600 and no_exif and (q is None or q <= 90) and fmt in ("JPEG", "WEBP"):
            notes.append(f"small size {w}x{h} with no EXIF — looks like a WhatsApp/Telegram/social copy")
        if min(w, h) < 256:
            notes.append("very low resolution")
        if fmt == "PNG" and (w, h) in {(1920, 1080), (2560, 1440), (1366, 768), (1170, 2532), (1179, 2556),
                                        (1080, 2400), (1290, 2796), (3840, 2160), (1440, 3200)}:
            notes.append("screen-sized PNG — possibly a screenshot")
    elif video_info:
        bpp = video_info.get("bits_per_pixel")
        det.update(video_info)
        if bpp is not None and bpp < 0.05:
            notes.append(f"low bit rate ({bpp:.3f} bits/pixel) — heavily compressed")
        if video_info.get("height") and min(video_info["width"], video_info["height"]) < 480:
            notes.append("low resolution video")
    if notes:
        return "Compression warning: " + "; ".join(notes) + ". Pixel scores are less reliable.", det
    return None, det
