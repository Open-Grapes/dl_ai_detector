"""Build dl_ai_detector for macOS (run on a Mac; GitHub Actions does this: .github/workflows/mac.yml).

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python build_mac.py          # -> dist/MacOS/dl_ai_detector-mac-apple-silicon.dmg

Apple Silicon only: PyTorch no longer publishes Intel-Mac builds. Makes the .app, smoke-tests the built app
(downloads a few models and analyses a test image, video and text inside it, opens the window and takes
screenshots, checks for updates), packs a .dmg, then runs the real updater against that .dmg.
"""

import json
import os
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dlai.common import APP_NAME, APP_VERSION  # noqa: E402

ARCH = "apple-silicon" if platform.machine() == "arm64" else "intel"
DIST = ROOT / "dist" / "MacOS"
WORK = ROOT / "build-mac"
SHOTS = WORK / "screenshots"
APP = WORK / "dist" / f"{APP_NAME}.app"
ICNS = ROOT / "assets" / "icon.icns"
BUNDLE_ID = "com.opengrapes.dl-ai-detector"
DMG_NAME = f"{APP_NAME}-mac-{ARCH}.dmg"
SUP = Path.home() / "Library" / "Application Support" / APP_NAME
SMOKE_MODELS = "siglip-ai,face-siglip,yunet,text-fakespot"     # small set: proves torch/transformers work


def sh(*cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def make_icns():
    if ICNS.exists():
        return
    from PIL import Image
    img = Image.open(ROOT / "assets" / "icon.png").convert("RGBA").resize((1024, 1024), Image.LANCZOS)
    img.save(ICNS, format="ICNS")


def pyinstaller():
    shutil.rmtree(WORK, ignore_errors=True)
    meta = []
    from importlib.metadata import distribution
    for pkg in ("transformers", "torch", "safetensors", "huggingface_hub", "tokenizers", "numpy", "tqdm",
                "regex", "requests", "packaging", "filelock", "pyyaml", "pillow", "c2pa-python", "av", "pypdf"):
        try:
            distribution(pkg)
            meta += ["--copy-metadata", pkg]
        except Exception:
            pass
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", APP_NAME, "--icon", ICNS, "--osx-bundle-identifier", BUNDLE_ID,
           "--add-data", f"{ROOT / 'assets' / 'icon.png'}:assets",
           "--add-data", f"{ROOT / 'dlai' / 'web_page.html'}:dlai",
           "--collect-submodules", "zeroconf", "--collect-submodules", "qrcode",
           "--collect-all", "tkinterdnd2", "--collect-all", "c2pa",
           "--collect-submodules", "av", "--collect-submodules", "pypdf", "--collect-data", "certifi",
           "--collect-data", "transformers", *meta,
           "--distpath", WORK / "dist", "--workpath", WORK / "work", "--specpath", WORK]
    for m in ("siglip", "swin", "swinv2", "vit", "auto", "modernbert", "roberta", "deberta_v2"):
        cmd += ["--collect-submodules", f"transformers.models.{m}"]
    for m in ("matplotlib", "IPython", "pytest", "tensorflow", "jax", "torchvision"):
        cmd += ["--exclude-module", m]
    sh(*cmd, ROOT / "run.py")


