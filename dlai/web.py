"""Share via Wi-Fi: phones, tablets and laptops on the same network open http://192.168.x.x (or
http://dl-ai-detector.local), upload a photo, video or document — or paste text — and this computer checks
whether it's AI-generated. Their browser shows the verdict, scores, "How this was made", forensic maps and the
video timeline, and offers the PDF report.

No password, but the Host header is checked (other websites can't reach it), blocked devices are turned away,
every visit is logged, a device only sees its own uploads, and uploads are kept on this computer (one folder
each, with the report) until you delete them in Admin. Jobs run one at a time, in upload order.
"""

import json
import mimetypes
import os
import queue
import secrets
import shutil
import socket
import socketserver
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import APP_NAME, APP_VERSION, IMAGE_EXT, VIDEO_EXT, kind_of, log, resource

PAGE = Path(__file__).with_name("web_page.html")
MAX_UPLOAD = 8 * 1024 ** 3
MAX_TEXT = 2_000_000                 # characters of pasted text
VIDEO_FRAMES = 48
LAN_PORTS = (80, 8778, 0)            # port 80 gives short addresses like http://192.168.0.20
MDNS_HOST = "dl-ai-detector"         # http://dl-ai-detector.local
LINGER = 8           # seconds an address keeps saying "sharing has ended" after Turn off (pages poll every ≤4 s)
BLOCKED_MSG = f"The owner of this computer has ended your access to {APP_NAME}."
BLOCKED_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{APP_NAME}</title><body style='font:17px system-ui,sans-serif;display:grid;place-items:center;"
                "min-height:90vh;margin:0;padding:16px;text-align:center;background:#f4f5f7;color:#222'>"
                "<div><div style='font-size:42px'>&#9940;</div><h2>Access ended</h2><p>" + BLOCKED_MSG + "</p></div>")
IMG_LABELS = {"original": "Analysed image", "ela": "Error Level Analysis", "fft": "Frequency spectrum (FFT)",
              "noise": "Noise map"}
_SEEN = {}


