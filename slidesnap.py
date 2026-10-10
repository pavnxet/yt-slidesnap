#!/usr/bin/env python3
"""yt-slidesnap - turn a lecture video into a PDF of its slides.

You give it a video URL and the timestamps where each question (or slide) is on
screen. It saves a clean frame of each one and stitches them into a PDF.

    yt-slidesnap run <url> --file timestamps.txt
    yt-slidesnap run <url> --text "Q22: Question 01:34:21 | Answer 01:34:53 (C)"
    Get-Content timestamps.txt | yt-slidesnap run <url>

Each line needs an id and at least one hh:mm:ss stamp. The first is when the slide
appears. Any stamps after it are ceilings and the earliest one wins, so a table
listing several later times (answer discussion, correct-option reveal) in any
column order still yields a clean window. Ceilings are never captured, so answers
cannot leak into the PDF.

No video is downloaded. Frames are read straight out of the stream over HTTP byte
ranges, so a 2-hour lecture costs a few hundred KB per slide.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw

__version__ = "1.0.0"

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled full build; never PATH ffmpeg
UA = "Mozilla/5.0"
DEFAULT_FORMAT = "bv*[height<=1080]/b[height<=1080]/b"

# A handful of hosts hand out stream URLs that are locked to the client that
# requested them, and ffmpeg then gets HTTP 403. YouTube's `android` client is
# the one that works; see README troubleshooting.
YOUTUBE_CLIENT = "android"

STAMP = re.compile(r"\d{1,2}:\d{2}:\d{2}")
ID = re.compile(r"^\s*(?:question\s*)?(Q?\d+)\b", re.I)
YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")


# --------------------------------------------------------------------------- input


def hms(text: str) -> int:
    h, m, s = (int(p) for p in text.split(":"))
    return h * 3600 + m * 60 + s


def parse_id(line: str, fallback: str) -> str:
    """Pull the slide id off a line, markdown table cell or plain prose."""
    first_cell = line.strip().lstrip("|").split("|")[0].strip()
    m = re.fullmatch(r"(?:Q\s*)?(\d+)", first_cell, re.I)
    if m:  # markdown row like "| Q19 | 01:41:04 | ..."
        return f"Q{m.group(1)}"
    m = ID.match(line)
    if not m:  # no recognisable id: number it by position
        return fallback
    got = m.group(1)
    return got.upper() if got.upper().startswith("Q") else f"Q{got}"


def parse_block(text: str):
    """Parse a pasted timestamp list -> [(id, slide_sec, ceiling_sec_or_None)].

    Loose on purpose: any line with an id and at least one hh:mm:ss is a slide.

    The first stamp is when the slide appears. The ceiling is the **earliest**
    stamp after it, not simply the second one - tables often carry several later
    stamps (answer discussion, correct-option reveal) in any column order, and the
    slide is only clean until the first thing that happens to it.
    """
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        stamps = STAMP.findall(line)
        if not stamps:
            continue
        start = hms(stamps[0])
        later = [hms(s) for s in stamps[1:] if hms(s) > start]
        out.append((parse_id(line, f"Q{len(out) + 1}"), start, min(later) if later else None))
    return out


def is_youtube(url: str) -> bool:
    return bool(YT_ID.search(url)) or "youtube.com" in url or "youtu.be" in url


def video_id(url: str) -> str:
    m = YT_ID.search(url)
    if m:
        return m.group(1)
    return re.sub(r"\W+", "-", url).strip("-")[-24:] or "video"


# --------------------------------------------------------------------------- extract


def resolve_stream(url: str, client: str, fmt: str) -> str:
    """Ask yt-dlp for a direct stream URL that ffmpeg can open."""
    if not shutil.which("yt-dlp"):
        sys.exit("yt-dlp not found on PATH.  Install it with:  pip install yt-dlp")
    cmd = ["yt-dlp", "-f", fmt, "-g", "--no-playlist", "--no-warnings"]
    if is_youtube(url) and client:
        cmd += ["--extractor-args", f"youtube:player_client={client}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        sys.exit(
            "could not resolve a stream URL.\n"
            "  * try another client:      --client android_vr | tv_embedded | ios\n"
            "  * try another format:      --format 'best[height<=720]'\n"
            "  * if it needs a login, export cookies and pass them to yt-dlp\n"
            f"\nyt-dlp said:\n{(proc.stderr or '')[-1200:]}"
        )
    return lines[0]


def extract_window(stream, qid, start, end, k, cand_dir):
    """One ffmpeg pass per slide -> k candidate frames spread across the window.

    Sampling stops at `end` (the answer stamp) because the span drives the
    frame rate, so the last candidate always lands before the answer.
    """
    span = max((end or start + 30) - start, 1.0)
    for stale in os.listdir(cand_dir):  # a smaller --frames must not leave old ones
        if stale.startswith(f"{qid}_"):
            os.remove(os.path.join(cand_dir, stale))
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-user_agent", UA,
         "-ss", str(start), "-i", stream, "-an", "-sn",
         "-vf", f"fps={k}/{span:.3f}", "-frames:v", str(k),
         "-fps_mode", "passthrough",
         os.path.join(cand_dir, f"{qid}_%02d.png")],
        capture_output=True, text=True,
    )
    return sorted(
        os.path.join(cand_dir, f) for f in os.listdir(cand_dir)
        if f.startswith(f"{qid}_") and f.endswith(".png")
    )


def autopick(paths):
    """Index of the least-occluded frame, by agreement with the window median.

    The slide is static and the presenter is not, so the per-pixel median across
    the window is the slide with the presenter removed. Whichever frame sits
    closest to that median is the one where they cover it least. -> (index, scores)
    """
    stack = np.stack([
        np.asarray(Image.open(p).convert("L"), dtype=np.float32) for p in paths
    ])
    median = np.median(stack, axis=0)
    scores = np.abs(stack - median).mean(axis=(1, 2))
    return int(np.argmin(scores)), scores


def run_one(stream, cand_dir, item, k, override):
    qid, start, end = item
    paths = extract_window(stream, qid, start, end, k, cand_dir)
    if not paths:
        return qid, None, None, start, "no frames extracted"
    idx, scores = autopick(paths)
    if override is not None:
        idx = max(0, min(override, len(paths) - 1))
    return qid, paths[idx], scores, start, f"picked {idx + 1}/{len(paths)}"


# --------------------------------------------------------------------------- output


def build_output(results, outdir, label=True):
    picked = [(q, p, s) for q, p, _sc, s, _n in results if p]
    if not picked:
        sys.exit("no frames were extracted - nothing to build")

    frames_dir = os.path.join(outdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    pages = []
    for qid, path, start in picked:
        im = Image.open(path).convert("RGB")
        im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
        if label:
            clock = f"{start // 3600}:{start % 3600 // 60:02d}:{start % 60:02d}"
            page = Image.new("RGB", (im.width, im.height + 26), (18, 18, 18))
            page.paste(im, (0, 26))
            ImageDraw.Draw(page).text((6, 7), f"{qid}    at {clock}", fill="#ffcc00")
            im = page
        im.save(os.path.join(frames_dir, f"{qid}.png"))
        pages.append(im)

    pdf = os.path.join(outdir, f"{picked[0][0]}-{picked[-1][0]}.pdf")
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=150.0)
    return pdf, picked


def contact_sheet(picked, outdir, cols=4, thumb=(640, 360)):
    rows = (len(picked) + cols - 1) // cols
    w, h = thumb
    sheet = Image.new("RGB", (cols * w, rows * (h + 20)), "black")
    draw = ImageDraw.Draw(sheet)
    for i, (qid, path, _s) in enumerate(picked):
        x, y = (i % cols) * w, (i // cols) * (h + 20)
        draw.text((5, y + 4), qid, fill="yellow")
        sheet.paste(Image.open(path).convert("RGB").resize(thumb), (x, y + 20))
    path = os.path.join(outdir, "auto_picked.png")
    sheet.save(path)
    return path


# --------------------------------------------------------------------------- commands


def cmd_run(args):
    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    elif not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text:
        sys.exit("no timestamps given - use --file, --text, or pipe them on stdin")

    items = parse_block(text)
    if not items:
        sys.exit("could not parse any timestamps - expected hh:mm:ss somewhere on each line")

    outdir = args.out or os.path.join(os.getcwd(), "slidesnap-out", video_id(args.url))
    cand_dir = os.path.join(outdir, "candidates")
    os.makedirs(cand_dir, exist_ok=True)

    overrides = {}
    for spec in args.override or []:
        key, _, val = spec.partition("=")
        overrides[key.strip().upper()] = int(val)

    print(f"url  : {args.url}")
    print(f"out  : {outdir}")
    print(f"found: {len(items)} slides ({items[0][0]}..{items[-1][0]})")
    stream = resolve_stream(args.url, args.client, args.format)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(
            lambda it: run_one(stream, cand_dir, it, args.frames, overrides.get(it[0])),
            items,
        ))
    for qid, _p, scores, _start, note in results:
        extra = ""
        if scores is not None:
            # a picked score close to the worst means every candidate was dirty
            extra = f"  (error {scores.min():.1f} vs worst {scores.max():.1f})"
        print(f"  {qid}: {note}{extra}")

    pdf, picked = build_output(results, outdir, label=not args.no_label)
    sheet = contact_sheet(picked, outdir)
    print(f"\npdf  : {pdf}  ({len(picked)} pages)")
    print(f"check: {sheet}")
    if len(picked) < len(items):
        print(f"WARNING: {len(items) - len(picked)} slide(s) produced no frames")


def cmd_selftest(_args):
    sample = (
        "Q22: Question 01:34:21 | Answer 01:34:53 (C)\n"
        "Q23: Question 01:38:45 | Answer 01:39:16 (D)\n"
        "Q29: Question 02:01:44 | Answer 02:01:50 (B)\n"
        "37. Question 02:20:08 | Answer 02:20:12 (D)\n"
        "\n"
        "Q38: Question 00:05:00 (no answer stamp)\n"
    )
    got = parse_block(sample)
    assert got[0] == ("Q22", 5661, 5693), got[0]
    assert got[1] == ("Q23", 5925, 5956), got[1]
    assert got[2] == ("Q29", 7304, 7310), got[2]
    assert got[3] == ("Q37", 8408, 8412), got[3]
    assert got[4] == ("Q38", 300, None), got[4]
    assert len(got) == 5, got
    assert hms("02:20:12") == 8412
    assert hms("00:00:00") == 0
    # answer stamp before the question -> window is unusable, must not be trusted
    assert parse_block("Q1 00:10:00 | 00:09:00")[0][2] is None
    # no answer stamp at all -> open-ended window
    assert parse_block("Q1 00:10:00")[0] == ("Q1", 600, None)

    # markdown table with three stamps per row in a non-chronological column
    # order: the ceiling must be the EARLIEST later stamp, not the second column
    table = (
        "| Q# | Question Start | Correct Option Timestamp | Answer Start | Correct Option |\n"
        "| -- | -------------- | ------------------------ | ------------ | -------------- |\n"
        "| Q19 | 01:41:04 | 01:44:16 | 01:41:27 | C |\n"
        "| Q26 | 02:13:05 | 02:17:51 | 02:13:19 | D |\n"
    )
    rows = parse_block(table)
    assert len(rows) == 2, rows  # header + separator have no stamps
    assert rows[0] == ("Q19", 6064, 6087), rows[0]
    assert rows[1] == ("Q26", 7985, 7999), rows[1]

    # ids survive a leading pipe and come out normalised
    assert parse_id("| Q19 | 01:41:04 |", "Q1") == "Q19"
    assert parse_id("| q7 | 00:01:00 |", "Q1") == "Q7"
    assert parse_id("Q22: Question 01:34:21 | Answer 01:34:53 (C)", "Q1") == "Q22"
    assert parse_id("no id here 00:01:00", "Q9") == "Q9"
    assert video_id("https://www.youtube.com/watch?v=6R4jGTw5owM") == "6R4jGTw5owM"
    assert video_id("https://youtu.be/6R4jGTw5owM?t=10") == "6R4jGTw5owM"
    assert is_youtube("https://youtu.be/6R4jGTw5owM")
    assert not is_youtube("https://example.com/lecture.mp4")
    print("selftest ok")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="yt-slidesnap", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="extract slide frames and build the PDF")
    r.add_argument("url", help="video URL (anything yt-dlp supports)")
    r.add_argument("--file", help="file holding the timestamp list")
    r.add_argument("--text", help="the timestamp list as a string")
    r.add_argument("--out", help="output folder (default ./slidesnap-out/<video-id>)")
    r.add_argument("--frames", type=int, default=8,
                   help="candidate frames per slide (default 8)")
    r.add_argument("--workers", type=int, default=4,
                   help="slides to extract at once (default 4)")
    r.add_argument("--format", default=DEFAULT_FORMAT,
                   help="yt-dlp format selector for the video stream")
    r.add_argument("--client", default=YOUTUBE_CLIENT,
                   help="yt-dlp player_client for YouTube (default android)")
    r.add_argument("--override", action="append", metavar="ID=N",
                   help="force candidate N for a slide, e.g. --override Q22=3")
    r.add_argument("--no-label", action="store_true",
                   help="don't caption PDF pages")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("selftest", help="check the timestamp parser")
    t.set_defaults(func=cmd_selftest)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
