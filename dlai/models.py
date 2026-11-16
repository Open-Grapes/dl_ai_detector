"""Detection models: catalog, download / update check / remove, and loading for inference.

Models are never bundled with the app. Each one is downloaded from Hugging Face into
%LOCALAPPDATA%\\dl_ai_detector\\models\\<key>\\ for the current Windows user, together with a manifest.json
recording which exact files (by their content hash) were installed, so "Check for updates" only reports a
model when its weights actually changed upstream (not when someone edits the README).
"""

import gc
import json
import shutil
import threading
import time
import urllib.request
from pathlib import Path

from .common import APP_NAME, APP_VERSION, MODELS_DIR, ensure_dirs, log

HF = "https://huggingface.co"
UA = f"{APP_NAME}/{APP_VERSION}"
CLS_FILES = ("config.json", "preprocessor_config.json", "model.safetensors")

# kind: "image" = whole-image AI classifier, "face" = face-crop deepfake classifier, "detector" = face finder
CATALOG = [
    {"key": "siglip-ai", "repo": "Ateeqq/ai-vs-human-image-detector", "kind": "image",
     "name": "SigLIP AI-vs-Human", "ai_labels": ("ai",), "size_mb": 372, "recommended": True,
     "license": "Apache-2.0", "files": CLS_FILES,
     "about": "SigLIP classifier trained on modern diffusion output (Midjourney, SDXL, DALL-E, ...) vs photos."},
    {"key": "swinv2-ai", "repo": "haywoodsloan/ai-image-detector-deploy", "kind": "image",
     "name": "SwinV2 AI Image Detector", "ai_labels": ("artificial",), "size_mb": 781, "recommended": True,
     "license": "Apache-2.0", "files": CLS_FILES,
     "about": "SwinV2 classifier, regularly retrained on newer generators; robust to resizing."},
    {"key": "sdxl-swin", "repo": "Organika/sdxl-detector", "kind": "image",
     "name": "SDXL Detector (Swin)", "ai_labels": ("artificial",), "size_mb": 347, "recommended": False,
     "license": "CC-BY-NC-3.0 (non-commercial)", "files": CLS_FILES,
     "about": "Older Swin model tuned on SDXL-era images. Optional third opinion."},
    {"key": "face-siglip", "repo": "prithivMLmods/deepfake-detector-model-v1", "kind": "face",
     "name": "Face Deepfake (SigLIP)", "ai_labels": ("fake",), "size_mb": 372, "recommended": True,
     "license": "Apache-2.0", "files": CLS_FILES,
     "about": "Scores cropped faces for face-swap / synthetic-face artifacts."},
    {"key": "face-vit", "repo": "prithivMLmods/Deep-Fake-Detector-v2-Model", "kind": "face",
     "name": "Face Deepfake (ViT)", "ai_labels": ("deepfake",), "size_mb": 343, "recommended": False,
     "license": "Apache-2.0", "files": CLS_FILES,
     "about": "Second face deepfake opinion (ViT-Base)."},
    {"key": "yunet", "repo": "opencv/face_detection_yunet", "kind": "detector",
     "name": "Face finder (YuNet)", "ai_labels": (), "size_mb": 0.23, "recommended": True,
     "license": "MIT", "files": ("face_detection_yunet_2023mar.onnx",),
     "about": "Tiny OpenCV face detector used to crop faces for the deepfake models."},
]
BY_KEY = {m["key"]: m for m in CATALOG}
KIND_LABEL = {"image": "AI image", "face": "Face deepfake", "detector": "Face finder"}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- local state

def model_dir(key):
    return MODELS_DIR / key


