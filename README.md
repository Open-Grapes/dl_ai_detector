# dl_ai_detector

Windows app that checks whether an **image or video** is AI-generated or deepfaked. It runs several independent checks and puts them in one report, instead of trusting a single "real / fake" guess.

Drop a file in → get a verdict, a breakdown, forensic maps you can inspect yourself, and a PDF/JSON report.

## What it checks

| Stage | What it does | Needs models? |
|---|---|---|
| **Preview & file info** | Plays the video right away with a scrub bar, frame stepping (← →, Shift = 1 s) and a score strip under the picture: one bar per analysed frame, coloured by AI score, which you can also click to seek. The AI % at the playhead updates as you scrub. When you pause on a frame, that exact frame can be scored too (*Score this frame*, or automatically with *Score frames I stop on*). Next to it: filename, duration, size, resolution, container, frame rate, codec, bit rate, audio | Scoring only |
| **How this was made** | Reads C2PA Content Credentials like Adobe's Inspect page: the AI model (e.g. `dreamina-seedance-2-5` → *ByteDance Seedance 2.5*), the app that created it, when, who signed it, and whether the signer is on the C2PA trust list | No |
| **Metadata & signatures** | C2PA Content Credentials (Adobe Firefly, OpenAI DALL·E / Sora, Google, Microsoft …), IPTC/XMP *Digital Source Type* (`trainedAlgorithmicMedia`), generator parameters left in PNG chunks / EXIF (Automatic1111, Forge, ComfyUI, InvokeAI, NovelAI, Fooocus), generator names in software tags, video container/encoder tags | No |
| **AI pixel score** | Hugging Face image classifiers score the whole image (and a centre crop at higher detail). Several models are averaged; if they disagree a lot, the verdict says so | Yes |
| **Face deepfake scan** | Finds faces (OpenCV YuNet), crops them and scores each face with a deepfake classifier: much more sensitive to face swaps than scanning the whole scene | Yes |
| **Forensic maps** | Error Level Analysis, FFT frequency spectrum, and a noise-level map, for checking with your own eyes | No |
| **Video timeline** | Samples ~1 frame per second (up to 48 frames, spread evenly over longer clips), plots the AI score and face score over time so a real clip with a few AI scenes spliced in stands out | Yes |
| **Temporal warp check** | Compares each sampled frame with the very next frame after compensating for motion (optical flow). Spikes mean texture or edges changed more than the motion explains, e.g. AI warping or details popping in and out | No |
| **Compression warning** | Estimates JPEG quality, spots WhatsApp/Telegram/social-style copies, screenshots and low-bitrate video, and warns that pixel scores are less reliable on them | No |

### Text tab: AI text detector

Paste text, type it in, or drop a document (**PDF, Word .docx, PowerPoint .pptx, OpenDocument, RTF, HTML, TXT/Markdown, subtitles**). The text is split into passages and each is scored by every installed AI-text model:

- **Verdict + overall AI likelihood** (weighted by passage length) and how much of the text sits in strongly-AI passages, so a human essay with a pasted-in AI section shows up as *Mixed*
- **Highlighted passages**: red = 75%+ likely AI, amber = uncertain, yellow = weak signal. Hover a passage to see each model's score
- **Chatbot phrases underlined** ("delve", "it's important to note", "in today's fast-paced" …) and writing-style statistics (sentence-length variation, vocabulary, em dashes). These are context, not proof
- Warnings for short texts (<50 words) and non-English text (the models are English-only)
- Scanned PDFs (pictures of pages) have no text to read; run them through OCR first

**Export report** saves `<name>_report.pdf`, `<name>_report.json` and the ELA/FFT/noise/timeline/face PNGs into one folder.

## Models: downloaded per user, never bundled

The app ships **without** any models. The first time you start it, it offers to download the recommended set (~1.5 GB). Click **Models…** any time to:

- **Download** recommended, all, or selected models
- **Check for updates.** This asks Hugging Face whether each model's weight files changed (by content hash, so README-only edits upstream don't count)
- **Install updates.** Downloads into a temporary folder and swaps the new version in only when it's complete. A cancelled update keeps the old one.
- **Remove selected** / **Remove all** to free up disk space
- **Open folder**

Models are stored in **`%LOCALAPPDATA%\dl_ai_detector\models`** (e.g. `C:\Users\<you>\AppData\Local\dl_ai_detector\models`). That folder belongs to the current Windows user, so anyone else using the app on another PC (or another account) starts fresh and downloads their own copy. `%TEMP%` is not used because Windows Storage Sense / Disk Cleanup empties it automatically.

