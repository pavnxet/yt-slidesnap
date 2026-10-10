#!/usr/bin/env python3
"""yt-slidesnap - turn a lecture video into a PDF of its slides.

You give it a video URL and the timestamps where each question (or slide) is on
screen. It saves a clean frame of each one and stitches them into a PDF.

    yt-slidesnap run <url> --file timestamps.txt
    yt-slidesnap run <url> --text "Q22: Question 01:34:21 | Answer 01:34:53 (C)"
    Get-Content timestamps.txt | yt-slidesnap run <url>
    yt-slidesnap serve                    # local web UI at http://127.0.0.1:8765

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
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

__version__ = "1.3.1"

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled full build; never PATH ffmpeg
UA = "Mozilla/5.0"
DEFAULT_FORMAT = "bv*[height<=1080]/b[height<=1080]/b"

# A handful of hosts hand out stream URLs that are locked to the client that
# requested them, and ffmpeg then gets HTTP 403. YouTube's `android` client is
# the one that works; see README troubleshooting.
YOUTUBE_CLIENT = "android"


class SnapError(Exception):
    """A failure the user should read, rather than a traceback."""

YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")

# Branding. The heart is U+2764 only - the emoji variation selector U+FE0F is a
# separate codepoint many text fonts lack, and it renders as a box when missing.
REPO = "github.com/pavnxet/yt-slidesnap"
REPO_URL = "https://" + REPO
HEART = "\u2764"
MARK = f"Made with {HEART} by {REPO}"
MARK_FALLBACK = f"Made with love by {REPO}"

# Fonts that might carry U+2764, best-guess order per platform. Chosen by
# capability at runtime, never assumed - see small_font().
MARK_FONTS = (
    r"C:\Windows\Fonts\seguisym.ttf",          # Segoe UI Symbol: has it
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
)
CAP_H, MARK_H, TEXT = 30, 28, 15  # page header/footer strip heights, text size


# --------------------------------------------------------------------------- input
#
# Timestamps are accepted in whatever shape they were pasted. All of these mean
# 1h34m21s:  1:34:21   01:34:21.500   94:21*   1h34m21s   1h 34m 21s   5661s
#            ?t=5661   5661+
#
#   * two-part A:B is read as M:S, never H:M - see README
#   + bare integers are seconds only where the shape says they are times, never
#     where they could be a question number

CLOCK = re.compile(
    r"\b(\d{1,3}):([0-5]?\d)(?::([0-5]?\d))?(?:[.,]\d{1,3})?(?![\d:])")
UNIT_PAIR = re.compile(
    r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.I)
UNIT_SEC = {"h": 3600, "m": 60, "s": 1}
UNIT_RANK = {"h": 0, "m": 1, "s": 2}
URI_T = re.compile(r"[?&]t=(\d{1,6})\b")
BARE = re.compile(r"(?<![\d:\w.])(\d{2,6})(?![\d:\w.])")

# ids: Q22 / q22 / q 22 / Question 22 / Slide 3 / #22 / [22] / 22. / 22)
ID_HEAD = r"^\s*[\[\(]?\s*(?:q(?:uestion)?|ques|slide|no|num)?\s*[.:\-)\]#]*\s*(\d{1,4})"
LEAD_ID = re.compile(ID_HEAD + r"\s*[.:\-)\]#]*\s", re.I)
CELL_ID = re.compile(ID_HEAD + r"\s*[.:\-)\]#]*\s*$", re.I)
NOISE = {"question", "answer", "ans", "time", "start", "end", "timestamp", "q",
         "no", "num", "slide", "at", "from", "to", "and"}
WORDY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,23}")  # one filename-safe word


def hms(text: str) -> int:
    """'H:MM:SS' -> seconds. For known-good strings; parse_block is looser."""
    h, m, s = (int(p) for p in text.split(":"))
    return h * 3600 + m * 60 + s


def clock(sec: int) -> str:
    """seconds -> 'H:MM:SS'."""
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _unit_stamps(line):
    """'1h34m21s' / '1h 34m' / '90s' -> [(start, end, seconds)].

    Adjacent number+unit pairs merge into ONE stamp only when the units descend
    h -> m -> s without repeating, which is what a single duration looks like.
    '5661s 5693s' is two timestamps, not 11354 seconds.
    """
    pairs = [(m.start(), m.end(), int(m.group(1)), m.group(2)[0].lower())
             for m in UNIT_PAIR.finditer(line)]
    out, i = [], 0
    while i < len(pairs):
        start, end, val, unit = pairs[i]
        total, rank = val * UNIT_SEC[unit], UNIT_RANK[unit]
        j = i + 1
        while j < len(pairs):
            n_start, n_end, n_val, n_unit = pairs[j]
            if line[end:n_start].strip(" \t,"):  # something in between: separate
                break
            if UNIT_RANK[n_unit] <= rank:  # same or rising unit: separate
                break
            total += n_val * UNIT_SEC[n_unit]
            rank, end, j = UNIT_RANK[n_unit], n_end, j + 1
        out.append((start, end, total))
        i = j
    return out


def _stamps(line):
    """Every timestamp on a line -> [(start, end, seconds)], de-overlapped.

    A two-part clock is M:S and a three-part one is H:M:S, so 94:21 is 94 minutes
    and 1:34:21 is an hour and a half. Fractional seconds are dropped - the frame
    granularity that matters here is 1/30s at worst and ffmpeg seeks by keyframe.
    """
    raw = []
    for m in CLOCK.finditer(line):
        a, b, c = m.group(1), m.group(2), m.group(3)
        sec = int(a) * 3600 + int(b) * 60 + int(c) if c is not None else int(a) * 60 + int(b)
        raw.append((m.start(), m.end(), sec))
    raw += _unit_stamps(line)
    for m in URI_T.finditer(line):
        raw.append((m.start(1), m.end(1), int(m.group(1))))

    out = []
    for span in sorted(raw):
        if out and span[0] < out[-1][1]:  # a token sitting inside one already taken
            continue
        out.append(span)
    return out


def _row_cells(line):
    """A markdown / semicolon / CSV row -> its cells, else None."""
    s = line.strip()
    if s.startswith("|") or (s.endswith("|") and s.count("|") >= 2):
        cells = [c.strip() for c in s.strip("|").split("|")]
        return cells if len(cells) >= 2 else None
    for sep in (";", ","):
        if s.count(sep) >= 2:
            cells = [c.strip() for c in s.split(sep)]
            if CELL_ID.match(cells[0]):  # a bare id cell, not prose full of commas
                return cells
    return None


def _id_span(line):
    """Character range of a markdown row's first cell, so its number isn't a time."""
    i = line.find("|")
    if i < 0:
        return None
    j = line.find("|", i + 1)
    if j < 0:
        return None
    a, b = i + 1, j
    while a < b and line[a].isspace():
        a += 1
    while b > a and line[b - 1].isspace():
        b -= 1
    return (a, b)


def _label(line, row, stamps):
    """Slide id, or None when the line carries no recognisable one."""
    if row:
        cell = row[0]
        m = CELL_ID.match(cell)
        if m:
            return f"Q{m.group(1)}"
        if 0 < len(cell) <= 24 and not cell[0].isdigit():
            return cell  # a worded row label, e.g. "Intro"
        return None
    m = LEAD_ID.match(line)
    if m and not any(a <= m.start(1) < b for a, b, _ in stamps):
        return f"Q{m.group(1)}"  # not a bare-seconds stamp mistaken for an id
    if stamps:
        head = line[:stamps[0][0]].strip(" \t-â€“â€”:|.,()]#")
        # must actually look like a label, or a URL's prefix becomes an id
        if WORDY.fullmatch(head) and head.lower() not in NOISE:
            return head  # e.g. "Intro - 00:05:00"
    return None


def parse_block(text: str, notes=None):
    """Parse a pasted timestamp list -> [(id, slide_sec, ceiling_sec_or_None)].

    The first stamp on a line is when the slide appears. The ceiling is the
    **earliest** stamp after it, not simply the second one: pasted tables carry
    several later stamps (answer discussion, correct-option reveal) in any column
    order, and a slide is only clean until the first thing happens to it.

    Ids are de-duplicated and the result is sorted by start time. If `notes` is a
    list, caveats about the interpretation are appended to it - a bare list of
    timestamps still parses, but it loses both the video's numbering and the
    answer ceiling, and that should be said out loud rather than discovered later.
    """
    # Windows editors happily save as "UTF-8 with BOM"; left in place it becomes
    # part of line 1 and quietly costs that line its id.
    text = text.replace("\ufeff", "")
    out, auto = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        row = _row_cells(line)
        stamps = _stamps(line)

        if not stamps:
            # No clock/unit stamps. Plain integers are seconds only when the shape
            # says so: several of them, and none of them the row's id cell.
            span = _id_span(line)
            big = []
            for m in BARE.finditer(line):
                if span and span[0] <= m.start(1) < span[1]:
                    continue
                if int(m.group(1)) >= 60:
                    big.append((m.start(1), m.end(1), int(m.group(1))))
            if len(big) >= 2:
                stamps = big[:2]

        if not stamps:
            continue
        start = stamps[0][2]
        later = [s[2] for s in stamps[1:] if s[2] > start]
        label = _label(line, row, stamps)
        auto += label is None
        out.append((label or f"Q{len(out) + 1}", start,
                    min(later) if later else None))

    seen, uniq = set(), []
    for item in out:
        if item not in seen:  # the same line pasted twice
            seen.add(item)
            uniq.append(item)

    # duplicate ids would overwrite each other's frame file
    counts, fixed = {}, []
    for qid, start, end in uniq:
        counts[qid] = counts.get(qid, 0) + 1
        fixed.append((qid if counts[qid] == 1 else f"{qid}-{counts[qid]}", start, end))

    if notes is not None:
        if auto:
            notes.append(
                f"{auto} line(s) had no id, so they were numbered by position "
                f"(Q1, Q2, ...). That will not match the video's own numbering - "
                f"start each line with an id, e.g. 'Q19 01:41:04'.")
        opened = sum(1 for _q, _s, e in fixed if e is None)
        if opened:
            notes.append(
                f"{opened} line(s) gave no end timestamp, so a 30s window was used. "
                f"A window that long can run past the answer and capture it - add the "
                f"answer time, e.g. 'Q19 01:41:04 01:41:27'.")

    return sorted(fixed, key=lambda it: it[1])


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
        raise SnapError(
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

_FONT_CACHE = {}


def small_font(size=TEXT):
    """Small font for the page caption and watermark -> (font, heart_ok).

    Picked by capability, not by assumption: each candidate is test-drawn and
    compared against a guaranteed-missing codepoint, so a font that would render
    the heart as a tofu box is rejected rather than shipped.
    """
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]

    def draw(font, ch):
        im = Image.new("L", (48, 48), 0)
        ImageDraw.Draw(im).text((0, 0), ch, font=font, fill=255)
        return im.tobytes()

    def has_heart(font):
        try:
            return draw(font, HEART) != draw(font, "\uffff")
        except Exception:
            return False

    chosen, ok = None, False
    for path in MARK_FONTS:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            continue
        if chosen is None:
            chosen = font
        if has_heart(font):
            chosen, ok = font, True
            break
    if chosen is None:
        try:
            chosen = ImageFont.load_default(size=size)  # Pillow >= 10.1
        except TypeError:
            chosen = ImageFont.load_default()
        ok = has_heart(chosen)
    _FONT_CACHE[size] = (chosen, ok)
    return chosen, ok


def build_output(results, outdir, label=True, watermark=True):
    picked = [(q, p, s) for q, p, _sc, s, _n in results if p]
    if not picked:
        raise SnapError("no frames were extracted - nothing to build")

    frames_dir = os.path.join(outdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    font, heart_ok = small_font()
    mark = MARK if heart_ok else MARK_FALLBACK

    pages = []
    for qid, path, start in picked:
        im = Image.open(path).convert("RGB")
        im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
        head = CAP_H if label else 0
        foot = MARK_H if watermark else 0
        if head or foot:
            page = Image.new("RGB", (im.width, im.height + head + foot), (18, 18, 18))
            page.paste(im, (0, head))
            draw = ImageDraw.Draw(page)
            if label:
                clock = f"{start // 3600}:{start % 3600 // 60:02d}:{start % 60:02d}"
                draw.text((10, 6), f"{qid}    at {clock}", fill="#ffcc00", font=font)
            if watermark:
                width = draw.textlength(mark, font=font)
                draw.text(((im.width - width) / 2, im.height + head + 5),
                          mark, fill="#7b8494", font=font)
            im = page
        im.save(os.path.join(frames_dir, f"{qid}.png"))
        pages.append(im)

    pdf = os.path.join(outdir, f"{picked[0][0]}-{picked[-1][0]}.pdf")
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=150.0)
    return pdf, picked


def contact_sheet(picked, outdir, cols=4, thumb=(640, 360)):
    cols = max(1, min(cols, len(picked)))  # short runs must not pad with empty cells
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


def _overrides(specs):
    out = {}
    for spec in specs or []:
        key, _, val = spec.partition("=")
        out[key.strip().upper()] = int(val)
    return out


def run_pipeline(url, items, outdir, frames=8, workers=4, client=YOUTUBE_CLIENT,
                 fmt=DEFAULT_FORMAT, overrides=None, label=True, watermark=True,
                 progress=None):
    """Extract every slide and build the PDF. Shared by the CLI and the web UI.

    `progress` is called with one human-readable line at a time, from worker
    threads as well as this one.
    """
    say = progress or (lambda _msg: None)
    overrides = overrides or {}
    cand_dir = os.path.join(outdir, "candidates")
    os.makedirs(cand_dir, exist_ok=True)

    say("resolving stream URL ...")
    stream = resolve_stream(url, client, fmt)
    say("stream resolved, extracting frames ...")

    def one(item):
        qid, path, scores, start, note = run_one(
            stream, cand_dir, item, frames, overrides.get(item[0]))
        detail = ""
        if scores is not None:
            # a picked score close to the worst means every candidate was dirty
            detail = f" (error {scores.min():.1f} vs worst {scores.max():.1f})"
        # show the window we understood, so a misread timestamp is visible here
        window = f"{clock(start)}-{clock(item[2])}" if item[2] else clock(start)
        say(f"{qid} [{window}]: {note}{detail}")
        return qid, path, scores, start, note

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, items))

    pdf, picked = build_output(results, outdir, label=label, watermark=watermark)
    sheet = contact_sheet(picked, outdir)
    if len(picked) < len(items):
        say(f"WARNING: {len(items) - len(picked)} slide(s) produced no frames")
    say(f"pdf: {os.path.basename(pdf)} ({len(picked)} pages)")
    return pdf, sheet, picked


def cmd_run(args):
    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    elif not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text:
        sys.exit("no timestamps given - use --file, --text, or pipe them on stdin")

    notes = []
    items = parse_block(text, notes)
    if not items:
        sys.exit("could not parse any timestamps - expected hh:mm:ss somewhere on each line")

    outdir = args.out or os.path.join(os.getcwd(), "slidesnap-out", video_id(args.url))
    print(f"url  : {args.url}")
    print(f"out  : {outdir}")
    print(f"found: {len(items)} slides ({items[0][0]}..{items[-1][0]})")
    for note in notes:
        print(f"note : {note}")

    if args.dry_run:
        # the parser accepts many shapes, so show what it decided before spending
        # a run on it - a misread 1:34 as M:S instead of H:M is silent otherwise
        for qid, start, end in items:
            span = f"{clock(start)} -> {clock(end)}" if end else clock(start)
            print(f"  {qid:<10} {span:<26} {f'{end - start}s' if end else 'open'}")
        print(f"\n{len(items)} slide(s) parsed - nothing extracted (--dry-run)")
        return

    try:
        pdf, sheet, picked = run_pipeline(
            args.url, items, outdir, frames=args.frames, workers=args.workers,
            client=args.client, fmt=args.format, overrides=_overrides(args.override),
            label=not args.no_label, watermark=not args.no_watermark,
            progress=lambda m: print(f"  {m}"),
        )
    except SnapError as exc:
        sys.exit(str(exc))

    print(f"\npdf  : {pdf}  ({len(picked)} pages)")
    print(f"check: {sheet}")


def cmd_selftest(_args):
    sample = (
        "Q22: Question 01:34:21 | Answer 01:34:53 (C)\n"
        "Q23: Question 01:38:45 | Answer 01:39:16 (D)\n"
        "Q29: Question 02:01:44 | Answer 02:01:50 (B)\n"
        "37. Question 02:20:08 | Answer 02:20:12 (D)\n"
        "\n"
        "Q38: Question 00:05:00 (no answer stamp)\n"
    )
    # keyed by id, not position: output is sorted by start time, so the sample's
    # Q38 at 00:05:00 legitimately sorts ahead of the rest
    got = {q: (s, e) for q, s, e in parse_block(sample)}
    assert got["Q22"] == (5661, 5693), got
    assert got["Q23"] == (5925, 5956), got
    assert got["Q29"] == (7304, 7310), got
    assert got["Q37"] == (8408, 8412), got
    assert got["Q38"] == (300, None), got
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

    # --- accepted timestamp shapes -------------------------------------------
    def one(line):
        got = parse_block(line)
        return got[0] if got else None

    # clock: three parts is H:M:S, two parts is M:S
    assert one("1:34:21") == ("Q1", 5661, None), one("1:34:21")
    assert one("01:34:21.500") == ("Q1", 5661, None), one("01:34:21.500")
    assert one("34:21") == ("Q1", 2061, None), one("34:21")   # M:S, never H:M
    assert one("94:21") == ("Q1", 5661, None), one("94:21")   # 94 minutes
    assert one("0:00:01") == ("Q1", 1, None), one("0:00:01")
    # units, including adjacent parts of one duration
    assert one("1h34m21s") == ("Q1", 5661, None), one("1h34m21s")
    assert one("1h 34m 21s") == ("Q1", 5661, None), one("1h 34m 21s")
    assert one("1h34m") == ("Q1", 5640, None), one("1h34m")
    assert one("2m3s") == ("Q1", 123, None), one("2m3s")
    assert one("90s") == ("Q1", 90, None), one("90s")
    assert one("1 hour 2 minutes") == ("Q1", 3720, None), one("1 hour 2 minutes")
    # two durations in a row stay two stamps - 5661s 5693s is not 11354s
    assert one("Q22 5661s 5693s") == ("Q22", 5661, 5693), one("Q22 5661s 5693s")
    # seconds, bare or from a URL
    assert one("https://youtu.be/xyz?t=5661") == ("Q1", 5661, None)
    assert one("https://youtu.be/xyz?t=1h34m21s") == ("Q1", 5661, None)
    assert one("Q22 5661 5693") == ("Q22", 5661, 5693), one("Q22 5661 5693")
    assert one("5661 5693") == ("Q1", 5661, 5693), one("5661 5693")  # times, not ids

    # --- accepted id shapes ---------------------------------------------------
    for text in ("Q22: 01:34:21", "q22 01:34:21", "q 22 - 01:34:21",
                 "Question 22: 01:34:21", "[22] 01:34:21", "22. 01:34:21",
                 "22) 01:34:21", "#22 01:34:21"):
        assert one(text)[0] == "Q22", (text, one(text))
    assert one("Slide 3 01:34:21")[0] == "Q3", one("Slide 3 01:34:21")
    assert one("Intro: 00:05:00")[0] == "Intro", one("Intro: 00:05:00")
    assert one("no id here 00:05:00")[0] == "Q1", one("no id here 00:05:00")

    # --- row shapes -----------------------------------------------------------
    assert one("| Q19 | 01:41:04 | 01:44:16 | 01:41:27 | C |") == ("Q19", 6064, 6087)
    assert one("| Q19 | 1:41:04 | 1:41:27 |") == ("Q19", 6064, 6087)
    assert one("Q22,1:34:21,1:34:53") == ("Q22", 5661, 5693)
    assert one("Q22;1:34:21;1:34:53") == ("Q22", 5661, 5693)
    assert one("| 100 | 5661 | 5693 |") == ("Q100", 5661, 5693)  # id cell isn't a time

    # --- housekeeping ---------------------------------------------------------
    # a repeated id is suffixed so frame files cannot overwrite each other, a
    # duplicated line is dropped, and output is sorted by start time
    dup = parse_block("Q5 00:10:00\nQ5 00:10:00\nQ5 00:20:00")
    assert [d[0] for d in dup] == ["Q5", "Q5-2"], dup
    assert [j[0] for j in parse_block("Q2 00:20:00\nQ1 00:10:00")] == ["Q1", "Q2"]
    # a heading or separator row is not a slide
    assert parse_block("just a heading\n---\n| -- | --- |") == []
    # a UTF-8 BOM must not cost the first line its id
    assert parse_block("\ufeffQ22: Question 01:34:21 | Answer 01:34:53 (C)")[0] == \
        ("Q22", 5661, 5693)

    # a bare list of timestamps still parses, but both caveats must be reported
    notes = []
    bare = parse_block("01:41:04\n01:47:56\nQ3 01:57:02 01:57:26", notes)
    assert [b[0] for b in bare] == ["Q1", "Q2", "Q3"], bare
    assert len(notes) == 2, notes
    assert any("numbered by position" in n for n in notes), notes
    assert any("no end timestamp" in n for n in notes), notes
    # a fully specified list is quiet
    quiet = []
    parse_block("Q19 01:41:04 01:41:27\nQ20 01:47:56 01:48:23", quiet)
    assert quiet == [], quiet
    assert video_id("https://www.youtube.com/watch?v=6R4jGTw5owM") == "6R4jGTw5owM"
    assert video_id("https://youtu.be/6R4jGTw5owM?t=10") == "6R4jGTw5owM"
    assert is_youtube("https://youtu.be/6R4jGTw5owM")
    assert not is_youtube("https://example.com/lecture.mp4")

    # a machine with none of the candidate fonts must still render a page, just
    # without the heart - the tofu box is the failure this guards against
    global MARK_FONTS
    saved, MARK_FONTS = MARK_FONTS, ()
    _FONT_CACHE.clear()
    try:
        font, heart_ok = small_font()
        assert font is not None, "fallback font must still be a usable font"
        assert heart_ok is False, "no candidate font exists, so no heart"
        assert HEART not in MARK_FALLBACK, MARK_FALLBACK
        assert REPO in MARK_FALLBACK and REPO in MARK, "branding must keep the repo"
    finally:
        MARK_FONTS, _ = saved, _FONT_CACHE.clear()
    print("selftest ok")


# --------------------------------------------------------------------------- web ui

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt-slidesnap</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;padding:36px 20px;background:#14161a;color:#e6e8eb;
     font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:880px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px}
h1 span{color:#60a5fa}
p.sub{margin:0 0 26px;color:#8b93a1}
label{display:block;font-weight:600;margin:18px 0 6px;font-size:12px;
      text-transform:uppercase;letter-spacing:.05em;color:#a8b0bd}
input,textarea{width:100%;padding:11px 13px;border-radius:8px;border:1px solid #2c313a;
     background:#1b1e24;color:#e6e8eb;font:inherit}
textarea{min-height:190px;font-family:ui-monospace,Consolas,monospace;font-size:13px}
input:focus,textarea:focus{outline:2px solid #3b82f6;outline-offset:-1px;border-color:transparent}
.row{display:flex;gap:18px}.row>div{flex:1}
button{margin-top:22px;padding:12px 24px;border:0;border-radius:8px;background:#3b82f6;
     color:#fff;font:600 15px inherit;cursor:pointer}
button:hover:not(:disabled){background:#2f74e0}
button:disabled{background:#2c313a;color:#6b7280;cursor:default}
#status{margin-top:14px;font-weight:600;min-height:22px}
.err{color:#f87171}.ok{color:#4ade80}
pre{margin-top:16px;padding:14px;border-radius:8px;background:#0d0f12;border:1px solid #23272e;
    max-height:260px;overflow:auto;font:12px/1.6 ui-monospace,Consolas,monospace;color:#9fb3c8}
#result{margin-top:28px;padding-top:22px;border-top:1px solid #23272e}
a{color:#60a5fa}
img{width:100%;border-radius:8px;margin-top:16px;border:1px solid #23272e}
footer{margin-top:36px;padding-top:18px;border-top:1px solid #23272e;text-align:center;
       color:#6b7280;font-size:13px}
footer .h{color:#e0556b}
footer a{color:#8b93a1;text-decoration:none;border-bottom:1px solid #2c313a}
footer a:hover{color:#60a5fa;border-bottom-color:#60a5fa}
</style></head><body><main>
<h1>yt-<span>slidesnap</span></h1>
<p class="sub">Paste a video URL and your timestamps. You get back a PDF of the slides.</p>

<label for="url">Video URL</label>
<input id="url" placeholder="https://youtu.be/...">

<label for="ts">Timestamps &mdash; one slide per line</label>
<textarea id="ts" placeholder="Q22: Question 01:34:21 | Answer 01:34:53 (C)&#10;&#10;Markdown tables work too:&#10;| Q19 | 01:41:04 | 01:44:16 | 01:41:27 | C |"></textarea>

<div class="row"><div>
  <label for="frames">Candidates per slide</label>
  <input id="frames" type="number" min="1" max="32" value="8">
</div></div>

<button id="go">Extract slides</button>
<div id="status"></div>
<pre id="log" hidden></pre>

<div id="result" hidden>
  <div>PDF: <a id="pdf" href="#" target="_blank" rel="noopener"></a></div>
  <img id="sheet" alt="contact sheet of the picked frames">
</div>

<footer>Made with <span class="h">&#10084;</span> by
  <a href="__REPO_URL__" target="_blank" rel="noopener">__REPO__</a></footer>
</main><script>
const $ = s => document.querySelector(s);
const say = (t, c) => { const e = $('#status'); e.textContent = t; e.className = c || ''; };

$('#go').onclick = async () => {
  $('#result').hidden = true;
  $('#log').hidden = false;
  $('#log').textContent = '';
  say('starting...');
  const r = await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url: $('#url').value.trim(),
                          timestamps: $('#ts').value,
                          frames: +$('#frames').value})});
  const j = await r.json();
  if (j.error) { say(j.error, 'err'); return; }
  $('#go').disabled = true;
  poll();
};

async function poll() {
  let s;
  try { s = await (await fetch('/status')).json(); }
  catch (e) { say('lost contact with the server', 'err'); $('#go').disabled = false; return; }
  if (s.log.length) {
    $('#log').textContent = s.log.join('\\n');
    $('#log').scrollTop = $('#log').scrollHeight;
  }
  if (!s.done) { say('working...'); setTimeout(poll, 600); return; }
  $('#go').disabled = false;
  if (s.error) { say(s.error, 'err'); return; }
  say('done - ' + s.pages + ' page' + (s.pages === 1 ? '' : 's'), 'ok');
  $('#pdf').href = '/file/' + encodeURIComponent(s.pdf);
  $('#pdf').textContent = s.pdf;
  $('#sheet').src = '/file/' + encodeURIComponent(s.sheet) + '?t=' + Date.now();
  $('#result').hidden = false;
}
</script></body></html>
""".replace("__REPO_URL__", REPO_URL).replace("__REPO__", REPO)

