# Post-run tasks: verify Munger m4b, benchmark MLX, add an MLX TTS backend

Date: 2026-08-18
Branch: `feat/epub-text-extraction`
Status: planning only — no code written in this step

## Goal & scope

Once the running Munger conversion finishes, confirm the audiobook it produced is
actually correct, settle whether MLX-accelerated Kokoro is faster than the current
torch path, and — if it is — let epubcast use it via a selectable TTS backend without
losing Windows/Linux/CUDA support. Then clear four small deferred items already
identified during this work.

The headline deliverable is **a backend seam plus one MLX adapter**. Everything else
is verification or cleanup.

### Out of scope

- A Qwen3-TTS backend. Different architecture, ~20x the parameters, and its value
  (voice design / cloning) is a separate feature from "make the current voices
  faster". Needs its own plan and its own benchmark.
- Voice cloning, voice design, or any new voice UI.
- Restructuring `gen_audio_segments`' sentence splitting, chunking or progress stats.
- Replacing the torch backend or removing `kokoro` as a dependency.
- Fixing the double AAC encode in `concat_wavs_with_ffmpeg` -> `create_m4b`
  (concat encodes to AAC 192k, then the mux re-encodes to AAC 64k). Real quality
  bug, tracked as a follow-up, not folded in here.

## Affected files/modules

| File | Reason |
| --- | --- |
| `epubcast/backends.py` (new) | Backend factory + MLX adapter; keeps engine choice out of `core.py`. |
| `epubcast/core.py` | Three concerns: build the pipeline via the factory (`:117`), espeak data path in `set_espeak_library`, `gen_text` (`:246`). |
| `epubcast/ui.py` | `on_preview_chapter` (`:491`) constructs its own `KPipeline`; must use the factory or previews diverge from output. |
| `epubcast/cli.py` | New `--backend`, `--lang`, `--repo-id` flags. |
| `pyproject.toml` | Optional `[mlx]` extra; audit the unused `qwen-tts` / `epub-toc` deps. |
| `test/test_backends.py` (new) | Factory + adapter contract tests using a fake backend. |
| `test/test_find_chapters.py` | Stale dracula expectation (see step 7). |
| `README.md` | Document backend selection and the Apple-Silicon constraint. |

## Steps

### 0. Verify the finished run (blocking gate, no code)

Before touching anything, confirm the current output is sound — it is the only
end-to-end evidence the extraction + splitting work is correct on a real book.

- `ffprobe` the m4b: duration, 22 chapter marks, titles matching the TOC, cover stream present.
- Spot-listen chapter boundaries: start of ch1 (`#anchor1`, "Dedication") and the
  join between ch3 and ch4, where a merged stub section should read continuously.
- Confirm no chapter is silent or truncated (compare wav duration against chapter
  char count; flag outliers).

If this fails, stop and fix extraction before any backend work.

### 1. Clean MLX-vs-torch benchmark

Same paragraph, same voice (`af_sky`), same speed, machine otherwise idle — the
earlier 645 c/s MLX figure was measured under contention and 195 c/s torch came
from the live conversion, so neither is clean.

Record for both: chars/sec, realtime factor, model load time, peak RSS.
Then an audio A/B on identical input: MLX runs **bf16**, torch runs fp32.
Numeric check (duration within ~2%, RMS within ~10%) plus a listening check —
the numeric one cannot detect artifacts.

**Gate:** proceed to step 2 only if MLX is materially faster *and* the A/B is clean.
If bf16 is audibly worse, re-run against a higher-precision MLX repo before deciding.

### 2. Introduce the backend seam

Smallest design that works: keep `gen_audio_segments` **completely unchanged** by
making the MLX adapter satisfy the contract that loop already expects —

```python
# what core.py:232 does today, and must keep doing:
for gs, ps, audio in pipeline(sent_text, voice=voice, speed=speed, split_pattern=r'\n\n\n'):
    audio_segments.append(audio)   # audio must be a numpy array
```

So `backends.py` exposes:

```python
def get_pipeline(voice, lang_code=None, backend='auto', repo_id=None): ...
# returns a callable with Kokoro's signature, yielding (gs, ps, numpy_audio)
```

- `backend='torch'` returns `KPipeline` as today (zero behaviour change).
- `backend='mlx'` returns an adapter wrapping `mlx_audio` `model.generate(...)`,
  converting each `mx.array` to numpy so `np.concatenate` downstream is untouched.
- `backend='auto'` picks mlx on Apple Silicon when `mlx_audio` imports, else torch.
- `mlx_audio` is imported **lazily inside the adapter**, never at module import, so
  non-Darwin installs are unaffected.

Rejected alternative: a general TTS-engine plugin interface with its own audio type.
More code, more surface, and nothing else needs it yet — the adapter approach means
the hot loop and progress accounting keep working as-is.

### 3. espeak data path

`misaki`'s `espeakng-loader` ships a hardcoded CI path
(`/Users/runner/work/espeakng-loader/...`) and fails with
`Error processing file '.../phontab'`. `set_espeak_library()` already locates the
espeak *library* per-platform; extend the same function to also resolve and export
`ESPEAK_DATA_PATH` (`<prefix>/share/espeak-ng-data`), guarded so an already-set env
var wins. Must run before the MLX pipeline is constructed.

### 4. Wire up the call sites

