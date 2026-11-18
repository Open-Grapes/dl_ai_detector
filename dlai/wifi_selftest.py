"""Self-test for Share via Wi-Fi (used by the build checks): acts like a phone on the network.

    python -m dlai --wifi-selftest IMAGE [TEXTFILE]
"""

import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import wifi_store
from .web import Sharing

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1"
AI_TEXT = ("In today's fast-paced digital landscape, effective time management has become more important than ever. "
           "Whether you're a student juggling multiple assignments or a professional navigating the complexities of a "
           "demanding career, mastering your schedule can unlock your full potential and foster a healthier work-life "
           "balance. Additionally, it's important to note that breaks play a crucial role in maintaining productivity. "
           "In conclusion, time management is not just about doing more, it's about doing what matters most.")


def _req(base, path, data=None, ctype="application/json", host=None):
    headers = {"User-Agent": UA}
    if data is not None:
        headers["Content-Type"] = ctype
    if host:
        headers["Host"] = host
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _wait(base, iid, limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        code, body = _req(base, f"/api/status?ids={iid}")
        st = json.loads(body)[0] if code == 200 else {}
        if st.get("state") in ("done", "error", "gone"):
            return st
        time.sleep(1)
    return {"state": "timeout"}


def run(sample, text_file=None, say=print):
    sample = Path(sample)
    results = []

    def check(name, ok, info=""):
        ok = bool(ok)
        results.append(ok)
        say(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({info})" if info else ""))

    before = {p.name for p in wifi_store.folders()}
    s = Sharing.start()
    base = s.urls[0]
    ip = urllib.parse.urlsplit(base).hostname
    check("sharing started", base.startswith("http://"), base)
    try:
        from .wifi_win import qr_matrix
        m = qr_matrix(base)
        check("QR code", len(m) > 20, f"{len(m)}x{len(m)}")
        code, body = _req(base, "/")
        check("phone opens the page", code == 200 and b"dl_ai_detector" in body, f"HTTP {code}")
        code, body = _req(base, "/api/info")
        info = json.loads(body)
        check("page knows the computer", code == 200 and info.get("pc"), f"{info.get('pc')} · models {info.get('models')}")

        code, body = _req(base, "/api/upload?name=" + urllib.parse.quote(sample.name), sample.read_bytes(),
                          ctype="application/octet-stream")
        up = json.loads(body)
        check("upload a photo", code == 200 and up.get("id"), up.get("kind", body[:80]))
        st = _wait(base, up["id"])
        r = st.get("result") or {}
        check("photo analysed", st.get("state") == "done" and r.get("verdict"),
              (r.get("verdict") or {}).get("label") or st.get("msg"))
        check("forensic maps for the page", len(r.get("images") or []) >= 3, f"{len(r.get('images') or [])} maps")
        code, body = _req(base, f"/api/img/{up['id']}/ela.jpg")
        check("forensic map image", code == 200 and body[:2] == b"\xff\xd8", f"{len(body)} bytes")
        code, body = _req(base, f"/api/report/{up['id']}")
        check("PDF report download", code == 200 and body[:5] == b"%PDF-", f"{len(body)} bytes")

        code, body = _req(base, "/api/text", json.dumps({"text": AI_TEXT * 2}).encode())
        tid = json.loads(body).get("id")
        st = _wait(base, tid)
        r = st.get("result") or {}
        check("pasted text analysed", st.get("state") == "done" and r.get("chunks"),
              f"{(r.get('verdict') or {}).get('label')} · {r.get('score')}")
        if text_file:
            tf = Path(text_file)
            code, body = _req(base, "/api/upload?name=" + urllib.parse.quote(tf.name), tf.read_bytes(),
                              ctype="application/octet-stream")
            st = _wait(base, json.loads(body).get("id"))
            check("document analysed", st.get("state") == "done", (st.get("result") or {}).get("verdict", {}).get("label"))

        code, body = _req(base, "/api/upload?name=virus.exe", b"MZ" * 100, ctype="application/octet-stream")
        check("unsupported files are refused", code == 400, f"HTTP {code}")

        # another device can't see this device's results (different IP: simulated through 127.0.0.1)
        code, body = _req(base.replace(ip, "127.0.0.1"), f"/api/status?ids={up['id']}")
        check("other devices can't see it", code == 200 and json.loads(body)[0]["state"] == "gone", f"HTTP {code}")
        code, _ = _req(base, "/", host="evil.example")
        check("other websites are refused", code == 403, f"HTTP {code}")

        wifi_store.block(ip, "self-test")
        code, body = _req(base, "/api/info")
        check("blocked device is turned away", code == 403 and b"blocked" in body, f"HTTP {code}")
        wifi_store.unblock(ip)
        code, _ = _req(base, "/api/info")
        check("unblocked device is back", code == 200, f"HTTP {code}")

        time.sleep(2.5)       # activity log rows are written in the background
        rows = [r for r in wifi_store.read_log(300) if r[2] == ip]
        actions = {r[6] for r in rows}
        need = {"Opened the page", "Uploaded", "Analysed", "Pasted text", "Downloaded the report"}
        check("activity log", need <= actions, ", ".join(sorted(actions & need)))
        dev = next((r[5] for r in rows if r[5]), "")
        check("device recognised", dev.startswith("iPhone"), dev)
    finally:
        s.stop(linger=3)
    time.sleep(0.5)
    code, body = _req(base, "/api/info")
    check("turning off tells the page", code == 503 and b"ended" in body, f"HTTP {code}")

    for d in wifi_store.folders():                # clean up what the test left behind
        if d.name not in before:
            shutil.rmtree(d, ignore_errors=True)
    ok = all(results)
    say(f"\nWIFI SELF-TEST {'PASSED' if ok else 'FAILED'} ({sum(results)}/{len(results)})")
    return ok
