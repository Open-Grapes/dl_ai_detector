"""AI text detection: split the text into passages, score each with every installed text model, combine.

Scores come from fine-tuned transformer classifiers (see models.CATALOG, kind "text"). Writing-style statistics
(sentence-length variation, vocabulary, stock AI phrases) are reported alongside as context, not as proof.
"""

import re
import statistics
import time

from . import models

LOW, HIGH = 0.25, 0.75
MIN_WORDS = 50            # below this, every detector is unreliable
SMALL = (70, 260)         # passages for highlighting / finding AI sections
BIG = (160, 420)          # passages for the overall score (longer = more reliable)

AI_PHRASES = [
    "delve", "delves", "delving", "tapestry", "testament to", "in today's fast-paced", "in today's digital",
    "it's important to note", "it is important to note", "it's worth noting", "it is worth noting",
    "navigate the complexities", "navigating the complexities", "ever-evolving", "ever-changing landscape",
    "in the realm of", "embark on", "a journey of", "multifaceted", "intricate", "underscores", "underscore the",
    "pivotal", "seamless", "seamlessly", "leverage", "leveraging", "harness the power", "unlock the potential",
    "foster", "fostering", "elevate", "vibrant", "showcasing", "plays a crucial role", "plays a vital role",
    "a crucial role", "in conclusion", "in summary", "overall,", "additionally,", "furthermore,", "moreover,",
    "game-changer", "cutting-edge", "nuanced", "robust", "comprehensive", "meticulous", "meticulously",
    "commendable", "noteworthy", "paramount", "holistic", "synergy", "bustling", "beacon", "resonate",
    "let's dive", "dive into", "dive deeper", "whether you're", "look no further", "rest assured",
    "i hope this helps", "as an ai", "certainly!", "great question",
]
STOP = set("the of and to a in is that it for on with as was be by this are not or at from but have an they which "
           "you we his her he she their has had were been will would can more one all there so if its about".split())


def _units(text, cmax):
    """Paragraphs (and sentences of very long paragraphs) as (start, end) character spans."""
    spans = []
    for m in re.finditer(r"\S(?:.*?\S)?(?=\s*\n\s*\n|\s*\Z)", text, re.S):
        a, b = m.span()
        if len(m.group(0).split()) <= cmax:
            spans.append((a, b))
            continue
        for s in re.finditer(r"\S.*?(?:[.!?…]+[\"'”’)\]]*(?=\s)|\Z)", m.group(0), re.S):
            spans.append((a + s.start(), a + s.end()))
    return spans


def chunks(text, size=SMALL):
    """Group units into passages of roughly size[0]..size[1] words -> list of (start, end, words)."""
    CHUNK_MIN, CHUNK_MAX = size
    out, cur, words = [], None, 0
    for a, b in _units(text, CHUNK_MAX):
        n = len(text[a:b].split())
        if cur and words + n > CHUNK_MAX and words >= CHUNK_MIN // 2:
            out.append((cur[0], cur[1], words))
            cur, words = None, 0
        cur = (cur[0], b) if cur else (a, b)
        words += n
        if words >= CHUNK_MIN:
            out.append((cur[0], cur[1], words))
            cur, words = None, 0
    if cur:
        if out and words < CHUNK_MIN // 2 and out[-1][2] + words <= CHUNK_MAX * 1.3:
            a, _, w = out.pop()
            out.append((a, cur[1], w + words))
        else:
            out.append((cur[0], cur[1], words))
    return out


def _clean(t):
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"[#*_>`|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def style(text):
    words = re.findall(r"[A-Za-z’']+", text)
    sents = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.split()) >= 3]
    lens = [len(s.split()) for s in sents]
    low = text.lower()
    found = {}
    for ph in AI_PHRASES:
        n = len(re.findall(r"(?<![a-z])" + re.escape(ph) + (r"(?![a-z])" if ph[-1].isalpha() else ""), low))
        if n:
            found[ph] = n
    wl = [w.lower() for w in words]
    win = wl[:1000]
    st = {
        "words": len(words),
        "sentences": len(sents),
        "avg_sentence_words": round(statistics.mean(lens), 1) if lens else None,
        "sentence_variation": round(statistics.pstdev(lens) / statistics.mean(lens), 2) if len(lens) >= 3 else None,
        "vocabulary_diversity": round(len(set(win)) / len(win), 2) if win else None,
        "em_dashes_per_1000_words": round(text.count("—") * 1000 / max(len(words), 1), 1),
        "ai_phrases": dict(sorted(found.items(), key=lambda kv: -kv[1])),
        "ai_phrases_per_1000_words": round(sum(found.values()) * 1000 / max(len(words), 1), 1),
        "english_ratio": round(sum(1 for w in wl if w in STOP) / max(len(wl), 1), 2),
    }
    return st


