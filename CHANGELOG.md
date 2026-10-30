# Changelog

## Unreleased

### Added

- `.python-version`, pinning the interpreter at 3.12. `requires-python` already bounded this
  at 3.10–3.12, but nothing told a tool manager which of those three to pick, and a machine
  with 3.14 installed fails resolution with an error that never names the boundary.
- A CI job running `poetry check --lock`. `poetry.lock` was the only pinned environment in
  the repo and nothing installed from it, so it could go stale indefinitely without any test
  noticing; the check fails the build when `pyproject.toml` moves and the lock does not.
- `test/test_find_chapters.py` to both CI matrices. It passed locally but no job ran it.
- **A `Preview` button next to the voice selector.** Choosing a voice by name alone meant
  committing a multi-hour run to one nobody had heard. It synthesizes a short sample with the
  selected engine, voice and speed through the same pipeline as the real run, and plays it.
  A `Sample` dropdown picks what it reads: `Auto` (English for English voices, Chinese for
  Chinese ones, both for an Edge *multilingual* voice, the open chapter otherwise), `English`,
  `Chinese`, or `Current chapter`. The sample-choice logic lives in `epubcast/preview.py` so
  it is testable without wxPython, and playback resolves to `afplay`, then `ffplay`, then the
  platform's own player.
- `requirements.lock`, a fully pinned pip path generated from `poetry.lock` with
  `poetry export`, plus a CI step that regenerates it and fails on any difference. The
  lockfile was previously the only pinned environment and nothing consumed it.
- A status bar, an engine badge in the top bar, and a one-line selection summary above the
  Start button stating how many chapters and characters the run will cover.

### Changed

- **The torch engine now loads its weights at a pinned upstream commit too.** Previously its
  entry in `DEFAULT_REVISIONS` only recorded what had been validated, because `kokoro` 0.9.4
  calls `hf_hub_download(repo_id=...)` with no revision argument and rejects a local path for
  `repo_id`. The seams kokoro does expose are enough to close it: `KModel` takes explicit
  config and weights paths, `KPipeline` takes a ready `KModel`, and `load_voice` takes a
  tensor. `PinnedTorchPipeline` resolves the config, the weights and the voice pack at the
  pinned commit and still runs kokoro's own inference. If any of that fails — no network, a
  kokoro upgrade that moves these seams — it falls back to the plain pipeline and says so,
  since unpinned weights beat no audiobook.
- **The MLX engine loads its weights at a pinned upstream commit.** A Hugging Face repo is
  mutable, so a repo update could swap the weights under unchanged code and change how a book
  sounds. `DEFAULT_REVISIONS` records the validated commit per engine and the MLX path passes
  it through `load_model`.
- A caller-supplied `--repo-id` is *not* given the default revision, since that commit names
  a commit in the default repo and would not resolve elsewhere. It stays on its branch unless
  pinned explicitly.
- **The window layout was rebuilt around hierarchy.** Before a book is open it shows a welcome
  state rather than a blank grey frame with two buttons in the corner. Once one is open, the
  settings column is a stack of single-bordered cards (Book / Engine / Voice / Output) above a
  footer that never scrolls — the Start button used to sit inside the scrolling area, below
  the fold in most windows. Section cards are parented to their `wx.StaticBox`, which removes
  the doubled inner frame and the "should be created as child of its wxStaticBox" warning.
- Labels now sit above their controls instead of in a label column, and each language row
  reads as a language rather than a code (`🇺🇸 English (US) (a)`). Helper text uses the
  system's grey so it stays legible in dark mode, where the hardcoded grey sat at the edge of
  legibility.
- `Speed` is a 0.5–2.0× slider instead of a free-text box, and the chapter table states exact
  character counts while the Book card uses a compact form (`1.4M characters`).

### Fixed

- The Linux CI job had been failing on every push for four runs, before any Python was
  installed: the pinned FFmpeg autobuild tag (`autobuild-2026-08-19-19-21`) had been pruned
  by upstream, so the download 404'd. Re-pinned to a tag that exists, with the hash taken
  from BtbN's own `checksums.sha256` and cross-checked against the downloaded archive.
