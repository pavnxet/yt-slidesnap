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
python slidesnap.py selftest
```

## Conventions and invariants

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
