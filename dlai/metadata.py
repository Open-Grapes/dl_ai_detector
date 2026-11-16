"""Fast first pass: does the file give itself away before we look at a single pixel?

Checks C2PA Content Credentials, IPTC/XMP "digital source type", generator parameters left in PNG text
chunks / EXIF (Stable Diffusion, ComfyUI, NovelAI, InvokeAI, ...), software tags and video container tags.
"""

import json
import re
from pathlib import Path

from PIL import ExifTags, Image

GENERATORS = [
    "stable diffusion", "stablediffusion", "sdxl", "midjourney", "dall-e", "dall·e", "dalle", "openai", "chatgpt",
    "gpt-image", "sora", "firefly", "adobe firefly", "imagen", "gemini", "nano banana", "veo", "google ai",
    "synthid", "runway", "runwayml", "kling", "pika", "luma", "dream machine", "hailuo", "minimax", "flux",
    "black forest labs", "comfyui", "automatic1111", "a1111", "forge", "fooocus", "novelai", "invokeai",
    "leonardo", "ideogram", "krea", "seedream", "seedance", "bytedance", "jimeng", "dreamina", "hunyuan",
    "grok", "aurora", "xai", "recraft", "bing image creator", "image creator", "copilot designer", "designer",
    "playground ai", "nightcafe", "starryai", "artbreeder", "deepai", "stability ai", "dreamstudio",
    "civitai", "tensor.art", "pixverse", "vidu", "higgsfield", "heygen", "synthesia", "d-id", "deepfacelab",
    "faceswap", "facefusion", "roop", "deep-live-cam", "liveportrait", "wan2", "wan 2", "ltx-video",
]
# words that are too generic on their own to count as evidence
WEAK = {"designer", "forge", "image creator", "openai", "google ai", "gemini", "bytedance", "xai", "luma"}

AI_SOURCE_TYPES = {
    "trainedalgorithmicmedia": "AI-generated (trainedAlgorithmicMedia)",
    "compositewithtrainedalgorithmicmedia": "Edited with AI (compositeWithTrainedAlgorithmicMedia)",
    "algorithmicmedia": "Computer-generated (algorithmicMedia)",
    "compositesynthetic": "Composite with synthetic elements",
}

# PNG text / info keys that generators write
GEN_KEYS = {
    "parameters": "Automatic1111 / Forge / Fooocus parameters",
    "prompt": "ComfyUI prompt graph",
    "workflow": "ComfyUI workflow",
    "invokeai_metadata": "InvokeAI metadata",
    "sd-metadata": "Stable Diffusion metadata",
    "dream": "Stable Diffusion (dream) parameters",
    "generation_data": "Generation data",
    "fooocus_scheme": "Fooocus",
    "novelai": "NovelAI",
}

# tags that show which AI platform exported the file (the platform also hosts upscalers/editors, so: a hint)
PLATFORM_KEYS = {
    "hf-job-id": "file was exported by Higgsfield (AI generation platform)",
}

# model ids / tool names found in Content Credentials -> what people call them
FRIENDLY = [
    ("seedance", "ByteDance Seedance"), ("seedream", "ByteDance Seedream"),
    ("dreamina", "ByteDance Dreamina"), ("byteplus", "BytePlus ModelArk (ByteDance)"), ("jimeng", "ByteDance Jimeng"),
    ("sora", "OpenAI Sora"), ("gpt-image", "OpenAI GPT Image"), ("dall", "OpenAI DALL·E"), ("chatgpt", "OpenAI ChatGPT"),
    ("veo", "Google Veo"), ("imagen", "Google Imagen"), ("nano banana", "Google Gemini image (Nano Banana)"),
    ("gemini", "Google Gemini"), ("firefly", "Adobe Firefly"), ("kling", "Kuaishou Kling"),
    ("runway", "Runway"), ("gen-4", "Runway Gen-4"), ("hailuo", "MiniMax Hailuo"), ("minimax", "MiniMax"),
    ("midjourney", "Midjourney"), ("flux", "Black Forest Labs FLUX"), ("ideogram", "Ideogram"),
    ("luma", "Luma"), ("ray2", "Luma Ray"), ("pika", "Pika"), ("wan", "Alibaba Wan"), ("hunyuan", "Tencent Hunyuan"),
    ("higgsfield", "Higgsfield"), ("vidu", "Vidu"), ("pixverse", "PixVerse"), ("grok", "xAI Grok / Aurora"),
    ("bing", "Microsoft Bing Image Creator"), ("designer", "Microsoft Designer"), ("copilot", "Microsoft Copilot"),
    ("stable", "Stability AI Stable Diffusion"), ("leonardo", "Leonardo.Ai"), ("canva", "Canva"),
    ("photoshop", "Adobe Photoshop"), ("lightroom", "Adobe Lightroom"),
]