- **The Edge backend could not synthesize a single word.** `edge-tts` 7.2.x sends
  `Sec-MS-GEC-Version: 1-...` on the websocket handshake, which the service now answers with
  `403 Invalid response status` on every request, so `Preview` failed after its four retries
  and a full run would have failed the same way. The `[edge]` floor is now `edge-tts (>=7.3.1)`,
  which sends `2-...`; `poetry.lock` already pinned 7.3.1, so only an environment installed
  from the old range (or an ad-hoc `pip install`) was affected.
- A second preview click froze the window until the first preview finished: `on_preview_chapter`
  joined every outstanding preview thread on the UI thread before starting another.
- Typing a non-numeric speed raised `ValueError` inside the change handler, after which the app
  would not start a run and never said why.
- The editor opened empty beside a heading naming a section, which read as a rendering failure.
  It now shows the first section immediately.
- Starting with no chapters ticked ran a full model load to produce nothing; it now says so.
- Previews needed `ffplay` specifically, which is absent on a machine with no ffmpeg; playback
  now resolves through `afplay` and then the platform player.
- `wx.EmptyImage` (deprecated) replaced with `wx.Image`.
- `test/test_main.py` downloads its books with `urllib` instead of shelling out to `wget`.
  `wget` is not part of macOS, and `os.system` discarded the failure, so all eight tests died
  on a bare `FileNotFoundError` naming the `.epub` with no hint at the cause. A failed
  download now skips, which is what the network guard was always meant to do.
- That file's `test_long_italian_sentence` was removed: it built a string and asserted
  nothing. `test_leigh_and_play_result` now skips when its untracked `leigh.epub` fixture is
  absent, and only plays the result when `ffplay` happens to be installed — playback is a
  convenience for a human, not a dependency of the project.
- The README's screenshot used a relative path, but `imgs/` is excluded from the sdist, so it
  rendered as a broken image anywhere the README is read outside a clone. It is now an
  absolute URL.
- `.gitignore` no longer ignores `*.sh` and `*.json` project-wide, which would have silently
  swallowed a legitimate script or fixture.

## 0.7.0

### Changed

