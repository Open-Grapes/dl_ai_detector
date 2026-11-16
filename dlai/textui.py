"""Text tab: paste text or drop a document (PDF, Word, PowerPoint, TXT …) and check whether it reads AI-written.

Passages are highlighted by score after analysis; hover one to see what each model said about it.
"""

import json
import os
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import charts, models, report, textio, theme
from .common import APP_NAME, APP_VERSION, log

HL = {  # passage highlight backgrounds: (light, dark)
    "ai": ("#ffd6d6", "#5c2427"),
    "warn": ("#ffe8bf", "#5a4416"),
    "lean": ("#f6f1c9", "#3d3b20"),
}


def _lvl(s):
    return "none" if s is None else "ai" if s >= 0.75 else "warn" if s >= 0.5 else "lean" if s >= 0.25 else "ok"


def _pct(s):
    return "—" if s is None else f"{s * 100:.0f}%"


class TextPage(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=(0, 6, 0, 0))
        self.app = app
        self.R = None
        self.source = None          # info dict of the loaded document, None for pasted/typed text
        self.cancel = threading.Event()
        self._stale = False

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Button(top, text="Open document…", command=self.open_doc).pack(side="left")
        ttk.Button(top, text="Paste", command=self.paste).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Clear", command=self.clear).pack(side="left", padx=(6, 0))
        self.go_btn = ttk.Button(top, text="Analyse text", style="Accent.TButton", command=self.analyze)
        self.go_btn.pack(side="left", padx=(12, 0))
        self.count_lbl = ttk.Label(top, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="left", padx=12)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(body)
        right = ttk.Frame(body, width=400)
        body.add(left, weight=1)
        body.add(right, weight=0)

        ed = ttk.Frame(left)
        ed.pack(fill="both", expand=True)
        self.ed = tk.Text(ed, wrap="word", undo=True, bd=0, highlightthickness=1, padx=14, pady=12,
                          font=(theme.UI, 11), spacing1=2, spacing3=2)
        sb = ttk.Scrollbar(ed, orient="vertical", command=self.ed.yview)
        self.ed.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.ed.pack(fill="both", expand=True)
        self.ed.bind("<<Modified>>", self._modified)
        self.hover = ttk.Label(left, text="Paste text, type, or drop a PDF / Word / PowerPoint / TXT file here.",
                               style="Muted.TLabel", anchor="w")
        self.hover.pack(fill="x", pady=(4, 0))
        try:
            from tkinterdnd2 import DND_FILES
            self.ed.drop_target_register(DND_FILES)
            self.ed.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

        r = ttk.Frame(right, padding=(12, 0, 0, 0))
        r.pack(fill="both", expand=True)
        self.v_label = tk.Label(r, text="No text analysed yet", font=(theme.UI, 15, "bold"), anchor="w",
                                justify="left", wraplength=370)
        self.v_label.pack(fill="x")
        self.v_sub = ttk.Label(r, text="", style="Muted.TLabel")
        self.v_sub.pack(fill="x", pady=(2, 8))
        self.rows = ttk.Frame(r)
        self.rows.pack(fill="x")
        self.info = tk.Text(r, wrap="word", bd=0, highlightthickness=0, padx=0, pady=6, font=(theme.UI, 10),
                            cursor="arrow", height=12)
        self.info.pack(fill="both", expand=True, pady=(8, 0))
        self.info.configure(state="disabled")

        st = ttk.Frame(r, padding=(0, 8, 0, 4))
        st.pack(side="bottom", fill="x")
        self.status = ttk.Label(st, text="Ready", style="Muted.TLabel")
        self.status.pack(fill="x")
        line = ttk.Frame(st)
        line.pack(fill="x", pady=(4, 0))
        self.pbar = ttk.Progressbar(line, maximum=1000)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ttk.Button(line, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self._render_info()
        self._count()

    # ------------------------------------------------------------------ theme
    def apply_theme(self):
        p = self.app.p
        dark = self.app.dark.get()
        self.ed.configure(bg=p["field"], fg=p["text"], insertbackground=p["text"],
                          selectbackground=p.get("select", "#cce0ff"), highlightbackground=p.get("line", p["muted"]),
                          highlightcolor=p["accent"])
        for k, (lt, dk) in HL.items():
            self.ed.tag_configure("hl_" + k, background=dk if dark else lt)
        self.ed.tag_configure("phrase", underline=True)
        self.ed.tag_configure("focus", borderwidth=1, relief="solid")
        self.info.configure(bg=p["bg"], fg=p["text"])
        self.info.tag_configure("h", font=(theme.UI, 11, "bold"), spacing1=8, spacing3=3)
        self.info.tag_configure("muted", foreground=p["muted"])
        self.info.tag_configure("small", foreground=p["faint"], font=(theme.UI, 9))
        for k, c in p["tags"].items():
            self.info.tag_configure(k, foreground=c)
        for k, (lt, dk) in HL.items():
            self.info.tag_configure("sw_" + k, background=dk if dark else lt)
        self.v_label.configure(bg=p["bg"], fg=p["tags"].get(self.R["verdict"]["level"], p["text"]) if self.R
                               else p["muted"])
        if self.R:
            self._show_rows()

    # ------------------------------------------------------------------ input
    def text(self):
        return self.ed.get("1.0", "end-1c")

    def _set_text(self, s):
        self.ed.delete("1.0", "end")
        self.ed.insert("1.0", s)
        self.ed.edit_reset()
        self.ed.edit_modified(False)
        self._clear_marks()
        self._count()

    def _modified(self, e=None):
        if not self.ed.edit_modified():
            return
        self.ed.edit_modified(False)
        if self.R and not self._stale:
            self._stale = True
            self._clear_marks()
            self.v_sub.configure(text="Text changed — analyse again to update")
        self._count()

    def _count(self):
        t = self.text()
        w = len(t.split())
        src = f" · {self.source['name']}" + (f" ({self.source['pages']} pages)" if self.source.get("pages") else "") \
            if self.source else ""
        self.count_lbl.configure(text=f"{w:,} words · {len(t):,} characters{src}")

    def paste(self):
        try:
            s = self.app.root.clipboard_get()
        except tk.TclError:
            s = ""
        if not s.strip():
            self.status.configure(text="The clipboard has no text")
            return
        self.source = None
        self._set_text(textio.tidy(s))
        self.analyze()

    def clear(self):
        self.source = None
        self.R = None
        self._set_text("")
        self.v_label.configure(text="No text analysed yet", fg=self.app.p["muted"])
        self.v_sub.configure(text="")
        for w in self.rows.winfo_children():
            w.destroy()
        self._render_info()

    def open_doc(self):
        exts = " ".join(f"*.{x}" for x in sorted(textio.TEXT_EXT))
        f = filedialog.askopenfilename(parent=self.app.root, title="Choose a document",
                                       initialdir=self.app.settings.get("last_text_dir") or str(Path.home()),
                                       filetypes=[("Documents", exts), ("PDF", "*.pdf"), ("Word", "*.docx"),
                                                  ("All files", "*.*")])
        if f:
            self.load_file(f)

    def _on_drop(self, e):
        files = self.app.root.tk.splitlist(e.data)
        if files:
            self.app.route(files[0])
        return e.action

    def load_file(self, path):
        if self.app.busy:
            messagebox.showinfo(APP_NAME, "Still analysing — wait for it to finish.", parent=self.app.root)
            return
        self.app.settings["last_text_dir"] = str(Path(path).parent)
        self.app._save()
        self.status.configure(text=f"Reading {Path(path).name}…")
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(12)

        def work():
            try:
                text, info = textio.extract(path)
                self.app.q.put(("call", lambda: self._loaded(text, info)))
            except Exception as e:
                log("text extract failed\n" + traceback.format_exc())
                msg = str(e)
                self.app.q.put(("call", lambda: self._load_failed(msg)))
        threading.Thread(target=work, daemon=True).start()

    def _stop_bar(self):
        self.pbar.stop()
        self.pbar.configure(mode="determinate")
        self.pbar["value"] = 0

    def _load_failed(self, msg):
        self._stop_bar()
        self.status.configure(text="Could not read the file")
        messagebox.showerror(APP_NAME, msg, parent=self.app.root)

    def _loaded(self, text, info):
        self._stop_bar()
        self.source = info
        self._set_text(text)
        self.analyze()

    # ------------------------------------------------------------------ analysis
    def analyze(self):
        if self.app.busy:
            return
        text = self.text()
        if not text.strip():
            self.status.configure(text="Paste or type some text first, or open a document")
            return
        if not models.installed("text"):
            ans = messagebox.askyesno(
                "No text models",
                "AI text detection needs its models first (~2.1 GB for the recommended two).\n\n"
                "Download them now?", parent=self.app.root)
            if ans:
                self.app.open_models(auto="text")
            return
        if self.app.models_dlg and self.app.models_dlg.busy:
            messagebox.showinfo(APP_NAME, "Wait for the model download/removal to finish first.", parent=self.app.root)
            return
        self.app.busy = True
        self.cancel.clear()
        self.cancel_btn.configure(state="normal")
        self.go_btn.configure(state="disabled")
        self.app.export_btn.configure(state="disabled")
        self.v_label.configure(text="Analysing…", fg=self.app.p["muted"])
        self._clear_marks()
        src = self.source

        def prog(f, s):
            self.app.q.put(("call", lambda: (self.pbar.configure(value=f * 1000), self.status.configure(text=s))))

        def work():
            try:
                from . import textdetect
                R = textdetect.analyze(text, prog, self.cancel, source=dict(src) if src else None)
                self.app.q.put(("call", lambda: self._done(R, text)))
            except Exception as e:
                log("text analyse failed\n" + traceback.format_exc())
                msg = str(e)
                self.app.q.put(("call", lambda: self._done(None, text, msg)))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, R, text, err=None):
        self.app.busy = False
        self.cancel_btn.configure(state="disabled")
        self.go_btn.configure(state="normal")
        self.pbar["value"] = 1000 if R else 0
        if err:
            self.status.configure(text="Failed")
            self.v_label.configure(text="Analysis failed", fg=self.app.p["tags"]["ai"])
            messagebox.showerror(APP_NAME, err, parent=self.app.root)
            return
        if R is None:
            self.status.configure(text="Cancelled")
            self.v_label.configure(text="Cancelled", fg=self.app.p["muted"])
            return
        if text != self.text():          # edited while analysing
            self.status.configure(text="Text changed while analysing — analyse again")
            return
        self.R = R
        self._stale = False
        self.app.export_btn.configure(state="normal")
        self.status.configure(text=f"Done in {R['elapsed_s']} s on {R['device'].upper()}")
        p = self.app.p
        v = R["verdict"]
        self.v_label.configure(text=v["label"], fg=p["tags"].get(v["level"], p["text"]))
        self.v_sub.configure(text=(f"AI likelihood {_pct(R['score'])} · {_pct(R['ai_share'])} of the text in "
                                   f"strongly-AI passages") if R["score"] is not None else "")
        self._show_rows()
        self._mark()
        self._render_info()

    def _show_rows(self):
        for w in self.rows.winfo_children():
            w.destroy()
        names = {u["key"]: u["name"] for u in self.R["models_used"]}
        for k, s in self.R["per_model"].items():
            f = ttk.Frame(self.rows)
            f.pack(fill="x", pady=2)
            ttk.Label(f, text=names.get(k, k), width=22).pack(side="left")
            pb = ttk.Progressbar(f, maximum=100, length=150)
            pb.pack(side="left", padx=6)
            pb["value"] = (s or 0) * 100
            ttk.Label(f, text=_pct(s), foreground=self.app.p["tags"].get(_lvl(s), self.app.p["muted"])).pack(side="left")

    # ------------------------------------------------------------------ highlighting
    def _clear_marks(self):
        for t in self.ed.tag_names():
            if t.startswith(("hl_", "c_")) or t in ("phrase", "focus"):
                self.ed.tag_remove(t, "1.0", "end")
        for t in [t for t in self.ed.tag_names() if t.startswith("c_")]:
            self.ed.tag_delete(t)

    def _idx(self, n):
        return f"1.0 + {n} chars"

    def _mark(self):
        self._clear_marks()
        names = {u["key"]: u["name"].split(" (")[0] for u in self.R["models_used"]}
        for i, c in enumerate(self.R["chunks"]):
            lv = _lvl(c["score"])
            a, b = self._idx(c["start"]), self._idx(c["end"])
            tag = f"c_{i}"
            self.ed.tag_add(tag, a, b)
            if lv in HL:
                self.ed.tag_add("hl_" + lv, a, b)
            per = " · ".join(f"{names.get(k, k)} {_pct(v)}" for k, v in (c.get("per_model") or {}).items())
            msg = f"Passage {i + 1}/{len(self.R['chunks'])} ({c['words']} words): AI {_pct(c['score'])}   [{per}]"
            self.ed.tag_bind(tag, "<Enter>", lambda e, m=msg, t=tag: self._hover(m, t))
            self.ed.tag_bind(tag, "<Leave>", lambda e: self._hover(None, None))
        # stock-phrase underlines
        import re
        low = self.text().lower()
        for ph in self.R["style"]["ai_phrases"]:
            for m in re.finditer(r"(?<![a-z])" + re.escape(ph), low):
                self.ed.tag_add("phrase", self._idx(m.start()), self._idx(m.end()))
        self.ed.tag_raise("phrase")

    def _hover(self, msg, tag):
        self.ed.tag_remove("focus", "1.0", "end")
        if msg:
            r = self.ed.tag_ranges(tag)
            if r:
                self.ed.tag_add("focus", r[0], r[1])
            self.hover.configure(text=msg)
        else:
            self.hover.configure(text="Hover a highlighted passage to see each model's score. "
                                      "Underlined = phrases chatbots overuse.")

    # ------------------------------------------------------------------ side panel
    def _render_info(self):
        t = self.info
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not self.R:
            t.insert("end", "How it works\n", "h")
            t.insert("end", "The text is split into passages and each is scored by every installed AI-text model "
                            "(fine-tuned transformer classifiers). Passages are highlighted by score; the overall "
                            "figure is weighted by passage length.\n", "muted")
            t.insert("end", "Works best on English prose of 150+ words. Short texts, lists, code, poetry, heavily "
                            "edited or translated text, and writing by non-native speakers are harder and give more "
                            "false alarms.\n", "small")
            t.configure(state="disabled")
            return
        R, st = self.R, self.R["style"]
        t.insert("end", "Key findings\n", "h")
        for ln in R["verdict"]["lines"]:
            t.insert("end", f"•  {ln}\n")
        for w in R["warnings"]:
            t.insert("end", f"⚠ {w}\n", "warn")
        t.insert("end", "Highlights\n", "h")
        for k, lab in (("ai", " 75%+ likely AI "), ("warn", " 50–75% uncertain "), ("lean", " 25–50% weak signal ")):
            t.insert("end", lab, "sw_" + k)
            t.insert("end", "  ")
        t.insert("end", "\nno colour = reads human\n", "small")
        t.insert("end", "Writing style (context, not proof)\n", "h")
        rows = [("Words / sentences", f"{st['words']:,} / {st['sentences']:,}"),
                ("Average sentence", f"{st['avg_sentence_words']} words" if st["avg_sentence_words"] else "—"),
                ("Sentence-length variation", f"{st['sentence_variation']}" if st["sentence_variation"] is not None
                 else "—"),
                ("Vocabulary diversity", f"{st['vocabulary_diversity']}" if st["vocabulary_diversity"] else "—"),
                ("Em dashes per 1,000 words", f"{st['em_dashes_per_1000_words']}"),
                ("Chatbot phrases per 1,000 words", f"{st['ai_phrases_per_1000_words']}")]
        for k, v in rows:
            t.insert("end", f"{k}: ", "muted")
            t.insert("end", f"{v}\n")
        t.insert("end", "People vary sentence length more (often 0.5+); chatbot prose tends to be evener. These "
                        "numbers alone can't tell AI from a careful human writer.\n", "small")
        t.configure(state="disabled")

    # ------------------------------------------------------------------ export
    def export(self):
        if not self.R:
            return
        d = filedialog.askdirectory(parent=self.app.root, title="Choose where to save the report",
                                    initialdir=self.app.settings.get("export_dir") or str(Path.home()))
        if not d:
            return
        self.app.settings["export_dir"] = d
        self.app._save()
        try:
            out = export_text(self.R, self.text(), d)
        except Exception as e:
            log("text export failed\n" + traceback.format_exc())
            messagebox.showerror(APP_NAME, f"Export failed:\n{e}", parent=self.app.root)
            return
        self.status.configure(text=f"Report saved to {out}")
        if messagebox.askyesno(APP_NAME, f"Report saved (PDF and JSON) to:\n{out}\n\nOpen the folder?",
                               parent=self.app.root):
            os.startfile(out)