def analyze(text, progress=None, cancel=None, source=None):
    t0 = time.time()
    prog = progress or (lambda f, s: None)
    text = text.strip()
    R = {"source": source or {"name": "Pasted text"}, "models_used": [], "warnings": []}
    st = style(text)
    R["style"] = st
    small = chunks(text, SMALL)
    big = chunks(text, BIG)
    if [c[:2] for c in big] == [c[:2] for c in small]:
        big = small
    R["chunks"] = [{"start": a, "end": b, "words": w} for a, b, w in small]
    big_rows = R["chunks"] if big is small else [{"start": a, "end": b, "words": w} for a, b, w in big]
    jobs = [(c, _clean(text[c["start"]:c["end"]])) for c in R["chunks"]]
    if big is not small:
        jobs += [(c, _clean(text[c["start"]:c["end"]])) for c in big_rows]
    tms = models.installed("text")
    used = []
    for i, m in enumerate(tms):
        if cancel and cancel.is_set():
            return None
        prog(0.05 + 0.9 * i / max(1, len(tms)), f"Loading {m['name']}…")
        try:
            clf = models.get_classifier(m["key"])
        except Exception as e:
            R["warnings"].append(f"{m['name']} failed to load: {e}")
            continue
        used.append(m["key"])
        R["models_used"].append({"key": m["key"], "name": m["name"], "repo": m["repo"],
                                 "version": (models.manifest(m["key"]) or {}).get("sha", "")[:10]})
        for j in range(0, len(jobs), 4):
            if cancel and cancel.is_set():
                return None
            prog(0.05 + 0.9 * (i + j / max(1, len(jobs))) / max(1, len(tms)),
                 f"{m['name']}: passage {min(j + 4, len(jobs))}/{len(jobs)}")
            batch = jobs[j:j + 4]
            for (c, _), sc in zip(batch, clf.predict([t for _, t in batch])):
                c.setdefault("per_model", {})[m["key"]] = round(sc, 4)
    for c in R["chunks"] + ([] if big is small else big_rows):
        vals = list((c.get("per_model") or {}).values())
        c["score"] = round(sum(vals) / len(vals), 4) if vals else None

    def wmean(rows, key=None):
        rows = [r for r in rows if (r["score"] if key is None else (r.get("per_model") or {}).get(key)) is not None]
        tw = sum(r["words"] for r in rows)
        if not tw:
            return None
        return sum((r["score"] if key is None else r["per_model"][key]) * r["words"] for r in rows) / tw

    R["score"] = wmean(big_rows)
    R["per_model"] = {k: wmean(big_rows, k) for k in used}
    total = sum(c["words"] for c in R["chunks"]) or 1
    scored = [c for c in R["chunks"] if c["score"] is not None]
    R["ai_share"] = (sum(c["words"] for c in scored if c["score"] >= HIGH) / total) if scored else None
    R["verdict"] = verdict(R)
    R["elapsed_s"] = round(time.time() - t0, 1)
    R["device"] = models.device() if used else "—"
    prog(1.0, "Done")
    return R


def verdict(R):
    st, s = R["style"], R["score"]
    lines = []
    vals = list(R["per_model"].values())
    disagree = len(vals) >= 2 and max(vals) - min(vals) > 0.5
    hi = sum(c["words"] for c in R["chunks"] if (c["score"] or 0) >= HIGH)
    lo = sum(c["words"] for c in R["chunks"] if c["score"] is not None and c["score"] < 0.4)
    total = sum(c["words"] for c in R["chunks"]) or 1
    mixed = len(R["chunks"]) >= 2 and hi >= 0.15 * total and lo >= 0.15 * total
    if s is None:
        label, level = "No text models installed — open Models… to download them", "none"
    elif mixed:
        label, level = "Mixed — some passages look AI-written", "warn"
    elif s >= HIGH:
        label, level = "Likely AI-written", "ai"
    elif s >= 0.5:
        label, level = "Possibly AI — inconclusive", "warn"
    elif s >= LOW:
        label, level = "Probably human-written (weak AI signal)", "lean"
    else:
        label, level = "Likely human-written", "ok"
    if s is not None and disagree and level in ("ai", "ok"):
        label = "Possibly AI — models disagree" if level == "ai" else "Probably human (models disagree)"
        level = "warn" if level == "ai" else "lean"
    if s is not None:
        lines.append(f"AI likelihood {s * 100:.0f}% (average of {len(vals)} model{'s' * (len(vals) != 1)}, "
                     f"weighted by passage length)")
        lines.append(f"{(R['ai_share'] or 0) * 100:.0f}% of the text is in passages scored 75%+ AI "
                     f"({len(R['chunks'])} passage{'s' * (len(R['chunks']) != 1)})")
    if disagree:
        lines.append(f"Text models disagree ({min(vals) * 100:.0f}% – {max(vals) * 100:.0f}%), treat with caution")
    if st["words"] < MIN_WORDS:
        lines.append(f"Only {st['words']} words — detectors need roughly {MIN_WORDS}+ words (ideally 150+) "
                     "to say anything useful")
        if s is not None:
            label = "Too short to judge reliably — " + ("leans AI" if s >= 0.5 else "leans human")
            level = "warn" if s >= 0.5 else "lean"
    if st["english_ratio"] < 0.18 and st["words"] >= 20:
        lines.append("This doesn't look like English: these detectors are trained on English text, so scores "
                     "for other languages are unreliable")
    if st["ai_phrases"]:
        top = ", ".join(f"“{k}”" + (f" ×{v}" if v > 1 else "") for k, v in list(st["ai_phrases"].items())[:6])
        lines.append(f"Stock phrases often overused by chatbots: {top}  (style hint only)")
    return {"label": label, "level": level, "lines": lines, "disagree": disagree, "mixed": mixed}
