"""(Shared with dl_transcribe.) Light and dark looks for the window (ttk). Light uses the native Windows style; dark uses 'clam' recoloured."""

import ctypes
import os
import sys
import tkinter as tk
from tkinter import ttk

IS_WIN, IS_MAC = os.name == "nt", sys.platform == "darwin"
UI = "Segoe UI" if IS_WIN else "Helvetica Neue" if IS_MAC else "DejaVu Sans"
MONO = "Consolas" if IS_WIN else "Menlo" if IS_MAC else "DejaVu Sans Mono"
_S = 0 if IS_WIN else 3   # macOS text needs to be a few points larger to look the same

LIGHT = {
    "dark": False, "bg": "SystemButtonFace" if IS_WIN else "systemWindowBackgroundColor" if IS_MAC else "#ececec", "panel": "#f3f4f6", "field": "#ffffff", "text": "#1d2330",
    "muted": "#555b66", "faint": "#777d88", "line": "#d0d4da", "accent": "#205cd6",
    "tags": {"ai": "#c62828", "warn": "#b45309", "lean": "#2f7d32", "ok": "#2e7d32", "none": "#555b66",
             "update": "#b45309", "installed": "#2e7d32", "missing": "#6b7280"},
}
DARK = {
    "dark": True, "bg": "#1c1d22", "panel": "#26282f", "field": "#202127", "text": "#e6e7ea",
    "muted": "#a3a8b3", "faint": "#7f8590", "line": "#3a3d46", "accent": "#3b82f6", "hover": "#30333b",
    "pressed": "#383c46", "select": "#2c4a7c",
    "tags": {"ai": "#ff7070", "warn": "#f5b544", "lean": "#7fd08a", "ok": "#5cc46a", "none": "#a3a8b3",
             "update": "#f5b544", "installed": "#5cc46a", "missing": "#9aa0aa"},
}


def palette(dark):
    return DARK if dark else LIGHT