def lan_ip():
    """This computer's address on the local network (the adapter with the default route), or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))      # picks the route; nothing is sent
            ip = s.getsockname()[0]
        return None if ip.startswith(("127.", "0.")) else ip
    except OSError:
        return None


def doc_kind(name):
    """'image' / 'video' / 'text' (a document) / None for an uploaded file name."""
    from .textio import is_text_file
    k = kind_of(name)
    if k:
        return k
    return "text" if is_text_file(name) else None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second copy of the app grab the same port, and phones then reach
    # either one at random. Claim the port exclusively there instead.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port, hosts):
        self.hosts = hosts
        self.lock = threading.Lock()
        self.items = {}      # id -> dict (see Handler._new_item)
        self.jobs = queue.Queue()
        self.closed = False                   # sharing turned off: answer "ended", then hang up
        self.conns = set()                    # open browser connections (kept alive between requests)
        self.conns_lock = threading.Lock()
        super().__init__(("0.0.0.0", port), Handler)
        threading.Thread(target=self._worker, daemon=True).start()

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer's machine-name lookup (socket.getfqdn), which can hang on some networks.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        """A browser that hangs up mid-answer (closed tab, phone left the Wi-Fi) is normal."""
        import sys
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionError, TimeoutError)):
            return
        log(f"web: request from {client_address[0]} failed: {err!r}")

    def hang_up(self):
        """Turn off: cut every open browser connection, so pages notice right away."""
        self.closed = True
        self.jobs.put(None)
        with self.conns_lock:
            conns = list(self.conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ---------------------------------------------------------------- the analysis queue
    def _worker(self):
        while True:
            iid = self.jobs.get()
            if iid is None or self.closed:
                return
            with self.lock:
                it = self.items.get(iid)
            if not it or it["state"] != "wait":
                continue
            it.update(state="run", prog=0.01, msg="Starting …")
            try:
                if it["kind"] == "text":
                    _analyse_text(it)
                else:
                    _analyse_media(it)
                it.update(state="done", prog=1.0, msg="")
                v = it["result"]["verdict"]
                _log_for(it, "Analysed", f"{it['name']} · {v['label']}")
            except Exception as e:
                log("wifi analyse failed\n" + traceback.format_exc())
                it.update(state="error", msg=str(e) or "Analysis failed")
                _log_for(it, "Couldn't analyse", f"{it['name']} · {e}")


def _log_for(it, action, details=""):
    from .wifi_store import log_event
    log_event(it["ip"], it.get("ua", ""), action, details)


def _jpeg(im, path, size=1400):
    im = im.convert("RGB")
    im.thumbnail((size, size))
    im.save(path, "JPEG", quality=86)


def _analyse_media(it):
    from . import fileinfo, pipeline, report

    def prog(f, s):
        it.update(prog=max(0.01, min(0.97, f)), msg=s)

    path = Path(it["src"])
    R, imgs = pipeline.analyze(path, VIDEO_FRAMES, prog)
    d = Path(it["dir"])
    it["msg"] = "Writing the report …"
    out = report.export(R, imgs, d)
    pdf = next(Path(out).glob("*_report.pdf"), None)
    view = d / "view"
    view.mkdir(exist_ok=True)
    images = []
    for k in ("original", "ela", "fft", "noise"):
        if imgs.get(k) is not None:
            _jpeg(imgs[k], view / f"{k}.jpg")
            images.append({"key": k, "label": IMG_LABELS[k]})
    faces = []
    for i, f in enumerate((imgs.get("faces") or [])[:8]):
        _jpeg(f[0], view / f"face{i}.jpg", 300)
        faces.append({"key": f"face{i}", "score": f[1], "t": f[2] if len(f) > 2 else None})
    if imgs.get("flicker_worst"):
        _jpeg(imgs["flicker_worst"][1], view / "flicker.jpg")
    names = {u["key"]: u["name"] for u in R["models_used"]}
    px, fc, vid = R.get("pixel") or {}, R.get("faces") or {}, R.get("video") or {}
    m = R["metadata"]
    try:
        info = fileinfo.info(path, R["file"]["kind"])
    except Exception:
        info = []
    it["report"] = str(pdf) if pdf else None
    it["result"] = {
        "kind": R["file"]["kind"], "verdict": R["verdict"], "elapsed": R.get("elapsed_s"),
        "metadata": {k: m[k] for k in ("verdict", "level", "proof", "hints", "findings")},
        "made": m.get("made"),
        "pixel": {"score": px.get("score"),
                  "per": [{"name": names.get(k, k), "score": s} for k, s in (px.get("per_model") or {}).items()],
                  "hybrid": px.get("hybrid")},
        "faces": {"enabled": fc.get("enabled"), "count": fc.get("count"), "max": fc.get("max_score")},
        "faces_img": faces,
        "compression": (R.get("compression") or {}).get("warning"),
        "file": [[k, str(v)] for k, v in info if k not in ("Folder", "Modified")],
        "images": images,
        "flicker_img": bool(imgs.get("flicker_worst")),
        "forensics_t": R.get("forensics_frame_t"),
        "timeline": [{"t": e["t"], "ai": e.get("ai"), "face": e.get("face")} for e in vid.get("timeline") or []],
        "duration": vid.get("duration"),
        "spikes": vid.get("flicker_spikes_at") or [],
        "warnings": R.get("warnings") or [],
    }


def _analyse_text(it):
    from . import textdetect, textio
    from .textui import export_text
    src = Path(it["src"])
    if it.get("pasted"):
        text, info = src.read_text(encoding="utf-8"), {"name": "Pasted text"}
    else:
        it["msg"] = "Reading the document …"
        text, info = textio.extract(src)

    def prog(f, s):
        it.update(prog=max(0.01, min(0.97, f)), msg=s)

    R = textdetect.analyze(text, prog, source=info)
    it["msg"] = "Writing the report …"
    out = export_text(R, text, it["dir"])
    pdf = next(Path(out).glob("*_report.pdf"), None)
    names = {u["key"]: u["name"] for u in R["models_used"]}
    it["report"] = str(pdf) if pdf else None
    it["result"] = {
        "kind": "text", "verdict": R["verdict"], "score": R["score"], "ai_share": R["ai_share"],
        "elapsed": R.get("elapsed_s"),
        "per": [{"name": names.get(k, k), "score": s} for k, s in R["per_model"].items()],
        "chunks": [{"text": text[c["start"]:c["end"]], "score": c["score"],
                    "per": [{"name": names.get(k, k).split(" (")[0], "score": v}
                            for k, v in (c.get("per_model") or {}).items()]} for c in R["chunks"]],
        "gaps": [text[a["end"]:b["start"]] for a, b in zip(R["chunks"], R["chunks"][1:])],
        "style": R["style"], "pages": info.get("pages"), "warnings": R.get("warnings") or [],
    }


def _safe_name(name):
    name = Path(name.replace("\\", "/")).name
    name = "".join(c for c in name if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return name or "file"


def _models():
    from . import models
    return {k: len(models.installed(k)) for k in ("image", "face", "text")}


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def setup(self):
        super().setup()
        with self.server.conns_lock:
            self.server.conns.add(self.connection)

    def finish(self):
        with self.server.conns_lock:
            self.server.conns.discard(self.connection)
        try:
            super().finish()
        except OSError:
            pass

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _ip(self):
        return self.client_address[0]

    def _allowed(self):
        """The Host header must name this computer (blocks other websites via DNS rebinding)."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in self.server.hosts

    def _gate(self, path):
        """Sharing turned off, or this device blocked — say so (the page shows a notice) and stop."""
        from .wifi_store import is_blocked
        if self.server.closed:
            self.close_connection = True
            self._send(503, {"error": "Wi-Fi sharing was turned off on the computer.", "ended": True},
                       headers={"Connection": "close"})
            return True
        ip = self._ip()
        if is_blocked(ip):
            self.close_connection = True
            if time.time() - _SEEN.get("blocked " + ip, 0) > 600:
                self._log("Tried to connect (blocked)")
                _SEEN["blocked " + ip] = time.time()
            if path.startswith("/api/"):
                self._send(403, {"error": BLOCKED_MSG, "blocked": True}, headers={"Connection": "close"})
            else:
                self._send(403, BLOCKED_PAGE, "text/html; charset=utf-8", {"Connection": "close"})
            return True
        return False

    def _log(self, action, details=""):
        from .wifi_store import log_event
        log_event(self._ip(), self.headers.get("User-Agent", ""), action, details)

    def _item(self, iid):
        with self.server.lock:
            it = self.server.items.get(iid)
        return it if it and it.get("ip") == self._ip() else None      # a device only gets its own files

    def _body_json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return None

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/ping":
            return self._send(200, {"app": APP_NAME, "ok": True})
        if path in ("/icon.png", "/favicon.ico"):
            ico = resource("assets/icon.png")
            return self._send(200, ico.read_bytes() if ico.exists() else b"", "image/png",
                              {"Cache-Control": "max-age=86400"})
        if not self._allowed():
            return self._send(403, "Open the address shown in the Share via Wi-Fi window.", "text/plain")
        if self._gate(path):
            return
        if path in ("/", "/index.html"):
            if time.time() - _SEEN.get(self._ip(), 0) > 600:       # once per 10 min per device
                self._log("Opened the page")
            _SEEN[self._ip()] = time.time()
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                          "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
        if path == "/api/info":
            return self._send(200, {"app": APP_NAME, "version": APP_VERSION, "lan": True,
                                    "pc": socket.gethostname(), "models": _models()})
        if path == "/api/heartbeat":
            return self._send(200, {"ok": True})
        if path == "/api/status":
            ids = [i for i in (self._query().get("ids") or [""])[0].split(",") if i]
            out = []
            for iid in ids:
                it = self._item(iid)
                if it:
                    out.append({"id": iid, "state": it["state"], "prog": round(it["prog"], 3), "msg": it["msg"],
                                "result": it.get("result") if it["state"] == "done" else None,
                                "report": bool(it.get("report"))})
                else:
                    out.append({"id": iid, "state": "gone"})
            return self._send(200, out)
        if path.startswith("/api/img/"):
            parts = path.split("/")          # /api/img/<id>/<name>
            it = self._item(parts[3]) if len(parts) == 5 else None
            name = parts[4] if len(parts) == 5 else ""
            if not it or not name.replace(".jpg", "").isalnum():
                return self._send(404, {"error": "not found"})
            p = Path(it["dir"]) / "view" / name
            if not p.is_file():
                return self._send(404, {"error": "not found"})
            return self._send(200, p.read_bytes(), "image/jpeg", {"Cache-Control": "private, max-age=3600"})
        if path.startswith("/api/report/"):
            it = self._item(path.rsplit("/", 1)[-1])
            if not it or not it.get("report") or not Path(it["report"]).is_file():
                return self._send(404, {"error": "not found"})
            self._log("Downloaded the report", it["name"])
            return self._send_file(Path(it["report"]))
        return self._send(404, {"error": "not found"})

    def _send_file(self, p):
        size = p.stat().st_size
        quoted = urllib.parse.quote(p.name)
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(p, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self._gate(path):
            return
        if path == "/api/upload":
            return self._upload()
        if path == "/api/text":
            req = self._body_json()
            text = ((req or {}).get("text") or "").strip()
            if not text:
                return self._send(400, {"error": "No text to check."})
            if len(text) > MAX_TEXT:
                return self._send(400, {"error": "That text is too long (2 million characters at most)."})
            it = self._new_item("Pasted text", "text", ext=".txt")
            Path(it["src"]).write_text(text, encoding="utf-8")
            it["pasted"] = True
            self._log("Pasted text", f"{len(text.split()):,} words")
            return self._queue(it)
        if path == "/api/forget":
            req = self._body_json() or {}
            n = 0
            for iid in req.get("ids", []):
                if self._item(iid):
                    with self.server.lock:
                        self.server.items.pop(iid, None)
                    n += 1
            if n:
                self._log("Cleared their list", f"{n} item{'s' * (n != 1)}")
            return self._send(200, {"ok": True})       # files stay in the uploads folder until Admin deletes them
        return self._send(404, {"error": "not found"})

    def _new_item(self, name, kind, ext=""):
        from .wifi_store import new_folder
        iid = secrets.token_hex(8)
        d = new_folder(iid[:6], self._ip(), name)
        src = d / (name if not ext else "pasted" + ext)
        it = {"id": iid, "name": name, "kind": kind, "src": str(src), "dir": str(d), "ip": self._ip(),
              "ua": self.headers.get("User-Agent", ""), "state": "wait", "prog": 0.0, "msg": "Waiting …",
              "result": None, "report": None}
        return it

    def _queue(self, it):
        with self.server.lock:
            self.server.items[it["id"]] = it
            ahead = sum(1 for x in self.server.items.values() if x["state"] in ("wait", "run")) - 1
        if ahead > 0:
            it["msg"] = f"Waiting — {ahead} ahead"
        self.server.jobs.put(it["id"])
        return self._send(200, {"id": it["id"], "name": it["name"], "kind": it["kind"]})

    def _upload(self):
        from .wifi_store import human
        name = _safe_name((self._query().get("name") or ["file"])[0])
        kind = doc_kind(name)
        length = int(self.headers.get("Content-Length") or 0)
        if kind is None:
            exts = ", ".join(sorted(IMAGE_EXT | VIDEO_EXT)[:12])
            self._drain(length)
            return self._send(400, {"error": f"Can't check this type of file. Photos ({exts} …), videos, "
                                             "PDF, Word, PowerPoint and text files work."})
        if length <= 0 or length > MAX_UPLOAD:
            self._drain(length)
            return self._send(400, {"error": "The file is empty or too large."})
        it = self._new_item(name, kind)
        left = length
        with open(it["src"], "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(left, 1024 * 1024))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
        if left:
            shutil.rmtree(it["dir"], ignore_errors=True)
            return self._send(400, {"error": "The upload was interrupted."})
        self._log("Uploaded", f"{name} · {human(length)}")
        return self._queue(it)

    def _drain(self, length):
        """Read (and drop) a refused upload, so the browser gets the answer instead of a broken connection."""
        left = min(length, 64 * 1024 ** 2)
        while left > 0:
            chunk = self.rfile.read(min(left, 1024 * 1024))
            if not chunk:
                break
            left -= len(chunk)
        if length > 64 * 1024 ** 2:
            self.close_connection = True


# ============================================================================ Share via Wi-Fi (from the window)
class Sharing:
    """The running Wi-Fi server: .urls, .mdns, .stop(). Start with Sharing.start()."""

    _ending = None          # the last server, still telling open pages "sharing has ended" for a few seconds

    def __init__(self):
        self.srv = None
        self.urls = []
        self.mdns = "pending"
        self._closers = []
        self._stopped = False

    @classmethod
    def start(cls, on_change=None):
        from .wifi_store import WORK as WIFI_WORK
        self = cls()
        if cls._ending is not None:         # turned straight back on: wait for the old one to let go of the port
            cls._ending.join(LINGER + 2)
        ip = lan_ip()
        if not ip:
            raise RuntimeError("This computer isn't connected to a network.")
        WIFI_WORK.mkdir(parents=True, exist_ok=True)       # earlier uploads are kept until you delete them
        hosts = {ip, f"{MDNS_HOST}.local", f"{socket.gethostname().lower()}.local", "127.0.0.1", "localhost"}
        for port in LAN_PORTS:
            try:
                self.srv = Server(port, hosts)
                break
            except OSError:
                continue
        if self.srv is None:
            raise RuntimeError("Couldn't open a network port.")
        port = self.srv.server_address[1]
        suffix = "" if port == 80 else f":{port}"
        self.urls = [f"http://{ip}{suffix}"]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
        log(f"wifi: sharing on {self.urls[0]}")

        def announce():          # http://dl-ai-detector.local follows when mDNS is ready (can be slow or blocked)
            close = _announce(ip, port)
            if close and not self._stopped:
                self._closers.append(close)
                self.urls.append(f"http://{MDNS_HOST}.local{suffix}")
            elif close:
                close()
            self.mdns = "on" if close else "off"
            if on_change:
                on_change()
        threading.Thread(target=announce, daemon=True).start()
        return self

    def stop(self, linger=LINGER):
        """Turn off. For a few seconds the address keeps answering "sharing has ended" — then the port is
        released."""
        self._stopped = True
        if self.srv:
            self.srv.closed = True
        for c in self._closers:
            try:
                c()
            except Exception:
                pass
        srv = self.srv
        if not srv:
            return
        log("wifi: sharing off")

        def end():
            srv.hang_up()
            time.sleep(linger)
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        t = threading.Thread(target=end, daemon=True)
        t.start()
        Sharing._ending = t

    def purge(self):
        """Before "Delete all": forget every finished item. Returns the folders being worked on (kept)."""
        if not self.srv:
            return set()
        with self.srv.lock:
            busy = {it["dir"] for it in self.srv.items.values() if it["state"] in ("wait", "run")}
            for iid in [i for i, it in self.srv.items.items() if it["dir"] not in busy]:
                self.srv.items.pop(iid, None)
        return busy

    def kick(self, ip):
        """Blocked: drop that device's items (waiting work is skipped)."""
        if not self.srv:
            return
        with self.srv.lock:
            for iid in [i for i, it in self.srv.items.items() if it.get("ip") == ip]:
                it = self.srv.items.pop(iid)
                if it["state"] == "wait":
                    it["state"] = "gone"

    def counts(self):
        """(files uploaded since sharing started, devices that sent them)."""
        if not self.srv:
            return 0, 0
        with self.srv.lock:
            items = list(self.srv.items.values())
        return len(items), len({it.get("ip") for it in items})

    def busy(self):
        if not self.srv:
            return 0
        with self.srv.lock:
            return sum(1 for it in self.srv.items.values() if it["state"] in ("wait", "run"))


def _announce(ip, port):
    """Make http://dl-ai-detector.local work (mDNS / Bonjour). Returns a closer, or None."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        zc = Zeroconf(interfaces=[ip])
        info = ServiceInfo("_http._tcp.local.", f"{APP_NAME} on {socket.gethostname()}._http._tcp.local.",
                           port=port, addresses=[socket.inet_aton(ip)], server=f"{MDNS_HOST}.local.",
                           properties={"path": "/"})
        zc.register_service(info, allow_name_change=True)

        def close():
            try:
                zc.unregister_service(info)
            finally:
                zc.close()
        return close
    except Exception as e:
        log(f"mDNS: {e!r}")
        return None