JOB = {"running": False, "done": False, "log": [], "error": None,
       "outdir": None, "pdf": None, "sheet": None, "pages": 0}
JOB_LOCK = threading.Lock()


def start_job(payload):
    """Kick off a run in the background. Returns an error string, or None."""
    url = (payload.get("url") or "").strip()
    if not url:
        return "paste a video URL first"
    notes = []
    items = parse_block(payload.get("timestamps") or "", notes)
    if not items:
        return "no timestamps found - each line needs an hh:mm:ss"
    try:
        frames = max(1, min(int(payload.get("frames") or 8), 32))
    except (TypeError, ValueError):
        return "candidates must be a whole number"

    with JOB_LOCK:
        if JOB["running"]:
            return "a run is already in progress"
        JOB.update({"running": True, "done": False, "log": [], "error": None,
                    "outdir": None, "pdf": None, "sheet": None, "pages": 0})

    outdir = os.path.join(os.getcwd(), "slidesnap-out", video_id(url))

    def work():
        def say(msg):
            with JOB_LOCK:
                JOB["log"].append(msg)

        def finish(**kw):
            with JOB_LOCK:
                JOB.update(kw)
                JOB["running"] = False
                JOB["done"] = True

        try:
            for note in notes:
                say(f"note: {note}")
            pdf, sheet, picked = run_pipeline(url, items, outdir, frames=frames,
                                              progress=say)
            finish(outdir=outdir, pdf=os.path.basename(pdf),
                   sheet=os.path.basename(sheet), pages=len(picked))
        except Exception as exc:  # never let a worker kill the server
            say(f"error: {exc}")
            finish(error=str(exc))

    threading.Thread(target=work, daemon=True).start()
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "yt-slidesnap"

    def log_message(self, *args):
        pass  # keep the console quiet; the page shows progress

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE)
        elif path == "/status":
            with JOB_LOCK:
                self._json(200, dict(JOB))
        elif path.startswith("/file/"):
            self._serve_file(unquote(path[len("/file/"):]))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            self._send(404, "not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._json(400, {"error": "malformed request"})
            return
        error = start_job(payload)
        self._json(409 if error else 200, {"error": error} if error else {"ok": True})

    def _serve_file(self, rel):
        """Serve only inside the current run's output folder."""
        with JOB_LOCK:
            outdir = JOB["outdir"]
        if not outdir:
            self._send(404, "no output yet", "text/plain")
            return
        root = os.path.realpath(outdir)
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            self._send(403, "forbidden", "text/plain")  # no traversal out of the run
            return
        if not os.path.isfile(target):
            self._send(404, "not found", "text/plain")
            return
        with open(target, "rb") as fh:
            self._send(200, fh.read(), mimetypes.guess_type(target)[0] or "application/octet-stream")


def cmd_serve(args):
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:  # port taken - take any free one instead of failing
        httpd = ThreadingHTTPServer((args.host, 0), Handler)
    url = f"http://{args.host}:{httpd.server_address[1]}/"
    print(f"yt-slidesnap web UI on {url}")
    print("ctrl-c to stop")
    print(f"output goes to {os.path.join(os.getcwd(), 'slidesnap-out')}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


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
    r.add_argument("--no-watermark", action="store_true",
                   help="don't stamp the repo line at the foot of PDF pages")
    r.add_argument("--dry-run", action="store_true",
                   help="show how the timestamps were understood, then exit")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("selftest", help="check the timestamp parser")
    t.set_defaults(func=cmd_selftest)

    s = sub.add_parser("serve", help="run the local web UI")
    s.add_argument("--host", default="127.0.0.1",
                   help="interface to bind (default 127.0.0.1, local only)")
    s.add_argument("--port", type=int, default=8765,
                   help="port (default 8765; falls back to a free one if taken)")
    s.add_argument("--no-open", action="store_true",
                   help="don't open a browser window")
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