def fill_bundle():
    res = APP / "Contents" / "Resources"
    shutil.copy2(ROOT / "README.md", res / "README.md")
    plist_path = APP / "Contents" / "Info.plist"
    with open(plist_path, "rb") as fh:
        pl = plistlib.load(fh)
    pl.update({
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": APP_VERSION, "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        # "Open With" and dropping files on the Dock icon.
        "CFBundleDocumentTypes": [
            {"CFBundleTypeName": "Image", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": ["public.image"]},
            {"CFBundleTypeName": "Movie", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": ["public.movie"]},
            {"CFBundleTypeName": "Document", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": ["com.adobe.pdf", "public.plain-text", "public.rtf", "public.html",
                                    "org.openxmlformats.wordprocessingml.document",
                                    "org.openxmlformats.presentationml.presentation"]},
        ],
    })
    with open(plist_path, "wb") as fh:
        plistlib.dump(pl, fh)
    # Ad-hoc signature (required on Apple Silicon). Not notarized: first launch needs "Open Anyway".
    sh("codesign", "--force", "--deep", "--sign", "-", APP)
    sh("codesign", "--verify", "--deep", "--strict", APP)


DMG_README = f"""{APP_NAME} {APP_VERSION} for macOS (Apple Silicon: M1 or newer, macOS 14 Sonoma or newer)

INSTALL
1. Drag {APP_NAME} onto the Applications folder.
2. Open Applications and double-click {APP_NAME}.
   macOS says it can't verify the developer: click Done.
3. Apple menu > System Settings > Privacy & Security, scroll down to Security:
   "{APP_NAME}" was blocked... click Open Anyway, then Open Anyway again and enter your password.
   (Only needed once.)
4. The first time, the app offers to download its detection models (about 1.5 GB for images/videos,
   about 2 GB more for the text detector). They are stored for your Mac user in
   ~/Library/Application Support/{APP_NAME}

USE
- Image / Video tab: drop a photo or video (or use Open file…). You get a verdict, metadata / C2PA
  ("How this was made"), AI pixel and face scores, forensic maps, and for videos a player you can scrub
  with the AI score at the playhead.
- Text tab: paste text or drop a PDF / Word / PowerPoint / text file to check whether it reads AI-written.
- Check for updates… updates the app and the models.
"""


def make_dmg():
    DIST.mkdir(parents=True, exist_ok=True)
    stage = WORK / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    sh("ditto", APP, stage / APP.name)              # ditto keeps symlinks and signatures intact
    os.symlink("/Applications", stage / "Applications")
    (stage / "READ ME.txt").write_text(DMG_README)
    out = DIST / DMG_NAME
    out.unlink(missing_ok=True)
    sh("hdiutil", "create", "-volname", APP_NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", out)
    print(f"\nBuilt {out} ({out.stat().st_size / 1e6:.0f} MB)")
    return out


# ---------------------------------------------------------------- smoke tests on the built app

AI_TEXT = """In today's fast-paced digital landscape, effective time management has become more important than ever.
Whether you're a student juggling multiple assignments or a professional navigating the complexities of a
demanding career, mastering your schedule can unlock your full potential and foster a healthier work-life balance.

One of the most effective strategies is prioritization. By identifying your most important tasks and tackling them
first, you can ensure that your energy is directed toward activities that truly matter. Tools like the Eisenhower
Matrix can help you distinguish between what is urgent and what is important.

Additionally, it's important to note that breaks play a crucial role in maintaining productivity. In conclusion,
time management is not just about doing more, it's about doing what matters most."""


def samples(t):
    import av
    import numpy as np
    from PIL import Image, ImageDraw, PngImagePlugin
    img = Image.new("RGB", (640, 480), (40, 60, 90))
    d = ImageDraw.Draw(img)
    for i in range(12):
        d.ellipse((30 + i * 45, 80 + (i % 3) * 90, 110 + i * 45, 160 + (i % 3) * 90), fill=(200, 120 + i * 9, 80))
    meta = PngImagePlugin.PngInfo()
    meta.add_text("parameters", "a test image\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1, Model: sdxl")
    img.save(t / "sample.png", pnginfo=meta)
    with av.open(str(t / "sample.mp4"), "w") as c:
        s = c.add_stream("mpeg4", rate=24)
        s.width, s.height, s.pix_fmt = 320, 240, "yuv420p"
        for i in range(72):
            a = np.zeros((240, 320, 3), np.uint8)
            a[:, :, 0] = 60
            a[60:180, (i * 3) % 260:(i * 3) % 260 + 60] = (230, 200, 40)
            for p in s.encode(av.VideoFrame.from_ndarray(a, format="rgb24")):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    (t / "sample.txt").write_text(AI_TEXT)
    return t / "sample.png", t / "sample.mp4", t / "sample.txt"


def run_cli(binary, f):
    p = subprocess.run([str(binary), "--cli", str(f)], capture_output=True, text=True, timeout=900)
    print(p.stderr[-1500:], flush=True)
    assert p.returncode == 0, f"--cli {f.name} failed ({p.returncode})"
    return json.loads(p.stdout)


def screenshot(name):
    SHOTS.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", str(SHOTS / name)], check=False)


def window_test(binary, f, wait, name):
    p = subprocess.Popen([str(binary), str(f)])
    time.sleep(wait)
    alive = p.poll() is None
    if alive:
        subprocess.run(["osascript", "-e", f'tell application "System Events" to set frontmost of '
                                           f'(first process whose unix id is {p.pid}) to true'], check=False)
        time.sleep(2)
        screenshot(name)
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
    assert alive, f"the window closed by itself ({name})"


def smoke_test():
    binary = APP / "Contents" / "MacOS" / APP_NAME
    t = WORK / "smoke"
    shutil.rmtree(t, ignore_errors=True)
    t.mkdir(parents=True)
    png, mp4, txt = samples(t)

    sh(binary, "--download-models", SMOKE_MODELS, timeout=1800)

    R = run_cli(binary, png)
    print("image:", R["verdict"]["label"], "| pixel", R["pixel"]["score"], "| meta", R["metadata"]["verdict"], flush=True)
    assert R["metadata"]["proof"], "metadata proof not found in the test PNG"
    assert R["pixel"]["score"] is not None, "no pixel score (image models didn't run)"

    R = run_cli(binary, mp4)
    print("video:", R["verdict"]["label"], "| frames", R["video"]["sampled"], "| pixel", R["pixel"]["score"], flush=True)
    assert R["video"]["sampled"] >= 2 and R["pixel"]["score"] is not None, "video analysis incomplete"

    R = run_cli(binary, txt)
    print("text:", R["verdict"]["label"], "| score", R["score"], flush=True)
    assert R["score"] is not None and R["score"] > 0.5, f"text detector gave {R['score']} for an AI-written sample"

    # The window opens, analyses and stays up: once with a video (Preview tab), once with a document (Text tab).
    window_test(binary, mp4, 45, "1_video.png")
    window_test(binary, txt, 30, "2_text.png")

    crash = SUP / "crash.log"
    if crash.exists() and crash.read_text().strip():
        print("crash.log:\n" + crash.read_text(), flush=True)
    log = (SUP / f"{APP_NAME}.log").read_text() if (SUP / f"{APP_NAME}.log").exists() else ""
    assert "CRASH" not in log, "the app logged a crash:\n" + log[-3000:]

    # Share via Wi-Fi, end to end like a phone: upload, analyse, maps, report, pasted text, blocking, log.
    p = subprocess.run([str(binary), "--wifi-selftest", str(png), str(txt)], capture_output=True, text=True,
                       timeout=900)
    print(p.stdout[-3000:], p.stderr[-1500:], flush=True)
    assert "WIFI SELF-TEST PASSED" in p.stdout, "Share via Wi-Fi self-test failed"

    # Check for updates reaches GitHub from inside the app (certificates, network). The workflow passes the
    # build's own GitHub token, which can read this repo's releases while the repo is private.
    out = t / "update.json"
    subprocess.run([str(binary), "--check-update", str(out)], timeout=120)
    res = json.loads(out.read_text())
    print("Check for updates:", res, flush=True)
    assert "error" not in res and res.get("version"), f"update check failed: {res}"
    print("\nSMOKE TEST PASSED", flush=True)


def update_install_test(dmg):
    """Run the real updater against the new .dmg: replace a copy of the app while a process 'is' the app."""
    import threading
    from dlai import updater
    t = WORK / "update-test"
    shutil.rmtree(t, ignore_errors=True)
    t.mkdir(parents=True)
    old = t / f"{APP_NAME}.app"
    sh("ditto", APP, old)
    (old / "Contents" / "Resources" / "OLD_MARKER").write_text("old copy")
    pkg = t / dmg.name
    shutil.copy2(dmg, pkg)                     # the helper deletes the package it installed
    dummy = subprocess.Popen(["sleep", "3"])
    threading.Thread(target=dummy.wait, daemon=True).start()     # reap it, as a real app that quits disappears
    cmd, work = updater.prepare_install(pkg, dummy.pid, launch=False, loc=old)
    updater.start_helper(cmd)
    for _ in range(240):
        if (work / "finished.txt").exists():
            break
        time.sleep(0.5)
    assert (work / "finished.txt").exists(), "the update helper didn't finish"
    assert not (old / "Contents" / "Resources" / "OLD_MARKER").exists(), "the app wasn't replaced"
    assert (old / "Contents" / "MacOS" / APP_NAME).exists(), "the replaced app has no program"
    sh("codesign", "--verify", "--deep", "--strict", old)
    print("UPDATE INSTALL TEST PASSED", flush=True)


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("build_mac.py must run on macOS (GitHub Actions builds it: .github/workflows/mac.yml)")
    if platform.machine() != "arm64":
        sys.exit("dl_ai_detector for Mac needs Apple Silicon: PyTorch no longer ships Intel-Mac builds")
    make_icns()
    pyinstaller()
    fill_bundle()
    smoke_test()
    update_install_test(make_dmg())
