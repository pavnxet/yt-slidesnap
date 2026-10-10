# yt-slidesnap

Give it a lecture video and the timestamps where each question is on screen. Get back a
clean PDF of the slides — one page each, no answers, no video downloaded.

Built for the tedious case: a 2.5-hour recorded class with 40 questions in it, where you
want the question slides in a document instead of scrubbing a timeline by hand.

```
$ yt-slidesnap run "https://www.youtube.com/watch?v=XXXXXXXXXXX" --file timestamps.txt
url  : https://www.youtube.com/watch?v=XXXXXXXXXXX
out  : ./slidesnap-out/XXXXXXXXXXX
found: 16 slides (Q22..Q37)
  Q22: picked 4/8  (error 8.2 vs worst 60.8)
  Q23: picked 2/8  (error 7.9 vs worst 36.0)
  ...

pdf  : ./slidesnap-out/XXXXXXXXXXX/Q22-Q37.pdf  (16 pages)
check: ./slidesnap-out/XXXXXXXXXXX/auto_picked.png
```

16 slides in **~25 seconds**, including resolving the stream URL.

## Install

```bash
pipx install git+https://github.com/pavnxet/yt-slidesnap
# or
pip install git+https://github.com/pavnxet/yt-slidesnap
# or just clone it and use the script directly
git clone https://github.com/pavnxet/yt-slidesnap && cd yt-slidesnap
pip install -r requirements.txt
python slidesnap.py --help
```

**You do not need ffmpeg installed.** A full ffmpeg build ships inside
[`imageio-ffmpeg`](https://pypi.org/project/imageio-ffmpeg/) and the tool always uses that
one. (`yt-dlp` is used to resolve the stream and is installed as a dependency.)

## Quick start

Write your timestamps in a file — anything with an id and at least one `hh:mm:ss` stamp per
line works, so you can paste them however you already have them:

```
Q22: Question 01:34:21 | Answer 01:34:53 (C)
Q23: Question 01:38:45 | Answer 01:39:16 (D)
```

Markdown tables work too, including ones with several later timestamps per row:

```
| Q# | Question Start | Correct Option Timestamp | Answer Start | Correct Option |
| -- | -------------- | ------------------------ | ------------ | -------------- |
| Q19 | 01:41:04 | 01:44:16 | 01:41:27 | C |
```

Then:

```bash
yt-slidesnap run "<video-url>" --file timestamps.txt

# inline instead of a file
yt-slidesnap run "<video-url>" --text "Q22: Question 01:34:21 | Answer 01:34:53 (C)"

# or pipe it
cat timestamps.txt | yt-slidesnap run "<video-url>"
```

The **first** stamp on a line is when the slide appears. Every later stamp is a ceiling and
the **earliest** one wins. So in the table above the window is `01:41:04 → 01:41:27` (Answer
Start), not `→ 01:44:16` — no frame is ever taken at or past the earliest later event, so an
answer cannot leak into the PDF, and the window can't drift across the next question.

See [`examples/timestamps.txt`](examples/timestamps.txt) for a full list.

## Web UI

Prefer clicking to typing flags? There's a local web page — no account, no cloud, binds to
`127.0.0.1` only:

```bash
yt-slidesnap serve
```

It opens `http://127.0.0.1:8765` in your browser. Paste the URL and the timestamps, click
**Extract slides**, and watch the progress log. When it finishes you get a link to the PDF
and a preview of the contact sheet, so you can check the picks without leaving the page.

Output still lands in `./slidesnap-out/<video-id>/`. One run at a time; a second click while
a run is in progress is rejected rather than started.

| Flag | Default | |
|---|---|---|
| `--port N` | `8765` | falls back to any free port if taken |
| `--host H` | `127.0.0.1` | local only by default |
| `--no-open` | off | don't auto-open a browser window |

It is a single-user local tool with no authentication — it serves the output folder of the
current run and nothing else. Don't expose it to a network you don't trust.

## Why the frames come out clean

Naively grabbing a frame tends to catch the presenter standing in front of the slide. So
each slide window is sampled 8 times, and the tool picks the frame closest to the
**per-pixel median** of those 8.

The slide is static; the presenter is not. So the median *is* the slide with the presenter
erased — and the frame closest to it is the one where they cover it least. No ML, no face
detection, just numpy. If a pick still looks wrong:

```bash
yt-slidesnap run "<url>" --file ts.txt --frames 16        # more candidates
yt-slidesnap run "<url>" --file ts.txt --override Q22=5   # pin one manually
```

Every candidate is kept, so re-picking costs nothing.

The printed `error X vs worst Y` tells you how confident the pick was. If `X` is close to
`Y`, every candidate was dirty and you should raise `--frames`.

## Output

```
slidesnap-out/<video-id>/
├── Q22-Q37.pdf        the deliverable, one captioned slide per page
├── auto_picked.png    contact sheet of every pick - eyeball this first
├── frames/Q22.png     the chosen slides
└── candidates/        all 8 candidates per slide, for re-picking
```

Each page is `[Q22  at 1:34:21]` on top, the slide, then `Made with ❤ by
github.com/pavnxet/yt-slidesnap` in the footer strip. Both strips sit outside the slide, so
neither ever covers the question. Turn either off with `--no-label` / `--no-watermark`.

## Flags

| Flag | Default | |
|---|---|---|
| `--out DIR` | `./slidesnap-out/<video-id>` | output folder |
| `--frames N` | `8` | candidates per slide |
| `--workers N` | `4` | slides extracted concurrently |
| `--override ID=N` | — | force candidate N, repeatable |
| `--client NAME` | `android` | yt-dlp `player_client` for YouTube |
| `--format SEL` | `bv*[height<=1080]/...` | yt-dlp format selector |
| `--no-label` | off | don't caption pages with `Q22  at 1:34:21` |
| `--no-watermark` | off | don't stamp the repo line at the foot of pages |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `could not resolve a stream URL` | Try `--client android_vr` / `tv_embedded` / `ios`, or `--format "best[height<=720]"`. Age-restricted or members-only videos need cookies exported and passed to yt-dlp. |
| HTTP 403 from ffmpeg | The stream URL is locked to the client that requested it. Only YouTube's `android` client is known to hand ffmpeg a usable URL. |
| Frames are 640x360 | Expected on YouTube. `android` is the only client whose URL ffmpeg can fetch, and it caps at 360p — fine for slide text, since the tool upscales 2x when writing the PDF. Going higher needs a JS runtime for yt-dlp's `nsig` handling, and that client's URL may still 403. |
| A slide is missing from the PDF | A `WARNING: N slide(s) produced no frames` line says which. Usually a timestamp past the end of the video. |
| A pick has the presenter over the text | `--frames 16`, re-run, or `--override Q22=5`. |

Check the parser still behaves:

```bash
yt-slidesnap selftest
```

## Scope and limits

- Works with anything `yt-dlp` supports, not just YouTube — but only YouTube has been
  tested end to end.
- Extraction is a single frame per timestamp. If a slide is built up with animation, the
  captured frame is whatever is on screen at that moment.
- If the *slide itself* changes mid-window (unlikely, since the window ends at the answer
  stamp), the median becomes a blend of two slides and every candidate scores badly. The
  `error vs worst` numbers are how you spot it.

## Please use responsibly

This tool downloads frames of someone else's video. That is fine for study notes and
fair-use quotation, and not fine for republishing a lecture. Your output folder is
gitignored by default — please keep it that way, and check the terms of the platform and
the rights of the video's owner before sharing anything it produces.

## License

MIT — see [LICENSE](LICENSE).