Route all three pipeline constructions through the factory: `core.py:117`,
`core.py:246` (`gen_text`), `ui.py:491` (chapter preview). The UI one matters —
if preview and output use different engines, previews stop being representative.

### 5. CLI flags

- `--backend {auto,torch,mlx}` (default decided in Open questions).
- `--lang` — decouple language from `voice[0]`. Today `KPipeline(lang_code=voice[0])`
  means a `.pt` path voice yields lang code `/`. Falls back to `voice[0]`.
- `--repo-id` — select model repo (`hexgrad/Kokoro-82M`, `mlx-community/Kokoro-82M-bf16`,
  quantized variants). Also silences the `WARNING: Defaulting repo_id` noise.

Sanitize `voice` where it is interpolated into the wav filename (`core.py:125`) —
a `.pt` path or a comma-blended voice currently injects `/` and `,` into filenames.

### 6. Optional dependency

Add `[project.optional-dependencies] mlx = ["mlx-audio>=0.5.0", "misaki[en]"]`.
Do **not** add to base deps: `mlx` is Apple-Silicon only. Verify mlx-audio's pins
(numpy 2.5, transformers 5.15, tokenizers) actually co-exist with `kokoro==0.9.4`
and torch **in the project venv** — the benchmark used an isolated venv, so this is
untested. If they conflict, keep the extra documented as install-at-your-own-venv.

### 7. Deferred small items (independent, any order)

- **Stale test expectation** — `test/test_find_chapters.py::test_dracula_default_all_chapters`
  asserts `toc.xhtml` is included; the front-matter filter now excludes it. Needs the
  `../epub/` fixtures, which are absent locally. Update expectation + comment.
- **Malformed-epub fallback** — `蔡澜 - 蔡澜谈日本.epub` has `mlns=` instead of `xmlns=` on
  `<package>`, so ebooklib crashes in `_load_metadata` with
  `'NoneType' object has no attribute 'nsmap'`. Catch, repair the namespace in a temp
  copy, retry. Behind a warning so silently-broken books are visible.
- **Unused dependencies** — `qwen-tts` and `epub-toc` are declared in `pyproject.toml`
  but imported nowhere. Either drop them or explain them; `deptry` is already a dev
  dep and should catch this.
- **Release checks** — `/security-audit` and `/release-check` before anything ships.

## Test strategy

New `test/test_backends.py`, no model downloads:

- **Adapter contract** — fake `mlx_audio` module yielding stub results; assert the
  adapter yields 3-tuples whose third element is a `numpy.ndarray`, so
  `np.concatenate` in `gen_audio_segments` still works.
- **Factory dispatch** — `torch` returns the Kokoro pipeline; `mlx` raises a clear,
  actionable error when `mlx_audio` is absent; `auto` falls back to torch on
  non-Darwin (monkeypatch `platform.system`).
- **Lang decoupling** — `--lang` overrides `voice[0]`; a `.pt` voice path no longer
  produces lang code `/`.
- **Filename sanitizing** — voice strings containing `/` and `,` produce safe wav names.

Edge cases to cover explicitly: empty text, a single sentence shorter than the
split threshold, a voice name with no valid lang prefix, and `mlx_audio` present but
the model repo missing (network/offline error path).

Integration: one `skipUnless(darwin and mlx_audio available)` test synthesizing a
short phrase and asserting non-silent audio of plausible duration.

Regression: all 31 existing tests stay green; the torch path must be byte-identical
to today when `--backend torch`.

## Risks & trade-offs

- **bf16 quality.** Different precision from the current fp32 path. Mitigated by the
  step-1 A/B gate; the numeric check alone is insufficient, a human must listen.
- **Dependency conflict in one venv.** mlx-audio pulls numpy 2.5 / transformers 5.15
  alongside kokoro + torch. Untested together. Worst case the extra is documented as
  a separate environment rather than a real extra.
- **Default backend changes behaviour.** `auto` silently switches engine on Apple
  Silicon — good for this machine, surprising for a shared repo. See Open questions.
- **Upstream workaround rot.** The `ESPEAK_DATA_PATH` fix works around an
  `espeakng-loader` packaging bug; if upstream fixes it, our override should still be
  harmless because it only sets the var when unset.
- **Two engines to keep working.** Every future change to synthesis must be checked
  against both. Accepted because the torch path stays the portable default and the
  adapter is thin.
- **Scope creep risk.** `--lang` / `--repo-id` are strictly speaking a separate
  feature (custom voices) that happens to share the same call sites. Bundled only
  because touching those lines twice is worse; they can be dropped without affecting
  the MLX work.

## Open questions

1. **Default backend** — `torch` (safe, portable, explicit opt-in via `--backend mlx`)
   or `auto` (fastest here, but changes behaviour for any Apple-Silicon user)?
   Recommendation: `auto`, given this is a personal fork and the speedup is large;
   revisit if upstreaming.
2. **Is this going upstream?** If a PR to `santinic/audiblez` is intended, the MLX
   extra and default must be conservative, and the extraction commits should probably
   be proposed separately from the backend work.
3. **`qwen-tts` dependency** — already declared but unused. Was a Qwen backend
   started upstream? Affects whether step 7 drops it or builds on it.
4. **Quantized MLX variants** — 8/6/4-bit Kokoro repos exist. Benchmark those too, or
   is bf16 fast enough that lower precision is not worth the quality risk?