- **Renamed the project to `epubcast`.** `audiblez-mlx` had stopped describing the thing: since
  0.6.0 the default engine is Kokoro on torch, and MLX is an opt-in extra, so the name promised
  a backend most installs never load. It also reused upstream's command and import names, which
  meant a machine with both installed had one silently shadow the other. The distribution, the
  import package and the console scripts are now `epubcast` and `epubcast-ui` throughout, so
  `pip install .` exposes a command that cannot be confused with upstream's. Nothing was ever
  published to PyPI under either name, so nothing installed breaks. Upstream
  [audiblez](https://github.com/santinic/audiblez) keeps its own name, its MIT licence and its
  attribution in the README — the 221 commits it contributed are untouched in this history.

### Removed

- The design plans for the two backends that were tried and dropped (Qwen3-TTS, CosyVoice2), so
  `docs/plans/` only documents engines the code actually has.
- `.github/FUNDING.yml`, which pointed at upstream's sponsor rather than this fork's — a fork
  should not collect sponsorship on someone else's behalf.

## 0.6.0

Note the breaking change: Qwen3-TTS was added and then removed again within this range, so it
never appeared in a tagged release.

### Added

- **Edge TTS backend** (`--backend edge`, `pip install ".[edge]"`): Microsoft's online neural
  voices, across 16 locales. It needs network at synthesis time and sends the book's text to
  Microsoft's servers, so `auto` never selects it — it has to be asked for by name. Requests
  are retried with a backoff sized to outlast throttling rather than a network round trip.
- **Traditional Chinese support.** Traditional script is converted to simplified before
  phonemization, because misaki's jieba/pypinyin dictionaries are simplified-keyed; the text
  written into the `.m4b` keeps the book's original characters. An Edge `zh-TW` voice reads
  traditional script natively and skips the conversion, and `zh-HK` voices are real Cantonese
  rather than Mandarin.
- **A wake lock for the length of a run** (`caffeinate` on macOS, `systemd-inhibit` on Linux,
  `SetThreadExecutionState` on Windows), held right through the final ffmpeg pass so an idle
  machine cannot suspend halfway through a multi-hour conversion. The display is left alone.
- **A language filter in the GUI voice dropdown**, ticked to English and Chinese by default
  (`en-US`, `en-GB`, `zh-CN`, `zh-TW` on Edge) so the list is not buried under ~50 voices.
- All output now defaults to an **`audiobooks/`** folder, created if missing; `-o` overrides.

### Changed

- **Edge TTS requests are now batched, and dispatched with adaptive concurrency.**
  Consecutive sentences are grouped into one request per up to 1500 characters instead of
  one network round trip per sentence, so a book with thousands of short sentences no
  longer pays thousands of serial round trips — a live run spent ~76 minutes of CPU time
  across more than a day of wall clock, almost all of it waiting on those individual round
  trips. A sentence already over the budget is still sent whole rather than split further,
  since Edge has no length limit worth working around. On top of that, up to 3 batches can
  be in flight at once — but only after a run of clean, first-attempt successes: Edge is
  documented to silently throttle (return empty audio) when requests are issued back to
  back, so concurrency starts fully serial and any retry drops it straight back to 1 rather
  than assuming the earlier throttling measurement no longer applies. Kokoro and torch keep
  the existing one-call-per-sentence, fully serial behavior, unaffected.
- **Chapters now follow the book's table of contents instead of its file layout.** A chapter
  starts at each contents entry and runs to the next one, over as many documents as that
  takes, so books that spill one chapter across a dozen files (`index_split_006.html` …
  `_018`) no longer produce a chapter — and a `.wav` — per fragment: one real book went from
  209 chapters to its actual 18. The same walk still splits single-file books at their
  contents anchors, and books with no usable contents still get one chapter per document.
  Documents are read in spine order rather than manifest order. Because chapter `.wav`
  filenames encode the chapter, a part-finished book from an earlier version will re-synthesize
  rather than resume.
- The GUI reports synthesis errors in a dialog instead of freezing with a disabled window.
- The CLI's chapter table lists chapter titles instead of filenames.
- `References` joins the titles treated as back matter, so it is listed but not ticked.
- The time estimate is seeded per language as well as per backend — a CJK character carries
  far more phonemes than a Latin one, and the first ETA for a Chinese book was optimistic by
  4-6x without it.
- **The `.m4b` is now encoded once instead of twice.** The chapter `.wav`s are concatenated
  into a lossless PCM intermediate and the single AAC encode happens at the end, so the audio
  no longer passes through a discarded 192k AAC generation on its way to 64k.
- The torch-CPU ETA seed now matches the measured ~238 chars/sec instead of upstream's 50,
  which mis-reported the first ETA by roughly 4x.

### Fixed

- Edge no longer silently drops a sentence it returned no audio for. That wrote a truncated
  chapter `.wav`, which the resume path then skipped over, making the loss permanent; the
  chapter is now failed and left unwritten so a re-run redoes it.
- An Edge voice name with a 3-letter subtag (`yue-CN-…`, `fil-PH-…`) is recognised instead of
  being rejected as "not an Edge TTS voice".
- Selecting a Kokoro backend with an Edge voice fails immediately rather than after mlx has
  downloaded a 339 MB model repo.
- A hand-typed voice in the GUI — a `.pt` path, a blend, or an uncurated Edge voice — survives
  ticking a language checkbox instead of being silently replaced.
- jieba can load its dictionary on setuptools 81-83, where `pkg_resources.resource_stream`
  is gone but the module still imports.
- The Start button in the GUI, broken by removing the `sys.path` hack.
- **The GUI now fits the screen it opens on.** The window is sized from the display's usable
  area — menu bar, dock and taskbar excluded — and every fixed pixel size in it scales with
  that, so the layout suits a 13" laptop and a 27" monitor alike. The parameters column had
  been squeezed to zero width by a text area whose hard-coded width the sizer had to honour,
  and the three boxes stacked in it asked for three times the tallest one's height; both are
  fixed, and the column scrolls rather than clipping when the screen is too short for it. A
  long book title or output path no longer stretches the window, and the chapter table's
  columns share out the width they actually have.
- **The macOS CI job asserted the pre-torch-default behavior** — MLX installed by default,
  torch absent — and failed on every run after torch became the default engine. It now
  verifies that `pip install .` gives torch with no MLX, that `pip install ".[mlx]"` makes MLX
  available while `auto` still prefers torch, and runs the end-to-end conversion on the
  default backend.
- **Three test files ran nothing when invoked directly** (`test_cli.py`,
  `test_find_chapters.py`, `test_main.py` had no `unittest.main()` block and exited 0 without
  running a single test). They now have entry points, skip cleanly when their gitignored
  `.epub` fixtures or the network are absent, and `test_cli.py`'s backend-check tests run in
  CI.

### Removed

- **Qwen3-TTS and the model registry it introduced** (breaking, but never released: it was
  added and removed within this range).
- Dead code: `unmark`/`unmark_element` (they referenced an undefined `Markdown` and nothing
  called them), a shadowed `_SPEAKABLE_RE`, unused imports, and commented-out UI blocks.

## 0.5.0 — first release of the `audiblez_mlx` fork

Forked from [santinic/audiblez](https://github.com/santinic/audiblez) at v0.4.9 and retargeted
at Apple Silicon. Measurements below are from an M5 Max.

### Added

- **MLX backend for Kokoro**, selected automatically on Apple Silicon. Measured **906
  chars/sec against 238** on the torch path — 3.8x. A 15.7-hour audiobook that took 87
  minutes on torch takes about 20 on MLX.
- `--backend {auto,torch,mlx}` to choose the engine, `--lang` to set the language code
  independently of the voice name, and `--repo-id` to select a different model repo
  (a quantized MLX build, for example).
- Books delivered as one enormous XHTML file are **split into chapters at their
  table-of-contents anchors**, so the `.m4b` gets usable chapter marks. Verified that split
  and unsplit text are byte-identical: the split moves boundaries, it never drops words.
- Chapter marks and the GUI chapter list are **named from the book's table of contents**
  instead of filenames.
- The GUI exposes the backend choice, greys out the torch device selector when MLX is in
  use, and accepts typed voices — comma blends (`af_heart,af_bella`) and paths to `.pt`
  voice packs, both of which the CLI already accepted.

### Changed

- **MLX is the default and only required backend.** torch and kokoro moved to a `[torch]`
  extra, removing a ~2GB dependency that did nothing useful on a Mac. `import audiblez.core`
  no longer imports torch at all.
- **The time estimate is measured rather than guessed.** It was a constant chosen by whether
  CUDA was present, so every Apple run reported the 50 chars/sec CPU figure — announcing
  nearly five hours for a book that took 87 minutes. It now recalibrates from real throughput.
- Text extraction walks the document instead of matching a fixed tag list. Text in `<div>`s,
  table cells and `<br>`-separated prose is no longer dropped: one Chinese novel went from
  **39 characters extracted to 107,220**, having no `<p>` tags at all.
- Sentence terminators are CJK-aware, so Chinese and Japanese lines no longer get a Latin
  full stop appended after `。`.
- CI targets macOS arm64 for the MLX path, with a Linux job covering the torch fallback.
  The upstream workflows installed the `audiblez` package from PyPI, which is not this code.

### Fixed

- **m4b creation was broken on any ffmpeg without libfdk_aac** (which is non-free, and
  excluded from Homebrew's default build). The concat step exited non-zero, the temp file was
  never written, and the run died on `unlink()` with `FileNotFoundError`. The encoder is now
  probed: libfdk_aac → aac_at → aac.
- Page numbers, footnote/endnote markers and their bodies, inline tables of contents, and
  index/copyright/contents sections are no longer read aloud.
- Text inside nested `<li><p>` was read twice.
- Empty paragraphs became a spoken `.` each.
- ffmetadata values are escaped, so chapter titles containing `=`, `;` or `#` no longer
  corrupt the chapter index.
- `audiblez.cli` used a bare `from core import main`, which only resolved when the working
  directory happened to be the package directory.
- The package no longer appends its own directory to `sys.path`, which had made its modules
  importable under bare names like `core` where they could shadow unrelated imports.
- The GUI no longer auto-opens `../epub/lewis.epub` on startup, a developer leftover.

### Removed

- `qwen-tts` and `epub-toc` dependencies — neither was imported anywhere, and `qwen-tts`
  pinned `transformers==4.57.3`, which blocked mlx-audio.
