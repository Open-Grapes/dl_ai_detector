"""Launcher used by PyInstaller and for running from source: python run.py"""
import faulthandler
import os
import sys
import traceback
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")

from dlai.common import LOG, ensure_dirs, log  # noqa: E402

if __name__ == "__main__":
    ensure_dirs()
    if sys.stderr is None:                       # --windowed build: tqdm & co. must have somewhere to write
        sys.stderr = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    _crash = open(LOG.with_name("crash.log"), "a", encoding="utf-8")
    faulthandler.enable(_crash)                  # native crashes get a trace too
    try:
        from dlai.__main__ import main
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        log("CRASH\n" + traceback.format_exc())
        raise