def manifest(key):
    try:
        return json.loads((model_dir(key) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_installed(key):
    m = manifest(key)
    return bool(m) and all((model_dir(key) / f).exists() for f in BY_KEY[key]["files"])


def installed(kind=None):
    return [m for m in CATALOG if is_installed(m["key"]) and (kind is None or m["kind"] == kind)]


def disk_usage_mb(key=None):
    roots = [model_dir(key)] if key else [MODELS_DIR]
    total = 0
    for r in roots:
        if r.exists():
            total += sum(f.stat().st_size for f in r.rglob("*") if f.is_file())
    return total / 1e6


# ---------------------------------------------------------------- remote

def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def remote_info(key):
    """{'sha', 'last_modified', 'files': {name: {'id', 'size'}}} for the files this app uses."""
    spec = BY_KEY[key]
    d = _get_json(f"{HF}/api/models/{spec['repo']}?blobs=true")
    files = {}
    for s in d.get("siblings", []):
        if s["rfilename"] in spec["files"]:
            lfs = s.get("lfs") or {}
            files[s["rfilename"]] = {"id": lfs.get("sha256") or s.get("blobId"), "size": lfs.get("size") or s.get("size") or 0}
    missing = [f for f in spec["files"] if f not in files]
    if missing:
        raise RuntimeError(f"{spec['repo']} no longer has {', '.join(missing)}")
    return {"sha": d.get("sha"), "last_modified": d.get("lastModified"), "files": files}


def check_update(key):
    """Returns (status, remote_info). status: 'up-to-date' | 'update' | 'not-installed'."""
    info = remote_info(key)
    local = manifest(key)
    if not local or not is_installed(key):
        return "not-installed", info
    lf = local.get("files", {})
    changed = any(lf.get(n, {}).get("id") != v["id"] for n, v in info["files"].items())
    return ("update" if changed else "up-to-date"), info


def download(key, progress=None, cancel=None, info=None):
    """Download (or re-download) a model. progress(done_bytes, total_bytes, filename). Replaces atomically:
    files land in <key>.partial and only swap in when every file is complete, so a cancelled update keeps the
    old working copy."""
    spec = BY_KEY[key]
    info = info or remote_info(key)
    ensure_dirs()
    tmp = MODELS_DIR / f"{key}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    total = sum(v["size"] for v in info["files"].values()) or 1
    done = 0
    try:
        for name in spec["files"]:
            url = f"{HF}/{spec['repo']}/resolve/{info['sha']}/{name}"
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp / name, "wb") as fh:
                while True:
                    if cancel and cancel.is_set():
                        raise Cancelled()
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, name)
        (tmp / "manifest.json").write_text(json.dumps({
            "repo": spec["repo"], "sha": info["sha"], "last_modified": info["last_modified"],
            "files": info["files"], "installed": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2), encoding="utf-8")
        unload(key)
        dst = model_dir(key)
        if dst.exists():
            _rmtree(dst)
        tmp.rename(dst)
        log(f"installed {key} {info['sha']}")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def remove(key):
    unload(key)
    _rmtree(model_dir(key))
    shutil.rmtree(MODELS_DIR / f"{key}.partial", ignore_errors=True)
    log(f"removed {key}")


def remove_all():
    unload_all()
    if MODELS_DIR.exists():
        for p in MODELS_DIR.iterdir():
            _rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)
    log("removed all models")


def _rmtree(p):
    for _ in range(5):          # a just-released memory-mapped file can stay locked for a moment on Windows
        try:
            shutil.rmtree(p)
            return
        except FileNotFoundError:
            return
        except OSError:
            gc.collect()
            time.sleep(0.4)
    shutil.rmtree(p)


# ---------------------------------------------------------------- inference

_loaded = {}
_lock = threading.Lock()


def device():
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


class Classifier:
    def __init__(self, key):
        import torch
        from transformers import AutoImageProcessor, AutoModelForImageClassification
        from transformers.utils import logging as hf_logging
        hf_logging.set_verbosity_error()
        self.spec = BY_KEY[key]
        d = str(model_dir(key))
        self.proc = AutoImageProcessor.from_pretrained(d)
        self.model = AutoModelForImageClassification.from_pretrained(d).eval().to(device())
        labels = {int(k): v.lower() for k, v in self.model.config.id2label.items()}
        self.ai_idx = [i for i, v in labels.items() if v in self.spec["ai_labels"]]
        if not self.ai_idx:
            raise RuntimeError(f"{key}: none of {self.spec['ai_labels']} in labels {labels}")
        self.torch = torch

    def predict(self, images, batch=8):
        """images: list of RGB PIL images -> list of P(AI) floats."""
        out = []
        for i in range(0, len(images), batch):
            px = self.proc(images=images[i:i + batch], return_tensors="pt")["pixel_values"].to(device())
            with self.torch.inference_mode():
                prob = self.model(pixel_values=px).logits.float().softmax(-1)
            out += prob[:, self.ai_idx].sum(-1).tolist()
        return out


def get_classifier(key):
    with _lock:
        if key not in _loaded:
            _loaded[key] = Classifier(key)
        return _loaded[key]


def face_detector_path():
    return model_dir("yunet") / BY_KEY["yunet"]["files"][0] if is_installed("yunet") else None


def unload(key):
    with _lock:
        if _loaded.pop(key, None) is not None:
            gc.collect()


def unload_all():
    with _lock:
        _loaded.clear()
        gc.collect()