def friendly_model(s):
    low = (s or "").lower()
    for k, v in FRIENDLY:
        if k in low:
            ver = re.search(r"(?<![\d.])(\d+(?:[.-]\d+)?)(?![\d.])", low.split(k, 1)[1])
            return v + (f" {ver.group(1).replace('-', '.')}" if ver and k not in ("byteplus",) else "")
    return ""


def _scan_keywords(text):
    low = text.lower()
    hits = []
    for g in GENERATORS:
        if re.search(r"(?<![a-z0-9])" + re.escape(g) + r"(?![a-z0-9])", low):
            hits.append(g)
    return hits


def _short(v, n=400):
    s = v if isinstance(v, str) else repr(v)
    s = s.replace("\x00", "").strip()
    return s if len(s) <= n else s[:n] + f"... ({len(s)} chars)"


def _read_head_tail(path, n=4 << 20):
    size = Path(path).stat().st_size
    with open(path, "rb") as fh:
        if size <= 2 * n:
            return fh.read()
        head = fh.read(n)
        fh.seek(size - n)
        return head + fh.read()


def _xmp(raw):
    out = []
    for m in re.finditer(rb"<x:xmpmeta.*?</x:xmpmeta>", raw, re.S):
        try:
            out.append(m.group(0).decode("utf-8", "replace"))
        except Exception:
            pass
    return "\n".join(out)


def c2pa_check(path):
    """Returns dict or None. Uses the official c2pa library when present, else a byte-marker fallback."""
    try:
        import c2pa
        with c2pa.Reader(str(path)) as r:
            store = json.loads(r.json())
    except ImportError:
        store = None
    except Exception as e:
        msg = str(e)
        if "ManifestNotFound" in msg or "no JUMBF" in msg.lower() or "not found" in msg.lower():
            return None
        store = {"_error": msg}
    if store is None:
        raw = _read_head_tail(path)
        if b"c2pa" in raw and b"jumb" in raw:
            return {"present": True, "parsed": False, "note": "C2PA manifest bytes found but could not be read"}
        return None
    if "_error" in store:
        raw = _read_head_tail(path)
        if b"c2pa" in raw:
            return {"present": True, "parsed": False, "note": f"C2PA present but unreadable: {store['_error'][:200]}"}
        return None
    active = store.get("active_manifest")
    mans = store.get("manifests", {})
    res = {"present": True, "parsed": True, "manifests": len(mans), "generators": [], "source_types": [],
           "actions": [], "signer": None, "issues": [], "made": [],
           "state": store.get("validation_state") or ""}
    for mid, man in mans.items():
        gen = man.get("claim_generator") or ""
        for gi in man.get("claim_generator_info") or []:
            gen = (gen + " " + " ".join(str(gi.get(k, "")) for k in ("name", "version"))).strip()
        if gen:
            res["generators"].append(gen.strip())
        sig = man.get("signature_info") or {}
        if mid == active and sig:
            res["signer"] = sig.get("issuer") or sig.get("common_name")
        for a in man.get("assertions") or []:
            data = a.get("data") or {}
            for act in data.get("actions") or []:
                name = act.get("action", "")
                dst = act.get("digitalSourceType") or ""
                swd = act.get("softwareAgent") or {}
                sw = swd.get("name") if isinstance(swd, dict) else swd
                prm = act.get("parameters") or {}
                if mid == active and (dst or prm.get("model_name") or sw):
                    tool = sw or prm.get("name") or ""
                    if isinstance(swd, dict) and swd.get("version"):
                        tool += f" {swd['version']}"
                    model = prm.get("model_name") or prm.get("model") or ""
                    res["made"].append({"action": name.replace("c2pa.", ""), "tool": tool.strip(), "model": model,
                                        "model_friendly": friendly_model(model or tool),
                                        "when": act.get("when") or prm.get("time") or "",
                                        "source_type": dst.rsplit("/", 1)[-1]})
                res["actions"].append(" ".join(x for x in (name, sw or "", dst.rsplit("/", 1)[-1]) if x))
                if dst:
                    res["source_types"].append(dst.rsplit("/", 1)[-1])
            if "digitalSourceType" in json.dumps(data):
                for m in re.findall(r"digitalsourcetype/(\w+)", json.dumps(data), re.I):
                    res["source_types"].append(m)
    for st in store.get("validation_status") or []:
        res["issues"].append(st.get("code", ""))
    res["source_types"] = sorted(set(res["source_types"]))
    res["generators"] = sorted(set(res["generators"]))
    return res


