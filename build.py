"""Build dl_ai_detector for Windows.

    py -3.13 -m venv .venv
    .venv\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cpu
    .venv\\Scripts\\pip install -r requirements.txt
    .venv\\Scripts\\python build.py            # -> dist\\Windows\\dl_ai_detector\\ and dl_ai_detector-windows.zip

Detection models are NOT bundled: each user downloads them from inside the app (Models… button) into
%LOCALAPPDATA%\\dl_ai_detector\\models, and can update or remove them there.
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dlai.common import APP_NAME, RELEASE_ASSET  # noqa: E402

DIST = ROOT / "dist" / "Windows"
ICON = ROOT / "assets" / "icon.ico"


def make_icon():
    if ICON.exists():
        return
    from PIL import Image, ImageDraw
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(24, 28, 40))
    # a "photo" frame with a scan line and a magnifier: inspecting an image
    d.rounded_rectangle((40, 52, 176, 168), radius=14, outline=(120, 170, 255), width=10)
    d.polygon([(56, 152), (96, 104), (124, 136), (140, 118), (162, 152)], fill=(120, 170, 255))
    d.ellipse((128, 68, 150, 90), fill=(255, 210, 63))
    d.ellipse((120, 112, 210, 202), outline=(255, 90, 90), width=14)
    d.line((198, 190, 232, 224), fill=(255, 90, 90), width=20)
    d.text((140, 140), "AI", fill=(255, 255, 255))
    ICON.parent.mkdir(exist_ok=True)
    img.save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((128, 128), Image.LANCZOS).save(ICON.with_suffix(".png"))


def pyinstaller():
    meta = []
    for pkg in ("transformers", "torch", "safetensors", "huggingface_hub", "tokenizers", "numpy", "tqdm",
                "regex", "requests", "packaging", "filelock", "pyyaml", "pillow", "c2pa-python", "av"):
        try:
            from importlib.metadata import distribution
            distribution(pkg)
            meta += ["--copy-metadata", pkg]
        except Exception:
            pass
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
                           "--name", APP_NAME, "--icon", str(ICON),
                           "--add-data", f"{ICON};assets",
                           "--collect-all", "tkinterdnd2",
                           "--collect-all", "c2pa",
                           "--collect-submodules", "av",
                           "--collect-submodules", "transformers.models.siglip",
                           "--collect-submodules", "transformers.models.swin",
                           "--collect-submodules", "transformers.models.swinv2",
                           "--collect-submodules", "transformers.models.vit",
                           "--collect-submodules", "transformers.models.auto",
                           "--collect-data", "transformers",
                           *meta,
                           "--exclude-module", "matplotlib", "--exclude-module", "IPython",
                           "--exclude-module", "pytest", "--exclude-module", "tensorflow",
                           "--exclude-module", "jax", "--exclude-module", "torchvision",
                           "--distpath", str(DIST), "--workpath", str(ROOT / "build"),
                           "--specpath", str(ROOT / "build"), str(ROOT / "run.py")])


def newer_vc_runtime(internal):
    """PyInstaller copies the MSVC runtime that shipped with Python (can be older than what torch / OpenCV
    were built with). Ship the newer System32 copies instead, when they are newer."""
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        src = sys32 / name
        if not src.exists():
            continue
        for dst in internal.glob(name):
            if _ver(src) > _ver(dst):
                shutil.copy2(src, dst)
                print(f"Updated {dst.name} {_ver(dst)}")


def _ver(path):
    import ctypes
    from ctypes import wintypes
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return (0,)
    buf = ctypes.create_string_buffer(size)
    ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buf)
    p, n = ctypes.c_void_p(), wintypes.UINT()
    ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(n))
    ms, ls = ctypes.cast(p, ctypes.POINTER(wintypes.DWORD * 4)).contents[2:4]
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def assemble():
    out = DIST / APP_NAME
    newer_vc_runtime(out / "_internal")
    shutil.copy2(ROOT / "README.md", out / "README.md")
    z = DIST / RELEASE_ASSET
    z.unlink(missing_ok=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                zf.write(f, Path(APP_NAME) / f.relative_to(out))
    print(f"\nBuilt {out}\nZipped {z} ({z.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    make_icon()
    pyinstaller()
    assemble()
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    print(f"Run: {DIST / APP_NAME / (APP_NAME + '.exe')}")
