"""Tk window: drop zone, verdict card, result tabs, and the Models manager."""

import os
import queue
import subprocess
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import charts, fileinfo, models, report, textio, theme
from .player import Player
from .textui import TextPage
from .common import (APP_NAME, APP_VERSION, IMAGE_EXT, MODELS_DIR, VIDEO_EXT, fmt_size, fmt_t, kind_of,
                     load_settings, log, resource, save_settings)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:          # pragma: no cover
    TkinterDnD = None

FORENSIC_VIEWS = {
    "original": ("Original", "The analysed image (for videos: the frame with the highest AI score)."),
    "ela": ("ELA", "Error Level Analysis: the image is re-saved as JPEG and the difference amplified. Areas that "
                   "were pasted, inpainted or generated separately often glow brighter or darker than their "
                   "surroundings. Edges and fine texture are naturally brighter — look for patches that don't fit."),
    "fft": ("FFT spectrum", "Frequency spectrum of the fine detail. Camera photos give a soft, round glow. Bright "
                            "off-centre dots, star patterns or grid lines are periodic artifacts typical of AI "
                            "upsamplers and diffusion/GAN decoders (a regular 8×8 grid can also come from JPEG)."),
    "noise": ("Noise map", "Local noise level. Real sensor grain is fairly even; AI images and retouched areas are "
                           "often unnaturally smooth, or carry a different noise texture than the rest."),
}
ICON_OK = "✓"
VIDEO_FRAMES = 48     # ~1 frame per second up to 48 s, then spread evenly over the clip


def fmt_pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