def inspect_image(path):
    findings, fields = [], {}
    proof, hints, camera = [], [], False
    raw = _read_head_tail(path)

    try:
        with Image.open(path) as im:
            fields["Format"] = f"{im.format} {im.width}x{im.height} {im.mode}"
            for k, v in (im.info or {}).items():
                if k in ("exif", "icc_profile", "xmp", "dpi", "jfif", "jfif_version", "jfif_unit",
                         "jfif_density", "progressive", "progression", "adobe", "adobe_transform", "transparency",
                         "gamma", "loop", "duration", "background", "photoshop"):
                    continue
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                fields[f"Info: {k}"] = _short(v)
                kl = k.lower()
                if kl in GEN_KEYS:
                    proof.append(f"{GEN_KEYS[kl]} found in '{k}' chunk")
                elif kl in PLATFORM_KEYS:
                    hints.append(f"'{k}' tag: {PLATFORM_KEYS[kl]}")
                elif kl in ("comment", "description", "software", "source", "title", "author") and isinstance(v, str):
                    if "steps:" in v.lower() and "sampler" in v.lower():
                        proof.append(f"Generation parameters in '{k}'")
            exif = im.getexif()
            tags = {ExifTags.TAGS.get(t, str(t)): v for t, v in exif.items()}
            try:
                tags.update({ExifTags.TAGS.get(t, str(t)): v for t, v in exif.get_ifd(0x8769).items()})
            except Exception:
                pass
            for k in ("Make", "Model", "Software", "DateTime", "DateTimeOriginal", "LensModel", "Artist",
                      "ImageDescription", "UserComment", "Copyright", "HostComputer"):
                if k in tags and tags[k] not in (None, "", b""):
                    v = tags[k]
                    if isinstance(v, bytes):
                        v = v.replace(b"ASCII\x00\x00\x00", b"").replace(b"UNICODE\x00", b"").decode("utf-8", "replace")
                    fields[f"EXIF {k}"] = _short(v)
            camera = bool(tags.get("Make") or tags.get("Model"))
            uc = str(fields.get("EXIF UserComment", "")).lower()
            if "steps:" in uc and "sampler" in uc:
                proof.append("Stable Diffusion parameters in EXIF UserComment")
    except Exception as e:
        findings.append(f"Could not read image header: {e}")

    xmp = _xmp(raw)
    if xmp:
        fields["XMP"] = _short(re.sub(r"\s+", " ", xmp), 600)
        low = xmp.lower()
        for k, label in AI_SOURCE_TYPES.items():
            if re.search(r"digitalsourcetype[^<>]{0,120}" + k + r"\b", low) or f"/{k}\"" in low or f"/{k}<" in low:
                (proof if "trained" in k else hints).append(f"IPTC Digital Source Type: {label}")
                break
        for k in ("midjourney", "dall", "firefly", "imagen", "made with ai", "ai generated", "ai-generated"):
            if k in low:
                hints.append(f"XMP mentions '{k}'")

    c2 = c2pa_check(path)
    meta_text = " ".join(str(v) for v in fields.values())
    return _conclude(fields, findings, proof, hints, camera, c2, meta_text)


def inspect_video(path):
    findings, fields, proof, hints = [], {}, [], []
    try:
        import av
        with av.open(str(path)) as c:
            fields["Container"] = c.format.long_name or c.format.name
            if c.duration:
                fields["Duration"] = f"{c.duration / 1e6:.2f} s"
            if c.bit_rate:
                fields["Bit rate"] = f"{c.bit_rate / 1000:.0f} kb/s"
            for k, v in (c.metadata or {}).items():
                fields[f"Tag: {k}"] = _short(v)
            for s in c.streams:
                cc = s.codec_context
                desc = f"{s.type} {cc.name if cc else ''}"
                if s.type == "video" and cc:
                    fr = s.average_rate or s.guessed_rate
                    desc += f" {cc.width}x{cc.height} {float(fr):.2f}fps" if fr else f" {cc.width}x{cc.height}"
                fields[f"Stream #{s.index}"] = desc
                for k, v in (s.metadata or {}).items():
                    fields[f"Stream #{s.index} {k}"] = _short(v)
    except Exception as e:
        findings.append(f"Could not read container: {e}")
    enc = " ".join(v for k, v in fields.items() if "encoder" in k.lower() or "handler" in k.lower())
    if "lavf" in enc.lower() or "lavc" in enc.lower():
        findings.append("Re-encoded with FFmpeg (Lavf/Lavc) — common for downloads, edits and AI tools alike")
    raw = _read_head_tail(path, 2 << 20)
    xmp = _xmp(raw)
    if xmp:
        fields["XMP"] = _short(re.sub(r"\s+", " ", xmp), 600)
        low = xmp.lower()
        for k, label in AI_SOURCE_TYPES.items():
            if k in low:
                (proof if "trained" in k else hints).append(f"IPTC Digital Source Type: {label}")
                break
    c2 = c2pa_check(path)
    meta_text = " ".join(str(v) for v in fields.values())
    return _conclude(fields, findings, proof, hints, False, c2, meta_text)