| ★ | Model | Type | Size | License |
|---|---|---|---|---|
| ★ | [Ateeqq/ai-vs-human-image-detector](https://huggingface.co/Ateeqq/ai-vs-human-image-detector) (SigLIP) | AI image | 372 MB | Apache-2.0 |
| ★ | [haywoodsloan/ai-image-detector-deploy](https://huggingface.co/haywoodsloan/ai-image-detector-deploy) (SwinV2) | AI image | 781 MB | Apache-2.0 |
|   | [Organika/sdxl-detector](https://huggingface.co/Organika/sdxl-detector) (Swin) | AI image | 347 MB | CC-BY-NC-3.0 |
| ★ | [prithivMLmods/deepfake-detector-model-v1](https://huggingface.co/prithivMLmods/deepfake-detector-model-v1) (SigLIP) | Face deepfake | 372 MB | Apache-2.0 |
|   | [prithivMLmods/Deep-Fake-Detector-v2-Model](https://huggingface.co/prithivMLmods/Deep-Fake-Detector-v2-Model) (ViT) | Face deepfake | 343 MB | Apache-2.0 |
| ★ | [opencv/face_detection_yunet](https://huggingface.co/opencv/face_detection_yunet) | Face finder | 0.2 MB | MIT |

| ★ | [ShantanuT01/vanguard-ai-text-detector](https://huggingface.co/ShantanuT01/vanguard-ai-text-detector) (ModernBERT-large) | AI text | 1.6 GB | MIT |
| ★ | [fakespot-ai/roberta-base-ai-text-detection-v1](https://huggingface.co/fakespot-ai/roberta-base-ai-text-detection-v1) (RoBERTa) | AI text | 499 MB | Apache-2.0 |
|   | [desklib/ai-text-detector-v1.01](https://huggingface.co/desklib/ai-text-detector-v1.01) (DeBERTa-v3-large) | AI text | 1.7 GB | MIT |

★ = recommended. The image/video set is offered on first run; the text set is offered the first time you analyse text. Settings and the log live next to the models in `%LOCALAPPDATA%\dl_ai_detector`.

## How to read the verdict

- **AI-generated: confirmed by metadata.** The file itself declares it (C2PA, IPTC source type or generator parameters). This is the strongest signal.
- **Likely AI-generated** (≥75%) / **Possibly AI, inconclusive** (50–75%) / **Probably real** (25–50%) / **Likely real** (<25%). These come from the pixel and face models.
- *Models disagree*: the pixel models are more than 50 points apart. Look at the forensic maps.

No detector is 100% reliable. If metadata is missing, that proves nothing, because most sites strip it. Pixel scores can be wrong on brand-new generators, heavily compressed files, screenshots, CGI/3D renders, and heavily retouched photos. Treat the result as evidence to weigh, not a final ruling.

## Updates

**Check for updates…** (toolbar) checks two things at once and **Update all** installs both:

- **The app**: asks GitHub for the latest release of this repo. If its tag (e.g. `v1.4.0`) is newer than the running version, it downloads `dl_ai_detector-windows.zip` from that release, and a small helper swaps the files once the app closes and restarts it. Models and settings in `%LOCALAPPDATA%` are kept. If you're already on the latest version it says so.
- **Models**: same as *Check for updates* in the Models window.

The app also checks quietly at start-up and changes the button to **⬆ Update to x.y.z** when there's a new version.

**Publishing an update**: bump `APP_VERSION` in `dlai/common.py`, build, then create a GitHub release whose tag is that version with the zip attached:

```bat
gh release create v1.4.0 dist\Windows\dl_ai_detector-windows.zip --title "1.4.0" --notes "What changed"
```

**Private repo**: GitHub hides private releases from everyone who isn't logged in, so other users only get updates once the repo is public. On a PC logged in with the GitHub CLI (`gh auth login`) as an account with access, the check uses that login, so the owner can test updates while the repo is private. No login or token is built into the app.

## Run from source

```bat
py -3.13 -m venv .venv
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\pip install -r requirements.txt
dl_ai_detector.cmd
```

Command-line mode (prints JSON, optional report):

```bat
.venv\Scripts\python -m dlai --cli photo.jpg --report out_folder
```

If a CUDA build of PyTorch is installed instead, the models run on the GPU automatically.

## Build the .exe

```bat
.venv\Scripts\python build.py
```

Output: `dist\Windows\dl_ai_detector\dl_ai_detector.exe` plus `dist\Windows\dl_ai_detector-windows.zip`. The build uses CPU PyTorch to keep it small. Analysis takes a few seconds per image and roughly 5–15 s per minute of video.

## Layout

```
dlai/
  common.py     paths (per-user data folder), settings, logging
  models.py     model catalog, download / update check / remove, inference
  metadata.py   C2PA, XMP/IPTC, PNG chunks, EXIF, video container tags
  forensics.py  ELA, FFT, noise map, JPEG quality, compression warning
  video.py      frame sampling, temporal warp check, face finder
  textio.py     text extraction from PDF, Word, PowerPoint, RTF, HTML …
  textdetect.py AI text detection: passages, scoring, style statistics
  textui.py     Text tab and text report
  player.py     Preview tab: video player, score strip, per-frame scoring
  fileinfo.py   file facts (duration, resolution, codec, …)
  pipeline.py   runs everything and builds the verdict
  report.py     JSON / PDF / PNG export
  charts.py     timeline charts (PIL)
  gui.py        window + Models manager
```
