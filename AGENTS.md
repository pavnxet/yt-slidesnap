# AGENTS.md

Guidance for AI coding agents working on **yt-slidesnap**. See `README.md` for user-facing
docs.

## What this is

A single-module CLI (`slidesnap.py`) that reads slide frames out of a video at the
timestamps a user supplies, and builds a PDF of them. No video file is ever downloaded:
ffmpeg seeks the resolved stream URL over HTTP byte ranges.

## Layout

| Path | |
|---|---|
| `slidesnap.py` | the whole tool - keep it single-file |
| `pyproject.toml` | packaging + `yt-slidesnap` console script |
| `examples/timestamps.txt` | sample input, and the format to stay compatible with |
| `learning/` | local notes, gitignored, not part of the published repo |

## Commands

```bash
python slidesnap.py run <url> --file examples/timestamps.txt
python slidesnap.py serve          # local web UI on 127.0.0.1
python slidesnap.py selftest
```

## Conventions and invariants

- **Keep it standard-library only.** The web UI uses `http.server` on purpose. Do not reach
  for Flask/FastAPI: "no extra install, works anywhere" is a feature of this tool.
- **`run_pipeline()` is the only implementation of the pipeline.** The CLI and the web UI
  both call it; `cmd_run` and `start_job` only differ in how they report progress. Never
  let a second copy of the extraction logic appear in the server.
- **Failures raise `SnapError`, not `sys.exit`.** `sys.exit` in a worker thread does not
  stop anything useful, so anything user-facing that the server also runs must raise.
- **Never assume a font can draw a glyph.** `small_font()` test-draws U+2764 and compares it
  against a guaranteed-missing codepoint, because a font without the glyph renders a tofu box
  rather than failing. The branding line falls back to `MARK_FALLBACK` (no heart) when no
  candidate font qualifies. Use `HEART` (U+2764) alone, never the emoji pair `U+2764 U+FE0F`:
  the variation selector is a second codepoint most text fonts lack.
- **Branding lives in `REPO` / `MARK` and is substituted into `PAGE` by `.replace()`.** The
  page is not an f-string on purpose: its CSS is full of braces.
- **Never add a `download()` function.** The tool's whole point is that the video is not
  stored. Frames are extracted by seeking the stream URL.
- **Use the bundled ffmpeg**, `imageio_ffmpeg.get_ffmpeg_exe()`. Never fall back to
  `ffmpeg` on PATH: builds shipped with download managers are often compiled with
  `--disable-everything` and have no H.264 decoder.
- **The answer timestamp is a ceiling, never a target.** `extract_window` derives the
  sampling rate from `answer - question` precisely so the last candidate lands before the
  answer. Keep that property - it is what makes the output answer-free.
- **Keep the timestamp parser loose.** It needs a leading id and one or more `hh:mm:ss`
  stamps on a line. Do not start matching on the words `Question` / `Answer`; users paste
  whatever their source gave them.
- **The ceiling is the *earliest* later stamp, not the second stamp.** Pasted tables often
  carry several later timestamps per row (answer discussion, correct-option reveal) in an
  arbitrary column order. Taking the second stamp would hand you a window that runs past the
  answer and across the next question, which also destroys the median auto-pick. Ids must
  also survive a leading `|`.
- **One ffmpeg pass per slide**, not one per candidate - `-vf fps=K/span -frames:v K`.
  Each invocation re-reads the MP4 header over HTTP, so invocation count dominates runtime.
- **Sample early in the window.** Late frames catch the presenter's annotations, which
  reveal the answer.
- Keep the YouTube `android` player client as the default: it is the only client whose
  stream URL ffmpeg can fetch without a 403.
- After changing the parser, run `python slidesnap.py selftest`. Extend it for new cases.

## Things not to commit

Extracted frames and PDFs are **someone else's copyrighted video content**. They are
gitignored; do not `git add -f` them.

## Verifying a change

`selftest` covers the parser. The frame-selection logic has no unit test - its check is the
`auto_picked.png` contact sheet the tool writes on every run. Eyeball it before claiming a
change works.

For the web UI, exercise the real endpoints rather than trusting the page:

```bash
python slidesnap.py serve --no-open &
curl -s localhost:8765/status                      # job state
curl -s -X POST localhost:8765/run -H 'Content-Type: application/json' \
     -d '{"url":"...","timestamps":"Q19 01:41:04 01:41:27"}'
curl -s localhost:8765/status                      # poll until done
curl -sI localhost:8765/file/<pdf>                 # must be application/pdf
curl -s -o /dev/null -w '%{http_code}' 'localhost:8765/file/..%2Fslidesnap.py'   # must be 403
```

A run started from the page must produce the *same* picks as the same run from the CLI - if
it doesn't, the server has grown its own copy of the pipeline.
