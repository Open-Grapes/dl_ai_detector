"""Pull plain text out of documents: PDF, Word (.docx), PowerPoint (.pptx), OpenDocument, RTF, HTML, text."""

import html
import re
import zipfile
from pathlib import Path

TEXT_EXT = {"txt", "md", "markdown", "text", "csv", "tsv", "json", "log", "srt", "vtt", "html", "htm", "xml",
            "rtf", "pdf", "docx", "docm", "dotx", "pptx", "odt", "odp", "tex", "rst", "eml"}


def is_text_file(path):
    return Path(path).suffix[1:].lower() in TEXT_EXT


def extract(path):
    """-> (text, info dict). Raises RuntimeError with a friendly message when it can't."""
    p = Path(path)
    ext = p.suffix[1:].lower()
    info = {"name": p.name, "path": str(p), "size_mb": round(p.stat().st_size / 1e6, 3), "type": ext.upper()}
    if ext == "pdf":
        text, pages = _pdf(p)
        info["pages"] = pages
    elif ext in ("docx", "docm", "dotx"):
        text = _docx(p)
    elif ext == "pptx":
        text = _pptx(p)
    elif ext in ("odt", "odp"):
        text = _odf(p)
    elif ext == "rtf":
        text = _rtf(_read(p))
    elif ext in ("html", "htm", "xml"):
        text = _html(_read(p))
    elif ext in ("srt", "vtt"):
        text = _subs(_read(p))
    elif ext == "doc":
        raise RuntimeError("Old .doc files aren't supported. Open it in Word and save as .docx, or paste the text.")
    else:
        text = _read(p)
    text = tidy(text)
    if not text.strip():
        raise RuntimeError("No text found in this file" + (" (it may be a scanned PDF: images of pages, not text)"
                                                             if ext == "pdf" else ""))
    return text, info


def tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\x0c", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read(p):
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", "replace")


def _pdf(p):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF support (pypdf) is missing from this build")
    try:
        r = PdfReader(str(p))
        if r.is_encrypted:
            try:
                r.decrypt("")
            except Exception:
                raise RuntimeError("This PDF is password-protected")
        pages = []
        for pg in r.pages:
            t = pg.extract_text() or ""
            # PDF line breaks are layout, not paragraphs: re-join wrapped lines, keep blank-line paragraph breaks
            t = re.sub(r"-\n(?=[a-z])", "", t)
            t = re.sub(r"(?<![.!?:;\n])\n(?!\n)", " ", t)
            pages.append(t)
        return "\n\n".join(pages), len(r.pages)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not read this PDF: {e}")


def _xml_text(xml, para_tag, text_tag, tab_tag=None, br_tag=None):
    out = []
    for para in re.findall(rf"<{para_tag}[ >].*?</{para_tag}>|<{para_tag}/>", xml, re.S):
        if tab_tag:
            para = re.sub(rf"<{tab_tag}\s*/>", "<t>\t</t>", para)
        if br_tag:
            para = re.sub(rf"<{br_tag}\s*/>", "<t>\n</t>", para)
        parts = re.findall(rf"<{text_tag}(?:\s[^>]*)?>(.*?)</{text_tag}>", para, re.S)
        out.append(html.unescape("".join(parts)))
    return "\n\n".join(x for x in out if x.strip())


def _docx(p):
    try:
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError(f"Could not read this Word file: {e}")
    xml = re.sub(r"<w:t(?:\s[^>]*)?>", "<t>", xml).replace("</w:t>", "</t>")
    return _xml_text(xml, "w:p", "t", "w:tab", "w:br")


def _pptx(p):
    with zipfile.ZipFile(p) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        out = []
        for n in slides:
            xml = z.read(n).decode("utf-8", "replace")
            out.append(_xml_text(xml, "a:p", "a:t"))
    return "\n\n".join(x for x in out if x.strip())


def _odf(p):
    with zipfile.ZipFile(p) as z:
        xml = z.read("content.xml").decode("utf-8", "replace")
    out = []
    for m in re.findall(r"<text:(?:p|h)[ >].*?</text:(?:p|h)>", xml, re.S):
        m = re.sub(r"<text:tab\s*/>", "\t", m)
        m = re.sub(r"<text:s(?:\s[^>]*)?/>", " ", m)
        out.append(html.unescape(re.sub(r"<[^>]+>", "", m)))
    return "\n\n".join(x for x in out if x.strip())


def _rtf(s):
    s = re.sub(r"\\par[d]?\b", "\n", s)
    s = re.sub(r"\{\\\*[^{}]*\}", "", s)
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), s)
    s = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    s = s.replace("{", "").replace("}", "")
    return s


def _html(s):
    s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def _subs(s):
    lines = [ln for ln in s.splitlines()
             if ln.strip() and not re.match(r"^\d+$", ln.strip()) and "-->" not in ln and ln.strip() != "WEBVTT"]
    return " ".join(lines)
