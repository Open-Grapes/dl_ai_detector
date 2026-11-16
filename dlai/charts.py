"""Small PIL-drawn charts (used on screen and in the PDF report), so no plotting library is needed."""

from PIL import Image, ImageDraw, ImageFont


def font(size, bold=False):
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")) + ("DejaVuSans.ttf",):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def timeline(series, duration, w=900, h=260, dark=False, title="AI probability over time", ymax=1.0,
             bands=True, unit="%"):
    """series: list of (label, color, [(t, value_or_None), ...])."""
    bg, fg, grid, muted = (("#1c1d22", "#e6e7ea", "#33363f", "#9aa0aa") if dark
                           else ("#ffffff", "#1d2330", "#e3e6ea", "#6b7280"))
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    f, fb = font(13), font(14, True)
    L, R, T, B = 48, 16, 30, 28
    pw, ph = w - L - R, h - T - B
    d.text((L, 6), title, fill=fg, font=fb)
    if bands and ymax == 1.0:
        y75 = T + ph * (1 - 0.75)
        y50 = T + ph * (1 - 0.5)
        d.rectangle((L, T, L + pw, y75), fill="#fdecec" if not dark else "#3a2426")
        d.rectangle((L, y75, L + pw, y50), fill="#fff6e0" if not dark else "#3a3324")
    for k in range(5):
        v = ymax * k / 4
        y = T + ph * (1 - k / 4)
        d.line((L, y, L + pw, y), fill=grid)
        lab = f"{v * 100:.0f}%" if unit == "%" else f"{v:.0f}"
        d.text((L - 6 - d.textlength(lab, font=f), y - 8), lab, fill=muted, font=f)
    dur = max(duration or 0, 1e-6)
    ticks = 6
    for k in range(ticks + 1):
        t = dur * k / ticks
        x = L + pw * k / ticks
        lab = f"{int(t // 60)}:{int(t % 60):02d}"
        d.text((x - d.textlength(lab, font=f) / 2, T + ph + 6), lab, fill=muted, font=f)
    lx = L + pw
    for label, color, pts in reversed(series):
        lx -= d.textlength(label, font=f) + 26
        d.rectangle((lx, 10, lx + 12, 20), fill=color)
        d.text((lx + 16, 6), label, fill=fg, font=f)
    for label, color, pts in series:
        xy = [(L + pw * (t / dur), T + ph * (1 - min(max(v / ymax, 0), 1))) for t, v in pts if v is not None]
        if len(xy) > 1:
            d.line(xy, fill=color, width=3, joint="curve")
        for x, y in xy:
            d.ellipse((x - 3.5, y - 3.5, x + 3.5, y + 3.5), fill=color)
    return img
