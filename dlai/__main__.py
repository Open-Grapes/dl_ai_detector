"""dl_ai_detector entry point.  python -m dlai [file]   or   python -m dlai --cli file [--json out.json]"""

import argparse
import json
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dl_ai_detector")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--cli", action="store_true", help="analyse without opening the window")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--report", help="folder to export the PDF/JSON report into (with --cli)")
    a = ap.parse_args(argv)
    if a.cli:
        if not a.file:
            ap.error("--cli needs a file")
        from . import pipeline, report
        R, imgs = pipeline.analyze(a.file, a.frames, lambda f, s: print(f"[{f * 100:3.0f}%] {s}", file=sys.stderr))
        print(json.dumps(report._clean({k: v for k, v in R.items()}), indent=2, ensure_ascii=False, default=str))
        if a.report:
            print("Report:", report.export(R, imgs, a.report), file=sys.stderr)
        return 0
    from .gui import App
    App([a.file] if a.file else []).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
