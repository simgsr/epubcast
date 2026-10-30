# epubcast: turn EPUB e-books into chaptered M4B audiobooks

<!-- Absolute URL on purpose: `imgs/` is excluded from the sdist, so a relative path renders
     as a broken image anywhere the README is read outside a clone (PKG-INFO, mirrors). -->
![epubcast GUI on macOS](https://raw.githubusercontent.com/simgsr/epubcast/main/imgs/mac.png)

**This is a fork of [audiblez](https://github.com/santinic/audiblez), renamed and re-scoped.**
It runs Kokoro's text-to-speech through PyTorch by default (portable, works on any machine)
and can speed it up with [MLX](https://github.com/Blaizzy/mlx-audio) on the Apple GPU.
Upstream audiblez runs Kokoro purely on the CPU on a Mac, which is slow — about 60
characters per second on an M2.

### What this fork is for

- **Kokoro on torch by default**, so a plain `pip install .` works everywhere. The
  accelerator — MLX on the Apple GPU — is an opt-in extra (`pip install ".[mlx]"`, Apple
  Silicon only). MLX is faster, but it has been linked to kernel panics on some machines, so
  torch is the safe default.
- **Measured 906 characters/second on an M5 Max on MLX**, against 238 for the same model on
  torch — 3.8x. A 15.7-hour audiobook took **87 minutes on torch, and about 20 on MLX**.
- **The time estimate is measured, not guessed.** Upstream hardcodes 50 chars/sec whenever
  CUDA is missing, so every Mac run mis-reported its ETA by roughly 4x.
- **Better text extraction**, which matters more than speed for a listenable result: page
  numbers, footnote markers, inline tables of contents, and index/copyright sections are no
  longer read aloud. See [What gets read](#what-gets-read).
- **Chapters follow the e-book's table of contents**, not its file layout, so the GUI list
  and the `.m4b` chapter marks match the book — whether it ships as one 15-hour XHTML file or
  as 209 fragments that are really 18 chapters.

Everything else — the voices, the GUI, the languages — is upstream's work and is unchanged.

[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) is a text-to-speech model with just 82M
params and very natural sounding output, released under the Apache licence and trained on
< 100 hours of audio. It supports: 🇺🇸 🇬🇧 🇪🇸 🇫🇷 🇮🇳 🇮🇹 🇯🇵 🇧🇷 🇨🇳

## How to install

You need `espeak-ng` and `ffmpeg`, plus Python 3.10–3.12 (spacy has no wheels for 3.13+).

This fork is **not published to PyPI** — install it from a clone:

```bash
brew install ffmpeg espeak-ng                       # on Mac 🍏
git clone https://github.com/simgsr/epubcast
cd epubcast
pip install .
```

That installs the default **torch** backend — Kokoro through PyTorch — which works on any
machine. On Apple Silicon you can opt into the faster **MLX** backend, which runs on the
Apple GPU (it pulls in `mlx-audio`):

```bash
pip install ".[mlx]"      # Apple Silicon only — optional accelerator
```

> The `[mlx]` extra only installs on Apple Silicon; it is ignored elsewhere, where the
> default torch backend is all you have (and all you need). MLX is faster but has been
> linked to kernel panics on some machines, which is why it is no longer the default.

**On a PC / Linux**, the one-liner that installs everything you need (core + edge) is:

```bash
pip install -r requirements.txt
```

That file mirrors `pyproject.toml`: it has the core dependencies (which include `kokoro`) and
the `[edge]` extra, so `edge-tts` is installed too. (The `[gui]` extra is left out; add it
with `pip install ".[gui]"` if you want the desktop UI.)

### Reproducible installs

`pip install .` and `requirements.txt` both resolve from version *ranges*, so two installs a
few weeks apart can land on different versions — newer `spacy`, newer `torch`, a different
`mlx-audio`. When you need the same environment twice, use one of the two pinned paths:

```bash
pip install poetry
poetry install                 # reads poetry.lock, not the ranges
```

```bash
pip install -r requirements.lock   # same pins, for a pip-only workflow (core + edge)
```

`requirements.lock` is generated from the lockfile with
`poetry export -f requirements.txt --without-hashes --without-urls --extras edge`; CI
regenerates it and fails if the committed copy differs. CI also runs `poetry check --lock`, so
a `pyproject.toml` change that was not carried into the lock fails the build instead of
drifting quietly. `.python-version` pins the interpreter at 3.12.

Model weights are pinned too. Both engines load from a fixed upstream commit, so a Hugging
Face repo update cannot change the audio under unchanged code. `--repo-id` overrides the repo,
and a repo you name yourself floats on its branch unless you pin it.

To also get the **Edge backend** — Microsoft's online neural voices, including real
Cantonese and native traditional-script reading — add the extra:

```bash
pip install ".[edge]"
```

Edge needs network at synthesis time and sends the book's text to Microsoft's servers, so
it is never chosen by `auto`; pick it explicitly with `--backend edge`. Requests are
batched — several consecutive sentences per network call, rather than one round trip per
sentence — and a few batches at a time can be in flight together, ramping up gradually and
dropping back to one at a time the moment a request needs a retry, since Edge is known to
throttle requests fired back to back.

### Environment variables

Both are optional; they are auto-detected from a Homebrew install and only need setting if
detection fails.

| Variable | Purpose |
|---|---|
| `ESPEAK_LIBRARY` | Path to `libespeak-ng.dylib`/`.so`/`.dll` |
| `ESPEAK_DATA_PATH` | Path to the `espeak-ng-data` directory. The MLX path needs this because `misaki`'s `espeakng-loader` ships a hardcoded build-machine path that does not exist on your system |

Then you can convert an .epub directly with:

```
epubcast book.epub -v af_sky
```

Everything is written to an **`audiobooks/`** folder, created if missing. Point it elsewhere
with `-o`:

```
epubcast book.epub -v af_sky -o ~/Audiobooks
```

It first creates one `.wav` per chapter, then produces the `book.m4b` you can play in VLC or
any audiobook player. The `.m4b` is only produced if `ffmpeg` is installed.

### What you can delete afterwards

Once the `.m4b` exists, **everything else in the folder is disposable**. The `.m4b` already
contains the audio, the cover art as an embedded image stream, and the chapter marks in its
metadata:

| Leftover | Still needed? |
|---|---|
| `*.wav` (one per chapter) | No — but keeping them lets a re-run skip re-synthesizing, which is useful if you want to change the cover or chapter titles and re-mux |
| `cover` | No — already embedded in the `.m4b` |
| `chapters.txt` | No — already written into the `.m4b` metadata |

The wavs are by far the largest artifact: a 15-hour audiobook produces roughly 2.5 GB of
them against a 490 MB `.m4b`. `audiobooks/` is git-ignored apart from its `.gitkeep`, so none
of it can be committed by accident.

## How to run the GUI

The GUI is a simple graphical interface to use epubcast:

```
brew install ffmpeg espeak-ng
pip install ".[gui]"
```

Then you can run the GUI with:
```
epubcast-ui
```

The GUI shows which backend will actually run, and lets you pick one explicitly. torch is
the default; the torch device selector is greyed out only when MLX is selected, since it has
no effect there.

**Audition a voice before committing to it.** The `Preview` button next to the voice selector
synthesizes a short sample with the selected engine, voice and speed, and plays it. The
`Sample` dropdown chooses what it reads:

| Sample | What it speaks |
|---|---|
| `Auto` | Whatever the selected voice can actually pronounce: English for English voices, Chinese for Chinese ones, both for an Edge *multilingual* voice, and the open chapter for any other language |
| `English` | The built-in English sample |
| `Chinese` | The built-in Chinese sample |
| `Current chapter` | The opening of the section in the editor — the most representative check before a run |

Previews use the same pipeline and text normalization as the real synthesis, so what you hear
is what you get. `Preview chapter`, above the editor, reads the edited text the same way, so a
correction can be checked without starting a conversion.

Playback uses `afplay` on macOS and falls back to `ffplay` (from ffmpeg), then to the
platform's own player.

## Sleep during a long run

A full-length novel takes hours, and an idle Mac suspending halfway through stops the
synthesis with it. So for as long as a conversion is running — GUI or CLI, right through the
final ffmpeg pass that writes the `.m4b` — epubcast holds a wake lock (`caffeinate` on macOS,
`systemd-inhibit` on Linux, `SetThreadExecutionState` on Windows) and drops it the moment the
book is finished. The display is left alone and may sleep as usual.

Closing the lid still suspends the machine; no application can veto that. If the lock cannot
be taken, the run prints a warning and carries on.

## Windows and Linux

This fork targets Apple Silicon. Linux is covered by one CI job that installs the default
torch backend and converts a book end to end, so it is known to work there, but it is not a
platform this fork optimises for. Windows is not tested at all.

The default torch backend is what runs on either — `pip install .` — but if that is your
platform you want [the upstream project](https://github.com/santinic/audiblez) instead,
which supports it properly and has CI across all three.

## Speed

By default the audio is generated using a normal speed, but you can make it up to twice slower or faster by specifying a speed argument between 0.5 to 2.0:

```
epubcast book.epub -v af_sky -s 1.5
```

Measured on an M5 Max, synthesizing the same paragraph with `af_sky`:

| Backend | Throughput | Faster than realtime |
|---|---|---|
| MLX (`Kokoro-82M-bf16`) | **906 chars/sec** | 47x |
| Torch (`Kokoro-82M`, CPU) | 238 chars/sec | 12x |

## Supported Voices

Use `-v` option to specify the voice to use. Available voices are listed here.
The first letter is the language code and the second is the gender of the speaker e.g. `im_nicola` is an italian male voice.

[For hearing samples of Kokoro-82M voices, go here](https://claudio.uk/posts/audiblez-v4.html)

| Language                  | Voices                                                                                                                                                                                                                                     |
|---------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 🇺🇸 American English     | `af_alloy`, `af_aoede`, `af_bella`, `af_heart`, `af_jessica`, `af_kore`, `af_nicole`, `af_nova`, `af_river`, `af_sarah`, `af_sky`, `am_adam`, `am_echo`, `am_eric`, `am_fenrir`, `am_liam`, `am_michael`, `am_onyx`, `am_puck`, `am_santa` |
| 🇬🇧 British English      | `bf_alice`, `bf_emma`, `bf_isabella`, `bf_lily`, `bm_daniel`, `bm_fable`, `bm_george`, `bm_lewis`                                                                                                                                          |
| 🇪🇸 Spanish              | `ef_dora`, `em_alex`, `em_santa`                                                                                                                                                                                                           |
| 🇫🇷 French               | `ff_siwis`                                                                                                                                                                                                                                 |
| 🇮🇳 Hindi                | `hf_alpha`, `hf_beta`, `hm_omega`, `hm_psi`                                                                                                                                                                                                |
| 🇮🇹 Italian              | `if_sara`, `im_nicola`                                                                                                                                                                                                                     |
| 🇯🇵 Japanese             | `jf_alpha`, `jf_gongitsune`, `jf_nezumi`, `jf_tebukuro`, `jm_kumo`                                                                                                                                                                         |
| 🇧🇷 Brazilian Portuguese | `pf_dora`, `pm_alex`, `pm_santa`                                                                                                                                                                                                           |
| 🇨🇳 Mandarin Chinese     | `zf_xiaobei`, `zf_xiaoni`, `zf_xiaoxiao`, `zf_xiaoyi`, `zm_yunjian`, `zm_yunxi`, `zm_yunxia`, `zm_yunyang`                                                                                                                                 |

For more detaila about voice quality, check this document: [Kokoro-82M voices](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)

### Edge voices (online)

The Edge backend uses Microsoft's neural voices — no model download, but it needs network
and sends the book's text to Microsoft's servers. Pick one with `--backend edge`:

| Language                  | Voices                                                                                                                                                                                                                                     |
|---------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 🇺🇸 American English     | `en-US-AriaNeural`, `en-US-JennyNeural`, `en-US-GuyNeural`, `en-US-EmmaNeural`, `en-US-BrianNeural`, `en-US-AndrewNeural`, `en-US-ChristopherNeural`, `en-US-MichelleNeural`                                                               |
| 🇬🇧 British English      | `en-GB-LibbyNeural`, `en-GB-MaisieNeural`, `en-GB-RyanNeural`, `en-GB-SoniaNeural`, `en-GB-ThomasNeural`                                                                                                                                  |
| 🇦🇺 Australian English   | `en-AU-NatashaNeural`, `en-AU-WilliamMultilingualNeural`                                                                                                                                                                                   |
| 🇨🇦 Canadian English     | `en-CA-ClaraNeural`, `en-CA-LiamNeural`                                                                                                                                                                                                    |
| 🇮🇳 Indian English       | `en-IN-NeerjaNeural`, `en-IN-PrabhatNeural`                                                                                                                                                                                                |
| 🇨🇳 Mandarin (mainland) | `zh-CN-XiaoxiaoNeural`, `zh-CN-XiaoyiNeural`, `zh-CN-YunjianNeural`, `zh-CN-YunxiNeural`, `zh-CN-YunxiaNeural`, `zh-CN-YunyangNeural`                                                                                                     |
| 🇹🇼 Mandarin (Taiwan)   | `zh-TW-HsiaoChenNeural`, `zh-TW-HsiaoYuNeural`, `zh-TW-YunJheNeural` — reads traditional script natively                                                                                                                                    |
| 🇭🇰 Cantonese (Hong Kong) | `zh-HK-HiuGaaiNeural`, `zh-HK-HiuMaanNeural`, `zh-HK-WanLungNeural`                                                                                                                                                                       |
| 🇪🇸 Spanish              | `es-ES-AlvaroNeural`, `es-ES-ElviraNeural`, `es-ES-XimenaNeural`, `es-MX-DaliaNeural`, `es-MX-JorgeNeural`                                                                                                                               |
| 🇫🇷 French               | `fr-FR-DeniseNeural`, `fr-FR-EloiseNeural`, `fr-FR-HenriNeural`, `fr-FR-RemyMultilingualNeural`, `fr-FR-VivienneMultilingualNeural`                                                                                                       |
| 🇩🇪 German               | `de-DE-ConradNeural`, `de-DE-KatjaNeural`, `de-DE-FlorianMultilingualNeural`, `de-DE-SeraphinaMultilingualNeural`                                                                                                                         |
| 🇮🇹 Italian              | `it-IT-DiegoNeural`, `it-IT-ElsaNeural`, `it-IT-IsabellaNeural`                                                                                                                                                                            |
| 🇯🇵 Japanese             | `ja-JP-KeitaNeural`, `ja-JP-NanamiNeural`                                                                                                                                                                                                   |
| 🇧🇷 Brazilian Portuguese | `pt-BR-AntonioNeural`, `pt-BR-FranciscaNeural`, `pt-BR-ThalitaMultilingualNeural`                                                                                                                                                          |
| 🇮🇳 Hindi                | `hi-IN-MadhurNeural`, `hi-IN-SwaraNeural`                                                                                                                                                                                                   |

This is a curated subset of the ~400-voice catalog; any other Edge voice name works if you
type it in full.

### Traditional Chinese

There is no separate traditional-Chinese voice to pick, in this or any other Kokoro build: a
voice is a speaker's timbre, and the script a book is printed in is handled before the model
ever sees the text. What *was* missing is that handling. Kokoro's Chinese front end is jieba
plus pypinyin, whose dictionaries are keyed on simplified characters, so traditional text fell
back to character-by-character lookup and lost the two things those dictionaries provide:
polyphone disambiguation (乾乾淨淨 read as *qián qián jìng jìng* instead of *gān gān jìng
jìng*) and word segmentation (臺灣的 split as 臺 / 灣的).

The one that hurts most is the aspect particle 著: 她笑著說 is read *xiào **zhù** shuō*
instead of the neutral-tone *xiào **zhe** shuō*, and that construction turns up in nearly
every paragraph of a novel.

This fork converts traditional text to simplified before phonemizing it, via OpenCC's `tw2s`
(chosen over the plainer `t2s` because it is the one that fixes 著 while leaving 著作 and 著名
alone, and over `tw2sp` because that would also rewrite regional vocabulary — 軟體 into 软件 —
which changes the author's words rather than their script). Nothing
is lost in doing so — Mandarin pronunciation does not depend on the script — and the text
written into the `.m4b` (titles, chapter marks) keeps the book's original characters. It
happens automatically for any of the `z*` voices; Japanese and Korean books are never
touched.

One real limitation remains: a traditional-script book is narrated in **Mandarin**. Kokoro
ships no Cantonese voice, so a Hong Kong text will be read correctly, character for
character, but in Mandarin.

The Edge backend lifts both halves of that limitation. A `zh-TW` voice reads traditional
script natively — the conversion above is skipped for it — and `zh-HK` voices are real
Cantonese. Use them with `--backend edge`:

```
# Traditional script, read as written, in Mandarin
epubcast book.epub -v zh-TW-HsiaoChenNeural --backend edge

# Traditional script, read in Cantonese
epubcast book.epub -v zh-HK-HiuMaanNeural --backend edge
```

## Choosing a backend

The torch backend is the default and is used automatically. Pick an engine explicitly with
`--backend`:

```
epubcast book.epub -v af_sky --backend torch   # default: Kokoro through PyTorch
epubcast book.epub -v af_sky --backend mlx     # force MLX (Apple Silicon only, needs ".[mlx]")
epubcast book.epub -v zh-TW-HsiaoChenNeural --backend edge   # Microsoft's online TTS (needs the [edge] extra + network)
epubcast book.epub -v af_sky --backend auto    # default: torch when installed, else MLX
```

MLX runs the model in `bf16` where Torch runs `fp32`. Output is equivalent in duration
(within 0.25%) and loudness, but not sample-identical: small timing differences accumulate
across a chapter. Use `--backend torch` if you want upstream's exact output.

Point either backend at a different model repo, for example a quantized one:

```
epubcast book.epub -v af_sky --repo-id mlx-community/Kokoro-82M-8bit
```

If you have an Nvidia GPU, `--cuda` still selects it for the torch backend. It is ignored on
Apple Silicon, where MLX already runs on the GPU.

## What gets read

An e-book contains a lot of text that should not be spoken. This fork drops, before synthesis:

- **Page numbers** embedded in the text, whether marked with `epub:type="pagebreak"` or by a
  page-number CSS class
- **Footnote and endnote markers** — the superscript `12` that otherwise gets read as a number
  in the middle of a sentence — and the note bodies themselves
- **Inline tables of contents**, and any line that is nothing but internal links
- **Index, copyright, colophon and contents sections**, identified from the table of contents

It also fixes text that upstream *misses*: text is extracted by walking the document rather
than matching a fixed tag list, so prose in `<div>`s, table cells, or separated by `<br>` is
no longer dropped. One Chinese novel went from **39 characters extracted to 107,220** — it
had no `<p>` tags at all. Nested `<li><p>` text is also no longer read twice.

Sentence terminators are CJK-aware, so Chinese and Japanese lines no longer get a stray Latin
full stop appended after `。`.

## Chapters

A chapter is one entry in the e-book's table of contents, not one file. The contents is read
in spine (reading) order, and a chapter runs from its entry to the next one across however
many files that takes:

- **Books split into fragments** — a Calibre-style `index_split_006.html` holding just the
  heading, then `_008` … `_018` holding the text — become one chapter per contents entry.
  Files the contents never names are continuations of the chapter before them. One such book
  went from 209 chapters (one per file, named `index_split_042`) to its actual 18.
- **Books that ship as one enormous XHTML file** are split at the anchors the contents points
  to, so the `.m4b` gets real chapter marks instead of one 15-hour track.
- **Chapter titles come from the contents** rather than filenames — a book whose files are
  `0.xhtml`, `1.xhtml` … used to produce chapters named `0`, `1`, `2`.

Two things are deliberately left alone. A file that trails the last contents entry in a book
that otherwise keeps one file per chapter is treated as back matter the contents forgot — an
index, a notes section — and stays its own chapter rather than being glued onto the end of the
final one. And a book with no usable contents keeps one chapter per file, as before.

Front and back matter the contents names (`Contents`, `Copyright`, `Index`, `About the
Author`, …) is listed but left unticked, in the GUI and on the CLI alike.

## Manually pick chapters to convert

Sometimes you want to manually select which chapters/sections in the e-book to read out loud.
To do so, you can use `--pick` to interactively choose the chapters to convert (without running the GUI).


## Help page

For all the options available, you can check the help page `epubcast --help`:

```
usage: epubcast [-h] [-v VOICE] [-p] [-s SPEED] [-c] [-o FOLDER]
                [-b {auto,torch,mlx,edge}] [--lang CODE] [--repo-id REPO]
                epub_file_path

positional arguments:
  epub_file_path        Path to the epub file

options:
  -h, --help            show this help message and exit
  -v VOICE, --voice VOICE
                        Choose narrating voice: a, b, e, f, h, i, j, p, z
  -p, --pick            Interactively select which chapters to read in the
                        audiobook
  -s SPEED, --speed SPEED
                        Set speed from 0.5 to 2.0
  -c, --cuda            Use an Nvidia GPU via Torch. Ignored on Apple Silicon
  -o FOLDER, --output FOLDER
                        Output folder for the audiobook and intermediate files
                        (default: audiobooks/, created if missing)
  -b {auto,torch,mlx,edge}, --backend {auto,torch,mlx,edge}
                        TTS engine: torch is the default (portable); auto picks it
                        when installed; mlx is Apple-Silicon only and faster; edge
                        is Microsoft's online TTS (needs network)
  --lang CODE           Language code: Kokoro (a, b, e, f, h, i, j, p, z) or an
                        Edge locale (en-US, zh-TW, zh-HK, ...). Defaults to the
                        first letter of the voice name, or the locale for an Edge
                        voice; set it when using a custom .pt voice
  --repo-id REPO        Hugging Face model repo to use instead of the backend
                        default

example:
  epubcast book.epub -v af_sky

to run GUI just run:
  epubcast-ui
```

## Voices beyond the built-in list

Kokoro's `load_voice` accepts more than the names above, and both Kokoro backends pass it through:

```
epubcast book.epub -v af_heart,af_bella          # blend two voices (averaged)
epubcast book.epub -v /path/to/voice.pt --lang a # a custom voice pack
```

Pass `--lang` with a `.pt` path, since the language can no longer be read off the voice name.

## Author

Upstream audiblez by [Claudio Santini](https://claudio.uk) in 2025, distributed under MIT licence.

Related Article: [Audiblez v4: Generate Audiobooks from E-books](https://claudio.uk/posts/audiblez-v4.html)

This fork is maintained at [simgsr/epubcast](https://github.com/simgsr/epubcast),
under the same MIT licence.
MLX inference via [mlx-audio](https://github.com/Blaizzy/mlx-audio).