def _made(c2):
    """'How this was made' card from Content Credentials (like Adobe's Inspect page)."""
    if not c2 or not c2.get("parsed"):
        return None
    m = next((x for x in c2["made"] if x["source_type"] or x["model"]), None) or (c2["made"] or [None])[0]
    untrusted = any("untrusted" in i.lower() for i in c2["issues"])
    broken = any(k in i.lower() for i in c2["issues"] for k in ("mismatch", "invalid", "revoked", "expired"))
    st = (m or {}).get("source_type", "")
    return {"tool": (m or {}).get("tool", "") or ", ".join(c2["generators"]),
            "model": (m or {}).get("model", ""),
            "model_friendly": (m or {}).get("model_friendly", "") or friendly_model(" ".join(c2["generators"])),
            "when": (m or {}).get("when", ""),
            "action": (m or {}).get("action", ""),
            "source_type": st,
            "source_label": AI_SOURCE_TYPES.get(st.lower(), st),
            "ai": "trained" in st.lower(),
            "app": ", ".join(c2["generators"]),
            "signer": c2.get("signer") or "",
            "trust": ("tampered or invalid signature" if broken else
                      "signed, but by a source not on the C2PA trust list" if untrusted else "signed and trusted")}


def _conclude(fields, findings, proof, hints, camera, c2, meta_text):
    made = _made(c2)
    if made:
        if made["model"] or made["model_friendly"]:
            fields["AI model (C2PA)"] = (made["model_friendly"] + (f"  [{made['model']}]" if made["model"] else "")).strip()
        if made["tool"]:
            fields["Created with (C2PA)"] = made["tool"]
        if made["when"]:
            fields["Created on (C2PA)"] = made["when"]
    if c2:
        if c2.get("parsed"):
            fields["C2PA generators"] = ", ".join(c2["generators"]) or "—"
            fields["C2PA signer"] = c2.get("signer") or "—"
            if c2["actions"]:
                fields["C2PA actions"] = _short("; ".join(c2["actions"]), 600)
            if c2["issues"]:
                fields["C2PA validation"] = ", ".join(c2["issues"])
            ai_st = [s for s in c2["source_types"] if s.lower() in AI_SOURCE_TYPES]
            trained = [s for s in ai_st if "trained" in s.lower()]
            note = (" (signer not on the C2PA trust list)"
                    if any("untrusted" in i.lower() for i in c2["issues"]) else "")
            if trained:
                by = (made or {}).get("model_friendly") or (made or {}).get("model") or (made or {}).get("tool")
                proof.append("C2PA Content Credentials declare " +
                             ", ".join(AI_SOURCE_TYPES[s.lower()] for s in trained) +
                             (f" — made with {by}" if by else "") + note)
            elif ai_st:
                hints.append("C2PA Content Credentials declare " + ", ".join(AI_SOURCE_TYPES[s.lower()] for s in ai_st)
                             + " — computer-generated/CGI, not necessarily AI" + note)
            else:
                findings.append(f"C2PA Content Credentials present (signed by {c2.get('signer') or 'unknown'}) "
                                f"without an AI source type")
            meta_text += " " + " ".join(c2["generators"]) + " " + " ".join(c2["actions"])
        else:
            hints.append(c2.get("note", "C2PA data present"))
    kw = _scan_keywords(meta_text)
    strong = [k for k in kw if k not in WEAK]
    if strong:
        hints.append("Generator names in metadata: " + ", ".join(sorted(set(strong))))
    elif kw:
        findings.append("Weak keyword matches: " + ", ".join(sorted(set(kw))))
    if camera:
        findings.append("Camera make/model present (supports a real photo, but EXIF can be copied or faked)")
    if proof:
        verdict, level = "AI-generated (metadata proof)", "ai"
    elif hints:
        verdict, level = "AI hints in metadata", "warn"
    elif camera:
        verdict, level = "Camera metadata, no AI tags", "ok"
    elif len(fields) <= 2:
        verdict, level = "Metadata stripped / empty", "none"
    else:
        verdict, level = "No AI tags found", "none"
    return {"verdict": verdict, "level": level, "proof": proof, "hints": hints, "findings": findings,
            "fields": fields, "camera": camera, "c2pa": c2, "made": made}


def inspect(path, kind):
    return inspect_video(path) if kind == "video" else inspect_image(path)
