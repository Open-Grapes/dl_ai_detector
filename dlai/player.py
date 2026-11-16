"""Preview tab: a small video player you can play and scrub, with the AI score at the playhead.

Under the picture there's a score strip (one bar per analysed frame, coloured by AI score) that also works as a
seek bar. While paused, the exact frame on screen can be scored by the installed models ("Score this frame",
or automatically with "Score frames I stop on"), so frames between the analysed samples can be checked too.
"""

import threading
import time
import tkinter as tk
import traceback
from tkinter import ttk

from PIL import Image, ImageTk

from . import theme
from .common import fmt_t, log

LOW, HIGH = 0.25, 0.75


def _lvl(s):
    return "none" if s is None else "ai" if s >= HIGH else "warn" if s >= 0.5 else "lean" if s >= LOW else "ok"


def _pct(s):
    return "—" if s is None else f"{s * 100:.0f}%"


class Player(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.cap = None
        self.kind = None
        self.path = None
        self.fps, self.n, self.cur = 25.0, 0, 0
        self.playing = False
        self._frame = None          # last shown frame as PIL (full size)
        self._photo = None
        self._seek_to = None
        self._seek_job = None
        self._play_job = None
        self._live_job = None
        self._setting_scale = False
        self._was_playing = False
        self.timeline, self.dur, self.spikes = [], 0.0, []
        self.R = None
        self.cache = {}             # frame index -> score dict
        self._gen = 0
        self._scoring = False
        self.live = tk.BooleanVar(value=app.settings.get("live_score", True))

        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg="#000000", height=300)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<Button-1>", lambda e: (self.canvas.focus_set(), self.toggle()))
        for w in (self.canvas,):
            w.bind("<space>", lambda e: self.toggle())
            w.bind("<Left>", lambda e: self.step(-1))
            w.bind("<Right>", lambda e: self.step(1))
            w.bind("<Shift-Left>", lambda e: self.step(-int(self.fps)))
            w.bind("<Shift-Right>", lambda e: self.step(int(self.fps)))

        self.strip = tk.Canvas(self, height=34, highlightthickness=0, bd=0, cursor="hand2")
        self.strip.pack(fill="x", pady=(6, 0))
        self.strip.bind("<Configure>", lambda e: self._draw_strip())
        self.strip.bind("<Button-1>", self._strip_click)
        self.strip.bind("<B1-Motion>", self._strip_click)
        self.strip.bind("<ButtonRelease-1>", self._strip_release)

        ctl = ttk.Frame(self)
        ctl.pack(fill="x", pady=(4, 0))
        self.ctl = ctl
        self.play_btn = ttk.Button(ctl, text="▶", width=3, command=self.toggle)
        self.play_btn.pack(side="left")
        ttk.Button(ctl, text="◀|", width=3, command=lambda: self.step(-1)).pack(side="left", padx=(4, 0))
        ttk.Button(ctl, text="|▶", width=3, command=lambda: self.step(1)).pack(side="left", padx=(2, 0))
        self.scale = ttk.Scale(ctl, from_=0, to=1, orient="horizontal", command=self._on_scale)
        self.scale.pack(side="left", fill="x", expand=True, padx=8)
        self.scale.bind("<ButtonPress-1>", self._grab)
        self.scale.bind("<ButtonRelease-1>", self._release)
        self.time_lbl = ttk.Label(ctl, text="0:00.0 / 0:00.0", style="Muted.TLabel", width=30, anchor="e")
        self.time_lbl.pack(side="left")

        ro = ttk.Frame(self, padding=(0, 8, 0, 0))
        ro.pack(fill="x")
        self.ro = ro
        self.big = tk.Label(ro, text="", font=(theme.UI, 20, "bold"), anchor="w", width=6)
        self.big.pack(side="left")
        right = ttk.Frame(ro)
        right.pack(side="right")
        self.score_btn = ttk.Button(right, text="Score this frame", command=self.score_now)
        self.score_btn.pack(side="top", fill="x")
        ttk.Checkbutton(right, text="Score frames I stop on", variable=self.live,
                        command=self._live_toggled).pack(side="top", anchor="w", pady=(2, 0))
        txt = ttk.Frame(ro)
        txt.pack(side="left", fill="x", expand=True, padx=(6, 8))
        self.near_lbl = ttk.Label(txt, text="Load a file to preview it", style="Muted.TLabel", anchor="w")
        self.near_lbl.pack(fill="x")
        self.exact_lbl = ttk.Label(txt, text="", style="Muted.TLabel", anchor="w", justify="left")
        self.exact_lbl.pack(fill="x")
        txt.bind("<Configure>", lambda e: (self.near_lbl.configure(wraplength=max(200, e.width - 4)),
                                           self.exact_lbl.configure(wraplength=max(200, e.width - 4))))
        self.apply_theme()

    # ------------------------------------------------------------------ theme
    def apply_theme(self):
        p = getattr(self.app, "p", None)
        if not p:
            return
        self.strip.configure(bg=p["field"])
        self.big.configure(bg=p["bg"], fg=p["muted"])
        self._draw_strip()

    # ------------------------------------------------------------------ loading
    def close(self):
        self.pause()
        if self.cap is not None:
            self.cap.release()
        self.cap = None

    def load(self, path, kind):
        import cv2
        self.close()
        self._gen += 1
        self.path, self.kind = path, kind
        self.cache.clear()
        self.timeline, self.spikes, self.R = [], [], None
        self.exact_lbl.configure(text="")
        if kind == "video":
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                self.near_lbl.configure(text="This video can't be played here (codec not supported)")
                return
            self.cap = cap
            self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            self.n = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
            self.dur = self.n / self.fps
            self._set_scale_range()
            for w in self.ctl.winfo_children():
                w.state(["!disabled"])
            self.cur = -1
            self._read_at(0)
            self.near_lbl.configure(text="Space = play/pause · ← → = one frame · Shift+← → = one second")
        else:
            from PIL import ImageOps
            try:
                self._frame = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            except Exception:
                self._frame = None
            self.n, self.dur = 1, 0.0
            for w in self.ctl.winfo_children():
                w.state(["disabled"])
            self.time_lbl.configure(text="still image")
            self.near_lbl.configure(text="Still image — analyse it to see the AI score")
            self._redraw()
        self.big.configure(text="", fg=self.app.p["muted"])
        self._draw_strip()

    def set_results(self, R):
        """Called after analysis: per-frame scores for the strip and the readout."""
        self.R = R
        vid = R.get("video") or {}
        self.timeline = vid.get("timeline") or []
        self.spikes = vid.get("flicker_spikes_at") or []
        if self.kind == "video":
            self.dur = vid.get("duration") or self.dur
        self._draw_strip()
        self._readout()

    def _set_scale_range(self):
        self._setting_scale = True
        self.scale.configure(from_=0, to=max(1, self.n - 1))
        self.scale.set(0)
        self._setting_scale = False

    # ------------------------------------------------------------------ decoding / drawing
    def _read_at(self, idx):
        import cv2
        if self.cap is None:
            return False
        idx = max(0, min(self.n - 1, int(idx)))
        if idx != self.cur + 1:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, fr = self.cap.read()
        if not ok:
            # some files report one or two frames more than they decode
            if idx > 0 and idx >= self.n - 3:
                self.n = idx
                self._set_scale_range()
            return False
        self.cur = idx
        self._frame = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        self._after_frame()
        return True

    def _after_frame(self):
        self._redraw()
        self._setting_scale = True
        self.scale.set(self.cur)
        self._setting_scale = False
        t = self.cur / self.fps
        self.time_lbl.configure(text=f"{fmt_t(t)} / {fmt_t(self.dur)}  ·  frame {self.cur + 1}/{self.n}")
        self._draw_playhead()
        self._readout()
        if not self.playing:
            self._schedule_live()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        if self._frame is None:
            c.create_text(w // 2, h // 2, text="Drop an image or video to preview it here", fill="#888888",
                          font=(theme.UI, 11))
            return
        im = self._frame
        s = min(w / im.width, h / im.height)
        size = (max(1, int(im.width * s)), max(1, int(im.height * s)))
        im = im.resize(size, Image.BILINEAR if self.playing else Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(im)
        c.create_image(w // 2, h // 2, image=self._photo)

    def _draw_strip(self):
        c = self.strip
        c.delete("all")
        p = self.app.p
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 20)
        c.create_rectangle(0, 0, w, h, fill=p["field"], outline=p.get("line", p["muted"]))
        if self.kind != "video":
            return
        dur = self.dur or 1
        if not self.timeline:
            c.create_text(8, h // 2, anchor="w", text="Analyse the video to see the AI score along the timeline",
                          fill=p["faint"], font=(theme.UI, 9))
        pts = [e for e in self.timeline if e.get("ai") is not None or e.get("face") is not None]
        for i, e in enumerate(pts):
            t0 = (pts[i - 1]["t"] + e["t"]) / 2 if i else 0
            t1 = (e["t"] + pts[i + 1]["t"]) / 2 if i + 1 < len(pts) else dur
            s = max(x for x in (e.get("ai"), 0.9 * e["face"] if e.get("face") is not None else None) if x is not None) \
                if (e.get("ai") is not None or e.get("face") is not None) else None
            col = p["tags"].get(_lvl(s), p["muted"])
            x0, x1 = t0 / dur * w, max(t0 / dur * w + 1, t1 / dur * w - 1)
            bh = 6 + (h - 10) * (s or 0)
            c.create_rectangle(x0, h - 2 - bh, x1, h - 2, fill=col, outline="")
        for idx, sc in self.cache.items():
            x = idx / self.fps / dur * w
            col = p["tags"].get(_lvl(sc.get("ai")), p["muted"])
            c.create_oval(x - 3, 2, x + 3, 8, fill=col, outline="")
        for t in self.spikes:
            x = t / dur * w
            c.create_polygon(x - 4, h, x + 4, h, x, h - 7, fill=p["tags"]["warn"], outline="")
        self._draw_playhead()

    def _draw_playhead(self):
        c = self.strip
        c.delete("ph")
        if self.kind != "video" or not self.dur:
            return
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 20)
        x = (self.cur / self.fps) / self.dur * w
        c.create_line(x, 0, x, h, fill=self.app.p["text"], width=2, tags="ph")

    # ------------------------------------------------------------------ readout
    def _nearest(self):
        if not self.timeline:
            return None
        t = self.cur / self.fps
        return min(self.timeline, key=lambda e: abs(e["t"] - t))

    def _readout(self):
        p = self.app.p
        if self.kind == "image":
            if self.R:
                px = (self.R.get("pixel") or {}).get("score")
                fc = (self.R.get("faces") or {}).get("max_score")
                self.big.configure(text=_pct(px), fg=p["tags"].get(_lvl(px), p["muted"]))
                self.near_lbl.configure(text=f"AI pixel score for this image · face deepfake {_pct(fc)}")
            return
        exact = self.cache.get(self.cur)
        near = self._nearest()
        if exact:
            s = exact["ai"]
            self.big.configure(text=_pct(s), fg=p["tags"].get(_lvl(s), p["muted"]))
        elif near:
            s = near.get("ai")
            self.big.configure(text=_pct(s), fg=p["tags"].get(_lvl(s), p["muted"]))
        if near:
            dt = abs(near["t"] - self.cur / self.fps)
            where = "this frame" if dt < 0.5 / self.fps else f"nearest analysed frame {fmt_t(near['t'])}"
            self.near_lbl.configure(text=f"AI {_pct(near.get('ai'))} · face {_pct(near.get('face'))}   ({where})")
        if exact:
            per = "  ·  ".join(f"{n} {_pct(v)}" for n, v in exact["per"].items())
            face = f" · face {_pct(exact['face'])} ({exact['nfaces']})" if exact.get("face") is not None else \
                (" · no faces" if exact.get("faces_on") else "")
            self.exact_lbl.configure(text=f"This exact frame: AI {_pct(exact['ai'])}{face}   [{per}]")
        elif self._scoring:
            self.exact_lbl.configure(text="Scoring this frame…")
        else:
            self.exact_lbl.configure(text="")

    # ------------------------------------------------------------------ playback
    def toggle(self):
        if self.kind != "video" or self.cap is None:
            return
        self.pause() if self.playing else self.play()

    def play(self):
        if self.cap is None:
            return
        if self.cur >= self.n - 1:
            self._read_at(0)
        self.playing = True
        self.play_btn.configure(text="❚❚")
        self._t0, self._f0 = time.perf_counter(), self.cur
        self._tick()

    def pause(self):
        self.playing = False
        self.play_btn.configure(text="▶")
        if self._play_job:
            self.after_cancel(self._play_job)
            self._play_job = None
        if self._frame is not None:
            self._redraw()       # sharper resize when paused
            self._schedule_live()

    def _tick(self):
        self._play_job = None
        if not self.playing or self.cap is None:
            return
        target = self._f0 + int((time.perf_counter() - self._t0) * self.fps)
        if target >= self.n:
            self.pause()
            return
        if target > self.cur:
            gap = target - self.cur - 1
            if gap > 12:
                self._read_at(target)
            else:
                for _ in range(gap):
                    self.cap.grab()
                    self.cur += 1
                if not self._read_at(self.cur + 1):
                    self.pause()
                    return
        self._play_job = self.after(max(1, int(500 / self.fps)), self._tick)

    def step(self, d):
        if self.kind != "video":
            return
        self.pause()
        self._read_at(self.cur + d)

    def _grab(self, e):
        self._was_playing = self.playing
        if self.playing:
            self.pause()

    def _release(self, e):
        if self._was_playing:
            self.after(60, self.play)

    def _on_scale(self, v):
        if self._setting_scale or self.cap is None:
            return
        self._seek(int(float(v)))

    def _seek(self, idx):
        self._seek_to = idx
        if not self._seek_job:
            self._seek_job = self.after(20, self._do_seek)

    def _do_seek(self):
        self._seek_job = None
        if self._seek_to is not None and self._seek_to != self.cur:
            self._read_at(self._seek_to)
        self._seek_to = None

    def _strip_click(self, e):
        if self.kind != "video" or self.cap is None:
            return
        if self.playing:
            self._was_playing = True
            self.pause()
        w = max(self.strip.winfo_width(), 1)
        self._seek(int(max(0, min(1, e.x / w)) * (self.n - 1)))

    def _strip_release(self, e):
        if self._was_playing:
            self._was_playing = False
            self.after(60, self.play)

    # ------------------------------------------------------------------ scoring the frame on screen
    def _live_toggled(self):
        self.app.settings["live_score"] = self.live.get()
        self.app._save()
        self._schedule_live()

    def _schedule_live(self):
        if self._live_job:
            self.after_cancel(self._live_job)
            self._live_job = None
        if self.live.get() and self.kind == "video" and self.cur not in self.cache:
            self._live_job = self.after(450, lambda: self.score_now(quiet=True))

    def score_now(self, quiet=False):
        self._live_job = None
        from . import models
        if self._frame is None or self.kind != "video":
            return
        if self.app.busy:
            if not quiet:
                self.exact_lbl.configure(text="Wait for the analysis to finish first")
            return
        if not models.installed("image"):
            if not quiet:
                self.exact_lbl.configure(text="No AI models installed — open Models…")
            return
        if self._scoring or self.cur in self.cache:
            self._readout()
            return
        self._scoring = True
        idx, img, gen = self.cur, self._frame, self._gen
        self._readout()

        def work():
            res = None
            try:
                res = score_frame(img)
            except Exception:
                log("frame scoring failed\n" + traceback.format_exc())

            def done():
                self._scoring = False
                if gen != self._gen:
                    return
                if res:
                    self.cache[idx] = res
                    self._draw_strip()
                self._readout()
                if not self.playing and self.cur not in self.cache:
                    self._schedule_live()
            self.app.q.put(("call", done))
        threading.Thread(target=work, daemon=True).start()


_finder = [None, None]


def score_frame(img):
    """Score one frame with every installed model (same way the analysis scores sampled frames)."""
    from . import models
    per, names = {}, {}
    for m in models.installed("image"):
        per[m["name"]] = models.get_classifier(m["key"]).predict([img])[0]
    ai = sum(per.values()) / len(per) if per else None
    face, nfaces, faces_on = None, 0, False
    fm = models.installed("face")
    fpath = models.face_detector_path()
    if fm and fpath:
        from .video import FaceFinder
        if _finder[0] != str(fpath):
            _finder[:] = [str(fpath), FaceFinder(fpath)]
        faces_on = True
        crops = [c for _, _, c in _finder[1].find(img)]
        nfaces = len(crops)
        if crops:
            sc = []
            for m in fm:
                sc.append(models.get_classifier(m["key"]).predict(crops))
            face = max(sum(col) / len(col) for col in zip(*sc))
    short = {k.replace("(", "").replace(")", "").split()[0]: v for k, v in per.items()}
    return {"ai": ai, "per": short, "face": face, "nfaces": nfaces, "faces_on": faces_on}