def apply(root, dark):
    """Restyle every ttk widget under root. Returns the palette used."""
    p = palette(dark)
    style = ttk.Style(root)
    if dark:
        style.theme_use("clam")
        bg, panel, field, text, line = p["bg"], p["panel"], p["field"], p["text"], p["line"]
        style.configure(".", background=bg, foreground=text, fieldbackground=field, bordercolor=line,
                        darkcolor=bg, lightcolor=bg, troughcolor=panel, focuscolor=p["accent"],
                        selectbackground=p["select"], selectforeground=text, insertcolor=text,
                        font=(UI, 9 + _S))
        style.map(".", foreground=[("disabled", p["faint"])])
        style.configure("TButton", background=panel, bordercolor=line, padding=(10, 4), relief="flat")
        style.map("TButton", background=[("pressed", p["pressed"]), ("active", p["hover"]), ("disabled", bg)],
                  bordercolor=[("focus", p["accent"])])
        style.configure("Toolbutton", background=bg, padding=(10, 4), relief="flat")
        style.map("Toolbutton", background=[("selected", p["accent"]), ("active", p["hover"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("TRadiobutton", background=bg, indicatorbackground=field, indicatorforeground=text,
                        upperbordercolor=line, lowerbordercolor=line)
        style.map("TRadiobutton", background=[("active", bg)],
                  indicatorbackground=[("selected", p["accent"]), ("active", p["hover"])],
                  indicatorforeground=[("selected", "#ffffff")])
        style.configure("TCheckbutton", background=bg, indicatorbackground=field, indicatorforeground=text,
                        upperbordercolor=line, lowerbordercolor=line)
        style.map("TCheckbutton", background=[("active", bg)],
                  indicatorbackground=[("selected", p["accent"]), ("active", p["hover"])],
                  indicatorforeground=[("selected", "#ffffff")])
        style.configure("Treeview", background=field, fieldbackground=field, foreground=text, bordercolor=line)
        style.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", text)])
        style.configure("Treeview.Heading", background=panel, foreground=text, bordercolor=line, relief="flat")
        style.map("Treeview.Heading", background=[("active", p["hover"])])
        style.configure("Vertical.TScrollbar", background=panel, troughcolor=bg, bordercolor=bg,
                        arrowcolor=p["muted"], relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", p["hover"])])
        # read-only comboboxes default to a light grey field in 'clam', which made the Model box look washed out
        style.configure("TCombobox", fieldbackground=field, background=panel, foreground=text, arrowcolor=text,
                        bordercolor=line, selectbackground=field, selectforeground=text, padding=(6, 3))
        style.map("TCombobox", fieldbackground=[("readonly", field), ("disabled", bg)],
                  foreground=[("readonly", text), ("disabled", p["faint"])],
                  selectbackground=[("readonly", field), ("focus", p["select"])],
                  selectforeground=[("readonly", text)], background=[("active", p["hover"])],
                  bordercolor=[("focus", p["accent"])], arrowcolor=[("disabled", p["faint"])])
        style.configure("TEntry", fieldbackground=field, foreground=text, bordercolor=line, padding=(6, 3))
        style.map("TEntry", bordercolor=[("focus", p["accent"])])
        style.configure("Horizontal.TScale", background=p["accent"], troughcolor=panel, bordercolor=line,
                        lightcolor=panel, darkcolor=panel, gripcount=0, sliderlength=12)
        style.map("Horizontal.TScale", background=[("active", "#60a5fa")])
        style.configure("Accent.TButton", background=p["accent"], foreground="#ffffff", bordercolor=p["accent"])
        style.map("Accent.TButton", background=[("pressed", "#2563eb"), ("active", "#4f8ff7"),
                                                ("disabled", panel)],
                  foreground=[("disabled", p["faint"])])
        style.configure("TProgressbar", background=p["accent"], troughcolor=panel, bordercolor=line)
        style.configure("TNotebook", background=bg, bordercolor=line, tabmargins=(0, 2, 0, 0))
        style.configure("TNotebook.Tab", background=panel, foreground=p["muted"], bordercolor=line,
                        lightcolor=panel, padding=(12, 4))
        style.map("TNotebook.Tab", background=[("selected", field), ("active", p["hover"])],
                  foreground=[("selected", text), ("active", text)],
                  lightcolor=[("selected", field)], expand=[("selected", (1, 1, 1, 0))])
        style.configure("TPanedwindow", background=bg)
        style.configure("Sash", sashthickness=6, gripcount=0, background=bg)
        style.configure("Drop.TLabel", background=panel, bordercolor=line)
    else:
        # ttk keeps settings per theme, so the dark colours above don't leak into the native look.
        names = style.theme_names()
        style.theme_use(next(t for t in ("vista", "aqua", "winnative", "clam") if t in names))
    style.configure("Drop.TLabel", font=(UI, 13 + _S), foreground=p["muted"], anchor="center")
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("Small.TLabel", foreground=p["faint"], font=(UI, 8 + _S))
    style.configure("Title.TLabel", font=(UI, 11 + _S, "bold"))
    style.configure("Treeview", rowheight=26)
    style.configure("Time.TLabel", font=(MONO, 9 + _S), foreground=p["muted"])
    style.configure("Play.TButton", font=(UI, 11 + _S), padding=(8, 2), width=3)
    # the drop-down list of a combobox is a plain Tk listbox
    for k, v in (("background", p["field"] if dark else "#ffffff"), ("foreground", p["text"]),
                 ("selectBackground", p.get("select", "#cce0ff") if dark else "#cce0ff"),
                 ("selectForeground", p["text"])):
        root.option_add(f"*TCombobox*Listbox.{k}", v)
    root.configure(bg=p["bg"])
    for w in _toplevels(root):
        try:
            w.configure(bg=p["bg"])
        except tk.TclError:
            pass
        title_bar(w, dark)
    return p


def _toplevels(root):
    out = [root]
    for w in root.winfo_children():
        if isinstance(w, tk.Toplevel):
            out.append(w)
    return out


def title_bar(win, dark):
    """Ask Windows 10/11 to draw this window's title bar dark (or light). (macOS: follows the system.)"""
    if not IS_WIN:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
        # Redraw the frame so the change shows immediately.
        SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP)
    except Exception:
        pass