class App:
    def __init__(self, files=()):
        self.root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1320x860")
        self.root.minsize(980, 640)
        try:
            self.root.iconbitmap(default=str(resource("assets/icon.ico")))
        except Exception:
            pass
        self.settings = load_settings()
        self.dark = tk.BooleanVar(value=self.settings.get("dark", False))
        self.view = tk.StringVar(value="ela")
        self.q = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.R, self.imgs = None, {}
        self._photos = {}
        self.models_dlg = None
        self._build()
        self.apply_theme()
        self.root.after(100, self._poll)
        self.root.after(300, self.refresh_model_hint)
        if files:
            self.root.after(400, lambda: self.route(files[0]))
        else:
            self.root.after(600, self.first_run)

    # ------------------------------------------------------------------ layout
    def _build(self):
        r = self.root
        bar = ttk.Frame(r, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="Open file…", command=self.open_file).pack(side="left")
        ttk.Button(bar, text="Models…", command=self.open_models).pack(side="left", padx=(6, 0))
        self.export_btn = ttk.Button(bar, text="Export report…", command=self.export, state="disabled")
        self.export_btn.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(bar, text="Dark mode", variable=self.dark, command=self.apply_theme).pack(side="right")
        self.model_hint = ttk.Label(bar, text="", style="Muted.TLabel")
        self.model_hint.pack(side="right", padx=12)

        self.modes = ttk.Notebook(r)
        self.modes.bind("<<NotebookTabChanged>>", lambda e: self._mode_changed())
        self.modes.pack(fill="both", expand=True, padx=10, pady=(4, 6))
        media = ttk.Frame(self.modes, padding=(0, 6, 0, 0))
        self.modes.add(media, text="  Image / Video  ")
        self.textpage = TextPage(self.modes, self)
        self.modes.add(self.textpage, text="  Text  ")
        body = ttk.PanedWindow(media, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=380)
        right = ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)

        self.drop = ttk.Label(left, text="Drop an image or video here\n\nor click to open\n\n"
                                         "jpg · png · webp · mp4 · mov · mkv · webm …\n"
                                         "(PDF / Word / text files open in the Text tab)",
                              style="Drop.TLabel", anchor="center", justify="center", cursor="hand2")
        self.drop.pack(fill="x", ipady=6)
        self.drop.configure(padding=10)
        self.drop.bind("<Button-1>", lambda e: self.open_file())
        self.drop_h = 250
        self.drop.configure(width=40)
        if TkinterDnD:
            for w in (self.root, self.drop):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        card = ttk.Frame(left, padding=(2, 12, 2, 0))
        card.pack(fill="x")
        self.v_label = tk.Label(card, text="No file analysed yet", font=(theme.UI, 15, "bold"), anchor="w",
                                justify="left", wraplength=370)
        self.v_label.pack(fill="x")
        self.v_overall = ttk.Label(card, text="", style="Muted.TLabel")
        self.v_overall.pack(fill="x", pady=(2, 8))
        self.rows = {}
        for key, title in (("pixel", "AI pixel score"), ("face", "Face deepfake score"),
                           ("meta", "Metadata result"), ("comp", "Compression")):
            f = ttk.Frame(card)
            f.pack(fill="x", pady=3)
            ttk.Label(f, text=title, style="Title.TLabel").pack(anchor="w")
            line = ttk.Frame(f)
            line.pack(fill="x")
            pb = ttk.Progressbar(line, maximum=100, length=180) if key in ("pixel", "face") else None
            if pb:
                pb.pack(side="left", padx=(0, 8))
            val = ttk.Label(line, text="—", style="Muted.TLabel", wraplength=360 if not pb else 170, justify="left")
            val.pack(side="left", fill="x")
            self.rows[key] = (pb, val)

        status = ttk.Frame(left, padding=(0, 10, 0, 8))
        status.pack(side="bottom", fill="x")
        self.status = ttk.Label(status, text="Ready", style="Muted.TLabel")
        self.status.pack(fill="x")
        line = ttk.Frame(status)
        line.pack(fill="x", pady=(4, 0))
        self.pbar = ttk.Progressbar(line, maximum=1000)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(line, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        # Preview: player + file facts
        pv = ttk.Frame(nb, padding=8)
        side = ttk.Frame(pv, width=340)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        self.info_txt = self._text(side)
        self.info_txt.master.pack(fill="both", expand=True)
        self.player = Player(pv, self)
        self.player.pack(side="left", fill="both", expand=True)
        nb.add(pv, text="Preview")
        # Summary
        self.summary = self._text(nb)
        nb.add(self.summary.master, text="Summary")
        # Metadata
        mf = ttk.Frame(nb)
        self.meta_tree = ttk.Treeview(mf, columns=("v",), show="tree headings")
        self.meta_tree.heading("#0", text="Field")
        self.meta_tree.heading("v", text="Value")
        self.meta_tree.column("#0", width=230, stretch=False)
        sb = ttk.Scrollbar(mf, orient="vertical", command=self.meta_tree.yview)
        self.meta_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.meta_tree.pack(fill="both", expand=True)
        self.meta_tree.bind("<Double-1>", self._copy_meta)
        nb.add(mf, text="Metadata")
        # Forensics
        ff = ttk.Frame(nb, padding=6)
        tb = ttk.Frame(ff)
        tb.pack(fill="x")
        for k, (lab, _) in FORENSIC_VIEWS.items():
            ttk.Radiobutton(tb, text=lab, value=k, variable=self.view, style="Toolbutton",
                            command=self.show_forensic).pack(side="left", padx=(0, 4))
        self.f_info = ttk.Label(tb, text="", style="Muted.TLabel")
        self.f_info.pack(side="right")
        self.f_desc = ttk.Label(ff, text="", style="Muted.TLabel", wraplength=780, justify="left")
        self.f_desc.pack(fill="x", pady=(6, 6))
        self.f_canvas = tk.Canvas(ff, highlightthickness=0, bd=0)
        self.f_canvas.pack(fill="both", expand=True)
        self.f_canvas.bind("<Configure>", lambda e: self.show_forensic())
        nb.add(ff, text="Forensic maps")
        # Faces
        self.faces_txt = self._text(nb)
        nb.add(self.faces_txt.master, text="Faces")
        # Timeline
        self.tl_txt = self._text(nb)
        self.tl_txt.bind("<Configure>", lambda e: self._sched_timeline())
        nb.add(self.tl_txt.master, text="Video timeline")
        self._tl_after = None

    def _text(self, parent):
        fr = ttk.Frame(parent)
        t = tk.Text(fr, wrap="word", bd=0, highlightthickness=0, padx=14, pady=12, font=(theme.UI, 10),
                    cursor="arrow")
        sb = ttk.Scrollbar(fr, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        return t

    # ------------------------------------------------------------------ theme / settings
    def apply_theme(self):
        self.p = theme.apply(self.root, self.dark.get())
        p = self.p
        for t in (self.summary, self.faces_txt, self.tl_txt, self.info_txt):
            t.configure(bg=p["field"], fg=p["text"], insertbackground=p["text"], selectbackground=p.get("select", "#cce0ff"))
            t.tag_configure("h", font=(theme.UI, 12, "bold"), spacing1=10, spacing3=4)
            t.tag_configure("big", font=(theme.UI, 16, "bold"), spacing3=6)
            t.tag_configure("muted", foreground=p["muted"])
            t.tag_configure("small", foreground=p["faint"], font=(theme.UI, 9))
            for k, c in p["tags"].items():
                t.tag_configure(k, foreground=c)
        self.f_canvas.configure(bg=p["field"])
        self.info_txt.tag_configure("k", foreground=p["muted"], font=(theme.UI, 9))
        self.info_txt.tag_configure("v", font=(theme.UI, 10, "bold"), spacing3=5)
        self.player.apply_theme()
        self.textpage.apply_theme()
        self.v_label.configure(bg=p["bg"], fg=p["tags"].get(self.R["verdict"]["level"], p["text"]) if self.R else p["muted"])
        self._save()
        if self.R:
            self.render_timeline()
        if self.models_dlg and self.models_dlg.win.winfo_exists():
            theme.title_bar(self.models_dlg.win, self.dark.get())

    def _save(self):
        self.settings.update(dark=self.dark.get())
        save_settings(self.settings)

    def _mode_changed(self):
        text = self.modes.index("current") == 1
        has = self.textpage.R is not None if text else self.R is not None
        self.export_btn.configure(state="normal" if has and not self.busy else "disabled")
        if text:
            self.player.pause()

    def refresh_model_hint(self):
        n_img, n_face, n_txt = (len(models.installed(k)) for k in ("image", "face", "text"))
        if not n_img and not n_txt:
            self.model_hint.configure(text="⚠ No AI models installed — click Models…")
        else:
            dev = "GPU" if self._has_cuda() else "CPU"
            self.model_hint.configure(text=f"{n_img} image · {n_face} face · {n_txt} text model(s) · {dev}")

    def _has_cuda(self):
        if not hasattr(self, "_cuda"):
            try:
                import torch
                self._cuda = torch.cuda.is_available()
            except Exception:
                self._cuda = False
        return self._cuda

    # ------------------------------------------------------------------ input
    def first_run(self):
        self.refresh_model_hint()
        if models.installed("image") or self.settings.get("declined_models"):
            return
        rec = [m for m in models.CATALOG if m["recommended"] and m["kind"] != "text"]
        size = sum(m["size_mb"] for m in rec)
        if messagebox.askyesno(
                "Download detection models",
                "Welcome! dl_ai_detector needs its AI detection models before it can score pixels.\n\n"
                f"Download the recommended set now (~{fmt_size(size)})?\n\n"
                f"They are saved only for this Windows user in:\n{MODELS_DIR}\n\n"
                "You can add, update or remove models any time with the Models… button.", parent=self.root):
            self.open_models(auto="media")
        else:
            self.settings["declined_models"] = True
            self._save()

    def _on_drop(self, e):
        files = [f for f in self.root.tk.splitlist(e.data)]
        if files:
            self.route(files[0])

    def route(self, path):
        """Documents go to the Text tab, everything else to Image / Video."""
        if textio.is_text_file(path) or Path(path).suffix.lower() == ".doc":
            self.modes.select(self.textpage)
            self.textpage.load_file(path)
        else:
            self.modes.select(0)
            self.analyze(path)

    def open_file(self):
        if self.busy:
            return
        exts = " ".join(f"*.{x}" for x in sorted(IMAGE_EXT | VIDEO_EXT | textio.TEXT_EXT))
        f = filedialog.askopenfilename(parent=self.root, title="Choose an image, video or document",
                                       initialdir=self.settings.get("last_dir") or str(Path.home()),
                                       filetypes=[("Images, videos and documents", exts),
                                                  ("Documents", " ".join(f"*.{x}" for x in sorted(textio.TEXT_EXT))),
                                                  ("Images", " ".join(f"*.{x}" for x in sorted(IMAGE_EXT))),
                                                  ("Videos", " ".join(f"*.{x}" for x in sorted(VIDEO_EXT))),
                                                  ("All files", "*.*")])
        if f:
            self.route(f)

    def analyze(self, path):
        if self.busy:
            messagebox.showinfo(APP_NAME, "Still analysing the previous file.", parent=self.root)
            return
        path = Path(path)
        if path.is_dir() or not path.exists():
            messagebox.showerror(APP_NAME, f"Not a file:\n{path}", parent=self.root)
            return
        if self.models_dlg and self.models_dlg.busy:
            messagebox.showinfo(APP_NAME, "Wait for the model download/removal to finish first.", parent=self.root)
            return
        if not models.installed("image"):
            ans = messagebox.askyesnocancel(
                "No detection models",
                "No AI detection models are installed yet, so there will be no AI pixel score.\n\n"
                "Yes — download the recommended models first\n"
                "No — analyse metadata and forensic maps only", parent=self.root)
            if ans is None:
                return
            if ans:
                self.open_models(auto="media")
                return
        self.settings["last_dir"] = str(path.parent)
        self._save()
        self._show_preview(path)
        kind = kind_of(path) or "image"
        self.R, self.imgs = None, {}
        self.player.load(path, kind)
        self.render_info(path, kind)
        self.nb.select(0)
        self.busy = True
        self.cancel.clear()
        self.cancel_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self.v_label.configure(text="Analysing…", fg=self.p["muted"])
        self.v_overall.configure(text=path.name)
        for pb, val in self.rows.values():
            if pb:
                pb["value"] = 0
            val.configure(text="…")
        frames = VIDEO_FRAMES

        def work():
            try:
                from . import pipeline
                R, imgs = pipeline.analyze(path, frames, lambda f, s: self.q.put(("prog", f, s)), self.cancel)
                self.q.put(("done", R, imgs) if R else ("cancelled",))
            except Exception as e:
                log("analyse failed\n" + traceback.format_exc())
                self.q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, path):
        im = None
        try:
            if kind_of(path) == "video":
                import cv2
                cap = cv2.VideoCapture(str(path))
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                cap.set(cv2.CAP_PROP_POS_FRAMES, n // 3)
                ok, fr = cap.read()
                cap.release()
                if ok:
                    im = Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
            else:
                from PIL import ImageOps
                im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        except Exception:
            im = None
        if im is None:
            self.drop.configure(image="", text=path.name)
            return
        im.thumbnail((360, self.drop_h))
        self._photos["preview"] = ImageTk.PhotoImage(im)
        self.drop.configure(image=self._photos["preview"], text="", compound="center")

    # ------------------------------------------------------------------ worker messages
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "prog":
                    self.pbar["value"] = msg[1] * 1000
                    self.status.configure(text=msg[2])
                elif kind == "done":
                    self._finish()
                    self.show_result(msg[1], msg[2])
                elif kind == "cancelled":
                    self._finish("Cancelled")
                    self.v_label.configure(text="Cancelled", fg=self.p["muted"])
                elif kind == "error":
                    self._finish("Failed")
                    self.v_label.configure(text="Analysis failed", fg=self.p["tags"]["ai"])
                    messagebox.showerror(APP_NAME, msg[1], parent=self.root)
                elif kind == "call":
                    msg[1]()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _finish(self, text="Done"):
        self.busy = False
        self.cancel_btn.configure(state="disabled")
        self.status.configure(text=text)
        self.pbar["value"] = 0 if text != "Done" else 1000

    # ------------------------------------------------------------------ results
    def show_result(self, R, imgs):
        self.R, self.imgs = R, imgs
        p = self.p
        v = R["verdict"]
        self.v_label.configure(text=v["label"], fg=p["tags"].get(v["level"], p["text"]))
        ov = ("Confirmed by metadata · " if R["metadata"]["proof"] else
              f"Overall AI likelihood {fmt_pct(v['overall'])} · " if v["overall"] is not None else "")
        self.v_overall.configure(text=ov + R["file"]["name"])
        px = (R.get("pixel") or {}).get("score")
        pb, val = self.rows["pixel"]
        pb["value"] = (px or 0) * 100
        val.configure(text=fmt_pct(px) + ("" if px is not None else "  (no models)"))
        fc = R.get("faces") or {}
        pb, val = self.rows["face"]
        pb["value"] = (fc.get("max_score") or 0) * 100
        val.configure(text=(f"{fmt_pct(fc['max_score'])} max · {fc['count']} face(s)" if fc.get("max_score") is not None
                            else "no faces found" if fc.get("enabled") else "face models not installed"))
        made = R["metadata"].get("made") or {}
        by = made.get("model_friendly") or made.get("model")
        self.rows["meta"][1].configure(text=R["metadata"]["verdict"] + (f" · {by}" if by else ""),
                                       foreground=p["tags"].get(R["metadata"]["level"], p["muted"]))
        comp = (R.get("compression") or {}).get("warning")
        self.rows["comp"][1].configure(text=comp.replace("Compression warning: ", "⚠ ") if comp else "No heavy compression detected",
                                       foreground=p["tags"]["warn"] if comp else p["muted"])
        self.export_btn.configure(state="normal")
        self.status.configure(text=f"Done in {R['elapsed_s']} s on {R['device'].upper()}")
        self.player.set_results(R)
        self.render_info(R["file"]["path"], R["file"]["kind"])
        self.render_summary()
        self.render_meta()
        self.show_forensic()
        self.render_faces()
        self.render_timeline()

    def _write(self, t, chunks):
        t.configure(state="normal")
        t.delete("1.0", "end")
        for c in chunks:
            if isinstance(c, tuple) and c and c[0] == "IMG":
                t.image_create("end", image=c[1], padx=4, pady=4)
            elif isinstance(c, tuple):
                t.insert("end", *c)
            else:
                t.insert("end", c)
        t.configure(state="disabled")

    def render_summary(self):
        R = self.R
        v, m = R["verdict"], R["metadata"]
        out = [(v["label"] + "\n", ("big", v["level"]))]
        if v["overall"] is not None and not m["proof"]:
            out.append((f"Overall AI likelihood: {fmt_pct(v['overall'])}\n", "muted"))
        out.append(("Key findings\n", "h"))
        out += [f"•  {ln}\n" for ln in v["lines"]] or ["•  Nothing notable\n"]
        out.append(("Metadata & signatures\n", "h"))
        out.append((m["verdict"] + "\n", m["level"]))
        for ln in m["proof"]:
            out.append((f"  ✔ {ln}\n", "ai"))
        for ln in m["hints"]:
            out.append((f"  ! {ln}\n", "warn"))
        for ln in m["findings"]:
            out.append((f"  · {ln}\n", "muted"))
        c2 = m.get("c2pa")
        out.append((f"  C2PA Content Credentials: {'present' if c2 else 'none found'}\n", "muted"))
        made = m.get("made")
        if made and (made.get("model") or made.get("tool")):
            out.append((f"  How it was made: {made.get('model_friendly') or made.get('model') or made.get('tool')}"
                        f"{' (' + made['model'] + ')' if made.get('model') and made.get('model_friendly') else ''}"
                        f" via {made.get('tool') or '?'} · signed by {made.get('signer') or '?'} · {made['trust']}\n",
                        "ai" if made.get("ai") else "muted"))
        px = R.get("pixel") or {}
        out.append(("Pixel models\n", "h"))
        names = {u["key"]: u for u in R["models_used"]}
        if not px.get("per_model"):
            out.append(("No image models installed — open Models… to download them.\n", "warn"))
        for k, s in (px.get("per_model") or {}).items():
            lvl = "ai" if s >= 0.75 else "warn" if s >= 0.5 else "ok"
            out += [(f"  {fmt_pct(s):>5}  ", lvl), (f"{names[k]['name']}", ""), (f"  ({names[k]['repo']})\n", "small")]
        if R["file"]["kind"] == "video" and px.get("frame_mean") is not None:
            out.append((f"  Frames: mean {fmt_pct(px['frame_mean'])} · 80th pct {fmt_pct(px['frame_p80'])} · "
                        f"max {fmt_pct(px['frame_max'])} over {R['video']['sampled']} sampled frames\n", "muted"))
        fo = R.get("forensics") or {}
        if fo:
            out.append(("Forensic measures\n", "h"))
            out.append((f"  FFT peak score {fo['fft_peak_score']}  (≈1–3 typical; higher = stronger periodic patterns)\n"
                        f"  Noise unevenness {fo['noise_unevenness']}  (how much the grain level varies across the image)\n",
                        "muted"))
        for w in R.get("warnings", []):
            out.append((f"⚠ {w}\n", "warn"))
        out.append(("\nNo detector is 100% reliable. Metadata proof is strong when present, but its absence proves "
                    "nothing; pixel scores are probabilities and can be wrong on new generators, heavily compressed "
                    "files, screenshots, CGI and heavily edited photos. Use the forensic maps to check with your own "
                    "eyes.\n", "small"))
        self._write(self.summary, out)

    def render_info(self, path, kind):
        try:
            rows = fileinfo.info(path, kind)
        except Exception as e:
            rows = [("Error", str(e))]
        made = self.R["metadata"].get("made") if self.R else None
        out = [("How this was made\n", "h")]
        if not self.R:
            out.append(("Analyse the file to read its Content Credentials.\n", "muted"))
        elif made:
            if made.get("model_friendly") or made.get("model"):
                out += [("AI model\n", "k"), ((made.get("model_friendly") or made["model"]) + "\n", ("v", "ai"))]
                if made.get("model") and made.get("model_friendly"):
                    out.append((f"model id: {made['model']}\n", "small"))
            if made.get("tool"):
                out += [("Created with\n", "k"), (made["tool"] + "\n", "v")]
            if made.get("source_label"):
                out += [("Source type\n", "k"), (made["source_label"] + "\n", ("v", "ai") if made["ai"] else "v")]
            if made.get("when"):
                out += [("Created on\n", "k"), (made["when"].replace("T", " ").replace("Z", " UTC") + "\n", "v")]
            if made.get("signer"):
                out += [("Signed by\n", "k"), (made["signer"] + "\n", "v")]
            ok = made["trust"].startswith("signed and")
            out.append((("✔ " if ok else "⚠ ") + made["trust"] + "\n", "ok" if ok else "warn"))
            out.append(("Read from the file's C2PA Content Credentials.\n", "small"))
        else:
            c2 = self.R["metadata"].get("c2pa")
            out.append(("No Content Credentials (C2PA) in this file" +
                        (" (they exist but couldn't be read)" if c2 else "") +
                        ". Most apps, social sites and metadata-removal tools strip them, so their absence proves "
                        "nothing either way.\n", "muted"))
        out.append(("File information\n", "h"))
        for k, v in rows:
            out += [(k + "\n", "k"), (str(v) + "\n", "v")]
        self._write(self.info_txt, out)

    def render_meta(self):
        t = self.meta_tree
        t.delete(*t.get_children())
        for k, v in self.R["metadata"]["fields"].items():
            t.insert("", "end", text=k, values=(str(v).replace("\n", " "),))
        if not self.R["metadata"]["fields"]:
            t.insert("", "end", text="(no metadata)", values=("",))

    def _copy_meta(self, e):
        it = self.meta_tree.focus()
        if it:
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{self.meta_tree.item(it, 'text')}: {self.meta_tree.item(it, 'values')[0]}")
            self.status.configure(text="Copied to clipboard")

    def show_forensic(self):
        c = self.f_canvas
        c.delete("all")
        k = self.view.get()
        self.f_desc.configure(text=FORENSIC_VIEWS[k][1], wraplength=max(300, c.winfo_width() - 10))
        im = self.imgs.get(k) if self.imgs else None
        if im is None:
            c.create_text(c.winfo_width() // 2, 60, text="Analyse a file to see its forensic maps",
                          fill=self.p["muted"], font=(theme.UI, 11))
            return
        if self.R and "forensics_frame_t" in self.R:
            self.f_info.configure(text=f"Frame at {fmt_t(self.R['forensics_frame_t'])}")
        else:
            self.f_info.configure(text="")
        w, h = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        im = im.copy()
        im.thumbnail((w, h), Image.LANCZOS)
        self._photos["forensic"] = ImageTk.PhotoImage(im)
        c.create_image(w // 2, h // 2, image=self._photos["forensic"])

    def render_faces(self):
        fc = self.R.get("faces") or {}
        out = [("Faces\n", "h")]
        if not fc.get("enabled"):
            out.append(("Face deepfake scanning needs a face model and the face finder — install them in Models….\n", "warn"))
        elif not self.imgs.get("faces"):
            out.append(("No faces large enough were found.\n", "muted"))
        else:
            out.append((f"{fc['count']} face(s) scored; highest {fmt_pct(fc['max_score'])}. "
                        "Showing the highest-scoring crops.\n\n", "muted"))
        self._photos["faces"] = []
        for f in self.imgs.get("faces") or []:
            crop, s = f[0], f[1]
            th = crop.copy()
            th.thumbnail((150, 150))
            canvas = Image.new("RGB", (160, 186), self.p["field"] if self.p["dark"] else "#ffffff")
            canvas.paste(th, ((160 - th.width) // 2, 0))
            from PIL import ImageDraw
            d = ImageDraw.Draw(canvas)
            col = self.p["tags"]["ai" if (s or 0) >= 0.75 else "warn" if (s or 0) >= 0.5 else "ok"]
            lab = fmt_pct(s) + (f"  @ {fmt_t(f[2])}" if len(f) > 2 else "")
            d.text((80 - d.textlength(lab, font=charts.font(15, True)) / 2, 158), lab, fill=col,
                   font=charts.font(15, True))
            ph = ImageTk.PhotoImage(canvas)
            self._photos["faces"].append(ph)
            out.append(("IMG", ph))
        self._write(self.faces_txt, out)

    def _sched_timeline(self):
        if self._tl_after:
            self.root.after_cancel(self._tl_after)
        self._tl_after = self.root.after(150, self.render_timeline)

    def render_timeline(self):
        self._tl_after = None
        if not self.R:
            return
        if self.R["file"]["kind"] != "video":
            self._write(self.tl_txt, [("Video timeline\n", "h"), ("Only for video files.\n", "muted")])
            return
        dark = self.dark.get()
        w = max(500, self.tl_txt.winfo_width() - 50)
        vid = self.R["video"]
        out = [("Video timeline\n", "h"),
               (f"{vid['width']}×{vid['height']} · {vid['fps']} fps · {fmt_t(vid['duration'])} · "
                f"{vid['sampled']} frames sampled\n", "muted")]
        self._photos["tl"] = []
        if "timeline_series" in self.imgs:
            s, dur = self.imgs["timeline_series"]
            ph = ImageTk.PhotoImage(charts.timeline(s, dur, w=w, h=280, dark=dark))
            self._photos["tl"].append(ph)
            out += [("IMG", ph), "\n", ("Red band ≥75% = likely AI, amber 50–75% = uncertain. A real clip with a few "
                                        "AI scenes spliced in shows up as a jump in the line.\n", "small")]
        if "flicker_series" in self.imgs:
            s, dur, ymax = self.imgs["flicker_series"]
            ph = ImageTk.PhotoImage(charts.timeline(s, dur, w=w, h=220, dark=dark, ymax=ymax, bands=False, unit="",
                                                    title="Temporal warp residual (spikes = jitter / popping)"))
            self._photos["tl"].append(ph)
            out += ["\n", ("IMG", ph), "\n",
                    ("Each point compares a frame with the very next one after compensating for motion. "
                     "Spikes mean texture or edges changed more than the motion explains (AI warping, popping "
                     "details) — but flashes, motion blur and fast cuts can spike too.\n", "small")]
        if self.imgs.get("flicker_worst"):
            t, im = self.imgs["flicker_worst"]
            im = im.copy()
            im.thumbnail((w, 360))
            ph = ImageTk.PhotoImage(im)
            self._photos["tl"].append(ph)
            out += [(f"\nLargest residual at {fmt_t(t)} — bright areas changed without matching motion:\n", "muted"),
                    ("IMG", ph), "\n"]
        self._write(self.tl_txt, out)

    # ------------------------------------------------------------------ export / models
    def export(self):
        if self.modes.index("current") == 1:
            self.textpage.export()
            return
        if not self.R:
            return
        d = filedialog.askdirectory(parent=self.root, title="Choose where to save the report",
                                    initialdir=self.settings.get("export_dir") or str(Path(self.R["file"]["path"]).parent))
        if not d:
            return
        self.settings["export_dir"] = d
        self._save()
        try:
            out = report.export(self.R, self.imgs, d)
        except Exception as e:
            log("export failed\n" + traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Export failed:\n{e}", parent=self.root)
            return
        self.status.configure(text=f"Report saved to {out}")
        if messagebox.askyesno(APP_NAME, f"Report saved (PDF, JSON and forensic PNGs) to:\n{out}\n\nOpen the folder?",
                               parent=self.root):
            os.startfile(out)

    def open_models(self, auto=None):
        if self.models_dlg and self.models_dlg.win.winfo_exists():
            self.models_dlg.win.lift()
        else:
            self.models_dlg = ModelsDialog(self)
        if auto:
            want = {"media": lambda m: m["kind"] != "text", "text": lambda m: m["kind"] == "text"}.get(
                auto, lambda m: True)
            self.models_dlg.download([m["key"] for m in models.CATALOG if m["recommended"] and want(m)
                                      and not models.is_installed(m["key"])])

    def run(self):
        self.root.mainloop()


class ModelsDialog:
    COLS = (("type", "Type", 110), ("size", "Size", 80), ("status", "Status", 170), ("version", "Version", 110),
            ("license", "License", 170))

    def __init__(self, app):
        self.app = app
        self.busy = False
        self.cancel = threading.Event()
        self.remote = {}     # key -> (status, info)
        w = self.win = tk.Toplevel(app.root)
        w.title("Models")
        w.geometry("1100x540")
        w.transient(app.root)
        w.configure(bg=app.p["bg"])
        top = ttk.Frame(w, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Detection models", style="Title.TLabel").pack(side="left")
        self.where = ttk.Label(top, text="", style="Muted.TLabel")
        self.where.pack(side="right")

        mid = ttk.Frame(w, padding=(12, 0))
        mid.pack(fill="both", expand=True)
        t = self.tree = ttk.Treeview(mid, columns=[c[0] for c in self.COLS], show="tree headings", selectmode="extended")
        t.heading("#0", text="Model")
        t.column("#0", width=260)
        for k, lab, wd in self.COLS:
            t.heading(k, text=lab)
            t.column(k, width=wd, stretch=k == "status")
        t.pack(fill="both", expand=True)
        t.bind("<<TreeviewSelect>>", lambda e: self._describe())
        self.desc = ttk.Label(mid, text="Select a model to see what it does.", style="Muted.TLabel", wraplength=900,
                              justify="left")
        self.desc.pack(fill="x", pady=(6, 0))

        bot = ttk.Frame(w, padding=12)
        bot.pack(fill="x")
        b = ttk.Frame(bot)
        b.pack(fill="x")
        self.btns = []
        for text, cmd, style in (
                ("Download recommended", lambda: self.download([m["key"] for m in models.CATALOG if m["recommended"]
                                                                and not models.is_installed(m["key"])]), "Accent.TButton"),
                ("Download all", self.download_all, "TButton"),
                ("Download selected", lambda: self.download(self._sel(installed=False)), "TButton"),
                ("Check for updates", self.check_updates, "TButton"),
                ("Install updates", self.install_updates, "TButton"),
                ("Remove selected", self.remove_selected, "TButton"),
                ("Remove all", self.remove_all, "TButton")):
            btn = ttk.Button(b, text=text, command=cmd, style=style)
            btn.pack(side="left", padx=(0, 6))
            self.btns.append(btn)
        ttk.Button(b, text="Open folder", command=self.open_folder).pack(side="right")
        line = ttk.Frame(bot)
        line.pack(fill="x", pady=(10, 0))
        self.status = ttk.Label(line, text="", style="Muted.TLabel")
        self.status.pack(side="left")
        self.cancel_btn = ttk.Button(line, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="right")
        self.pbar = ttk.Progressbar(line, maximum=1000, length=300)
        self.pbar.pack(side="right", padx=8)
        theme.title_bar(w, app.dark.get())
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    # ---- helpers
    def _sel(self, installed=None):
        keys = list(self.tree.selection())
        if installed is not None:
            keys = [k for k in keys if models.is_installed(k) == installed]
        return keys

    def _ui(self, fn):
        self.app.q.put(("call", fn))

    def _set_busy(self, b):
        self.busy = b
        if not self.win.winfo_exists():
            return
        for x in self.btns:
            x.configure(state="disabled" if b else "normal")
        self.cancel_btn.configure(state="normal" if b else "disabled")
        if not b:
            self.pbar["value"] = 0

    def refresh(self):
        if not self.win.winfo_exists():
            return
        t = self.tree
        sel = t.selection()
        t.delete(*t.get_children())
        tags = self.app.p["tags"]
        for k in ("installed", "update", "missing"):
            t.tag_configure(k, foreground=tags[k])
        for m in models.CATALOG:
            k = m["key"]
            inst = models.is_installed(k)
            man = models.manifest(k) or {}
            rs = self.remote.get(k, (None,))[0]
            if not inst:
                status, tag = "Not installed" + (" (recommended)" if m["recommended"] else ""), "missing"
            elif rs == "update":
                info = self.remote[k][1]
                status, tag = f"Update available ({(info['last_modified'] or '')[:10]})", "update"
            elif rs == "up-to-date":
                status, tag = f"Installed · up to date {ICON_OK}", "installed"
            else:
                status, tag = "Installed", "installed"
            ver = (man.get("last_modified") or "")[:10] if inst else ""
            size = fmt_size(models.disk_usage_mb(k)) if inst else fmt_size(m["size_mb"])
            name = ("★ " if m["recommended"] else "   ") + m["name"]
            t.insert("", "end", iid=k, text=name, values=(models.KIND_LABEL[m["kind"]], size, status, ver, m["license"]),
                     tags=(tag,))
        for s in sel:
            if t.exists(s):
                t.selection_add(s)
        self.where.configure(text=f"{MODELS_DIR}   ·   using {fmt_size(models.disk_usage_mb())}")
        self.app.refresh_model_hint()

    def _describe(self):
        s = self.tree.selection()
        if s:
            m = models.BY_KEY[s[0]]
            man = models.manifest(m["key"]) or {}
            extra = f"  Installed {man.get('installed')} · revision {man.get('sha', '')[:10]}" if man else ""
            self.desc.configure(text=f"{m['about']}\nhttps://huggingface.co/{m['repo']}{extra}")

    def _run(self, label, fn):
        if self.busy:
            return
        if self.app.busy:
            messagebox.showinfo("Models", "Wait for the current analysis to finish first.", parent=self.win)
            return
        self.cancel.clear()
        self._set_busy(True)
        self.status.configure(text=label)

        def work():
            err, ok_msg = None, None
            try:
                ok_msg = fn()
            except models.Cancelled:
                err = "Cancelled"
            except Exception as e:
                log("models op failed\n" + traceback.format_exc())
                err = f"Failed: {e}"

            def done():
                self._set_busy(False)
                if self.win.winfo_exists():
                    self.status.configure(text=err or (ok_msg if isinstance(ok_msg, str) else "Done"))
                    self.refresh()
                    if err and err != "Cancelled":
                        messagebox.showerror("Models", err, parent=self.win)
            self._ui(done)
        threading.Thread(target=work, daemon=True).start()

    def _progress(self, i, n, name):
        def cb(done, total, fname):
            frac = (i + done / max(total, 1)) / n

            def ui():
                if self.win.winfo_exists():
                    self.pbar["value"] = frac * 1000
                    self.status.configure(text=f"Downloading {name} ({i + 1}/{n}) — {done / 1e6:.0f} / "
                                               f"{total / 1e6:.0f} MB")
            self._ui(ui)
        return cb

    # ---- actions
    def download(self, keys):
        if not keys:
            messagebox.showinfo("Models", "Nothing to download — select models that aren't installed yet "
                                          "(or they're all installed already).", parent=self.win)
            return

        def fn():
            for i, k in enumerate(keys):
                models.download(k, self._progress(i, len(keys), models.BY_KEY[k]["name"]), self.cancel,
                                info=(self.remote.get(k) or (None, None))[1])
                self.remote.pop(k, None)
                self._ui(self.refresh)
        self._run("Starting download…", fn)

    def download_all(self):
        keys = [m["key"] for m in models.CATALOG if not models.is_installed(m["key"])]
        if not keys:
            messagebox.showinfo("Models", "All models are already installed.", parent=self.win)
            return
        size = sum(models.BY_KEY[k]["size_mb"] for k in keys)
        nc = [models.BY_KEY[k]["name"] for k in keys if "NC" in models.BY_KEY[k]["license"]]
        note = ("\n\nNote: " + ", ".join(nc) + " is licensed for non-commercial use only.") if nc else ""
        if messagebox.askyesno("Download all models",
                               f"Download {len(keys)} model(s), about {fmt_size(size)}?\n\n"
                               "More models = a steadier average, but each one adds analysis time." + note,
                               parent=self.win):
            self.download(keys)

    def check_updates(self):
        keys = [m["key"] for m in models.CATALOG if models.is_installed(m["key"])]
        if not keys:
            messagebox.showinfo("Models", "No models installed yet.", parent=self.win)
            return

        def fn():
            n = 0
            for i, k in enumerate(keys):
                if self.cancel.is_set():
                    raise models.Cancelled()
                self._ui(lambda i=i, k=k: self.status.configure(text=f"Checking {models.BY_KEY[k]['name']}…"))
                self.remote[k] = models.check_update(k)
                n += self.remote[k][0] == "update"
            return f"{n} update(s) available — click Install updates" if n else "All installed models are up to date"
        self._run("Checking Hugging Face for newer versions…", fn)

    def install_updates(self):
        keys = [k for k, (st, _) in self.remote.items() if st == "update"]
        if not keys:
            messagebox.showinfo("Models", "No updates pending. Click 'Check for updates' first.", parent=self.win)
            return
        self.download(keys)

    def remove_selected(self):
        keys = self._sel(installed=True)
        if not keys:
            messagebox.showinfo("Models", "Select installed models to remove.", parent=self.win)
            return
        names = "\n".join("• " + models.BY_KEY[k]["name"] for k in keys)
        if not messagebox.askyesno("Remove models", f"Delete these models from this PC?\n\n{names}\n\n"
                                                    "You can download them again later.", parent=self.win):
            return
        self._run("Removing…", lambda: [models.remove(k) for k in keys])

    def remove_all(self):
        if not models.installed():
            messagebox.showinfo("Models", "No models installed.", parent=self.win)
            return
        if not messagebox.askyesno("Remove all models",
                                   f"Delete ALL downloaded models ({fmt_size(models.disk_usage_mb())}) from\n"
                                   f"{MODELS_DIR}?\n\nThe app will ask to download them again next time.",
                                   parent=self.win):
            return
        self.remote.clear()
        self._run("Removing all models…", models.remove_all)

    def open_folder(self):
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(MODELS_DIR)

    def close(self):
        if self.busy:
            if not messagebox.askyesno("Models", "A download is running. Cancel it and close?", parent=self.win):
                return
            self.cancel.set()
        self.win.destroy()