def export_text(R, text, folder):
    src = R.get("source") or {}
    stem = Path(src.get("name") or "pasted_text").stem if src.get("name") != "Pasted text" else "pasted_text"
    stem += "_" + time.strftime("%Y%m%d_%H%M%S") if stem == "pasted_text" else ""
    out = Path(folder) / f"{stem}_ai_text_report"
    out.mkdir(parents=True, exist_ok=True)
    data = report._clean({"app": f"{APP_NAME} {APP_VERSION}", "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                          **R, "chunks": [{**c, "text": text[c["start"]:c["end"]]} for c in R["chunks"]]})
    (out / f"{stem}_report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    P = report._Pager()
    v = R["verdict"]
    P.text("AI text report", 34, True)
    P.text(f"{src.get('name') or 'Pasted text'} · {R['style']['words']:,} words · "
           f"{time.strftime('%Y-%m-%d %H:%M')}", 20, color="#555b66")
    P.y += 10
    P.text(v["label"], 40, True, report.LEVEL_COLOR.get(v["level"], "#1d2330"))
    for ln in v["lines"]:
        P.text("• " + ln, 21)
    names = {u["key"]: u["name"] for u in R["models_used"]}
    P.y += 10
    P.text("Models", 28, True)
    for k, s in R["per_model"].items():
        P.text(f"– {names.get(k, k)}: {_pct(s)}", 20, indent=20)
    P.y += 10
    P.text("Passages", 28, True)
    col = {"ai": "#b71c1c", "warn": "#b45309", "lean": "#6b6b1f", "ok": "#1d2330", "none": "#1d2330"}
    for i, c in enumerate(R["chunks"]):
        lv = _lvl(c["score"])
        P.text(f"Passage {i + 1} — AI {_pct(c['score'])}", 20, True, col[lv])
        P.text(" ".join(text[c["start"]:c["end"]].split()), 19, color=col[lv], indent=20)
        P.y += 6
    used = ", ".join(f"{u['name']} ({u['repo']}@{u['version']})" for u in R["models_used"]) or "none"
    P.text("Scores are probabilities from machine-learning models and can be wrong — especially on short, edited, "
           "translated or non-native writing. Don't use them as the only evidence for a decision about a person.",
           17, color="#777d88")
    P.text(f"Models: {used}", 16, color="#777d88")
    pdf = out / f"{stem}_report.pdf"
    P.pages[0].save(pdf, "PDF", resolution=150, save_all=True, append_images=P.pages[1:])
    return out


_ = charts  # charts.font is used through report._Pager
