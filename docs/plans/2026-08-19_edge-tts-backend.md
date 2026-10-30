# Edge TTS backend: Microsoft's neural voices, alongside Kokoro

Date: 2026-08-19
Branch: `feat/traditional-chinese`
Status: **implemented and shipped**, including the traditional-Chinese/Cantonese scope and
the GUI language multi-selector described below. Its own per-sentence request pattern was
later found to be network-round-trip-bound on long books and batched into fewer, larger
requests — see `perf(edge): batch sentences into fewer Edge requests` and the CHANGELOG.

## Goal & scope

Add an `edge` backend to epubcast that narrates through Microsoft Edge's online TTS
service (the `edge-tts` package, already used by the sibling `bookreader` project), beside
the existing `mlx`/`torch` Kokoro backends. This is the replacement for the aborted
CosyVoice2 plan (`2026-08-19_cosyvoice2-crosslingual-chinese.md`): it solves the same
Chinese-quality ceiling with no model downloads, and adds two things Kokoro cannot do at
all:

- **Real Cantonese** (`zh-HK` voices). The README's stated limitation — "Kokoro ships no
  Cantonese voice, so a Hong Kong text will be read… in Mandarin" — disappears.
- **Native traditional-script reading** (`zh-TW` voices). A Taiwan voice reads 臺灣 and 著
  correctly by itself, so the OpenCC `tw2s` conversion is skipped for these voices rather
  than applied.

Also in scope: a **language multi-selector in the GUI** that narrows the voice dropdown to
the selected languages, so the dropdown does not grow to the full edge catalog (~400
voices) plus the existing ~60 Kokoro voices.

### Verified facts this plan rests on

- `soundfile` 1.2.2 (already a dependency) decodes MP3 in-memory at 24 kHz — the exact
  format edge-tts outputs. No ffmpeg subprocess needed per sentence.
- `edge_tts.Communicate(text, voice, rate=..., volume=..., pitch=..., boundary=...)` accepts
  a `rate` string (`"+50%"`, `"-20%"`), which is how epubcast's `speed` (0.5–2.0) maps over.
- The live catalog has 3 `zh-HK` (Cantonese), 3 `zh-TW` (Taiwanese Mandarin), 6 `zh-CN`
  (Mandarin) voices, plus the usual en/es/fr/de/it/ja/pt/hi sets.

### Out of scope

- **`auto` never picks edge.** Edge needs network and is a different kind of engine; it is
  chosen explicitly with `--backend edge` / the GUI dropdown. `resolve_backend('auto')`
  behaviour is unchanged.
- Word-boundary timing / karaoke-style highlighting. epubcast produces whole-chapter audio;
  it has no use for edge-tts's `WordBoundary` events (bookreader does, epubcast does not).
- A full ~400-voice catalog. The dropdown is curated to the languages Kokoro already
  supports plus the Chinese variants; the rest is reachable by typing a voice name (the
  dropdown stays editable).
- Offline mode, caching, or batching. Each sentence is one request, matching the existing
  per-sentence progress/ETA accounting.
- Changing the `('a', 'b')` sentence-split branch in `gen_audio_segments`. Edge English
  voices will take the long-sentence split path; it is harmless (edge-tts handles 400-char
  sentences fine) and noted as a follow-up simplification, not folded in here.

## Affected files/modules

| File | Why |
|---|---|
| `pyproject.toml` | Add an `[edge]` optional extra (`edge-tts`), matching the `[torch]` extra pattern |
| `epubcast/voices.py` | `edge_voices` dict keyed by locale, edge flags, `is_edge_voice()`, `lang_code_for()` |
| `epubcast/backends.py` | `BACKENDS` + `edge`, `edge_available()`, `EdgePipeline`, edge ETA seed |
| `epubcast/chinese.py` | `wants_simplification`: `zh-CN` converts, `zh-TW`/`zh-HK` do not |
| `epubcast/core.py` | Replace the three `voice[0]` lang-code derivations with `lang_code_for()` |
| `epubcast/cli.py` | `--backend` choices add `edge`; help text mentions edge locales |
| `epubcast/ui.py` | Backend dropdown adds `edge`; language multi-selector; voice dropdown filtering |
| `test/test_backends.py` | `EdgePipeline` adapter tests with a fake `edge_tts` module |
| `test/test_chinese.py` | `zh-CN`/`zh-TW`/`zh-HK` conversion decisions |
| `test/test_cli.py` | `--help` lists the edge backend and an edge voice |
| `README.md` | Document the backend, its network requirement, and the voice list |

## Steps

### 1. Dependency, on its own

Add to `pyproject.toml`:

```toml
edge = [
    "edge-tts (>=7.2.0)",
]
```

Install into the project venv and confirm `import edge_tts` works. This step is
independently reversible and does not touch code.

### 2. Voice naming: `voices.py`

Add, beside the existing `voices`/`flags` dicts:

- `edge_voices`: locale → curated voice list (see below).
- `edge_flags`: locale → emoji (🇺🇸 🇬🇧 🇨🇳 🇹🇼 🇭🇰 🇪🇸 🇫🇷 🇩🇪 🇮🇹 🇯🇵 🇧🇷 🇮🇳).
- `is_edge_voice(voice)`: `True` when the name matches `^[a-z]{2}-[A-Z]{2}-` (e.g.
  `zh-TW-HsiaoChenNeural`). Pattern-based so a typed-in custom edge voice is recognised
  without being in the curated list; Kokoro names (`af_sky`, `zf_xiaobei`), blends
  (`af_heart,af_bella`) and `.pt` paths never match.
- `lang_code_for(voice, lang_code=None)`: explicit `lang_code` wins; an edge voice returns
  its locale (`zh-TW`); otherwise `voice[0]` (Kokoro's convention). This is the single
  place the three current `voice[0]` derivations collapse into.

Curated `edge_voices` (Chinese enumerated in full — they are the point; the rest trimmed
to the common voices):

```python
edge_voices = {
    'en-US': ['en-US-AriaNeural', 'en-US-JennyNeural', 'en-US-GuyNeural',
              'en-US-EmmaNeural', 'en-US-BrianNeural', 'en-US-AndrewNeural',
              'en-US-ChristopherNeural', 'en-US-MichelleNeural'],
    'en-GB': ['en-GB-LibbyNeural', 'en-GB-MaisieNeural', 'en-GB-RyanNeural',
              'en-GB-SoniaNeural', 'en-GB-ThomasNeural'],
    'en-AU': ['en-AU-NatashaNeural', 'en-AU-WilliamMultilingualNeural'],
    'en-CA': ['en-CA-ClaraNeural', 'en-CA-LiamNeural'],
    'en-IN': ['en-IN-NeerjaNeural', 'en-IN-PrabhatNeural'],
    'zh-CN': ['zh-CN-XiaoxiaoNeural', 'zh-CN-XiaoyiNeural', 'zh-CN-YunjianNeural',
              'zh-CN-YunxiNeural', 'zh-CN-YunxiaNeural', 'zh-CN-YunyangNeural'],
    'zh-TW': ['zh-TW-HsiaoChenNeural', 'zh-TW-HsiaoYuNeural', 'zh-TW-YunJheNeural'],
    'zh-HK': ['zh-HK-HiuGaaiNeural', 'zh-HK-HiuMaanNeural', 'zh-HK-WanLungNeural'],
    'es-ES': ['es-ES-AlvaroNeural', 'es-ES-ElviraNeural', 'es-ES-XimenaNeural'],
    'es-MX': ['es-MX-DaliaNeural', 'es-MX-JorgeNeural'],
    'fr-FR': ['fr-FR-DeniseNeural', 'fr-FR-EloiseNeural', 'fr-FR-HenriNeural',
              'fr-FR-RemyMultilingualNeural', 'fr-FR-VivienneMultilingualNeural'],
    'de-DE': ['de-DE-ConradNeural', 'de-DE-KatjaNeural', 'de-DE-FlorianMultilingualNeural',
              'de-DE-SeraphinaMultilingualNeural'],
    'it-IT': ['it-IT-DiegoNeural', 'it-IT-ElsaNeural', 'it-IT-IsabellaNeural'],
    'ja-JP': ['ja-JP-KeitaNeural', 'ja-JP-NanamiNeural'],
    'pt-BR': ['pt-BR-AntonioNeural', 'pt-BR-FranciscaNeural', 'pt-BR-ThalitaMultilingualNeural'],
    'hi-IN': ['hi-IN-MadhurNeural', 'hi-IN-SwaraNeural'],
}
```

`available_voices_str` gains an edge section so the CLI epilog stays readable.

### 3. Backend: `backends.py`

- `BACKENDS = ('auto', 'torch', 'mlx', 'edge')`; `resolve_backend('edge')` passes through,
  `resolve_backend('auto')` unchanged.
- `edge_available()`: `import edge_tts` succeeds.
- `EdgePipeline(lang_code)`, signature-compatible with the pipeline interface
  (`__call__(text, voice, speed, split_pattern)` yields `(None, None, np.ndarray)` at
  24 kHz):

```python
class EdgePipeline:
    def __init__(self, lang_code):
        self.lang_code = lang_code

    def __call__(self, text, voice, speed=1.0, split_pattern=None):
        import asyncio, io
        import edge_tts, soundfile, numpy as np
        rate = f"{int(round((speed - 1) * 100)):+d}%"
        async def synth():
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            chunks = []
            async for chunk in communicate.stream():
                if chunk['type'] == 'audio':
                    chunks.append(chunk['data'])
            return b''.join(chunks)
        mp3 = asyncio.run(synth())
        audio, _ = soundfile.read(io.BytesIO(mp3), dtype='float32')
        yield None, None, audio
```

  `split_pattern` is accepted and ignored — edge-tts needs no sentence-level split pattern.
  `asyncio.run()` per call is deliberate: one request per sentence, no shared loop to
  thread-safety-manage.
- `get_pipeline`: `resolved == 'edge'` → require `edge_available()` (else a
  `pip install ".[edge]"` error), reject a non-edge voice at construction
  (`is_edge_voice(voice)`), return `EdgePipeline(lang_code_for(voice, lang_code))`.
- `initial_chars_per_sec`: `resolve_backend(backend) == 'edge'` → `CHARS_PER_SEC_GUESS['edge']`.
  Seed conservatively (network-bound; ~150 chars/sec) — `measured_chars_per_sec`
  recalibrates from real throughput within the first chapter.

### 4. Skip-conversion: `chinese.py`

`wants_simplification` gains the edge locales:

- `'z'` (Kokoro Mandarin) → convert (unchanged).
- `'zh-CN'` (edge Mandarin) → convert — same simplified-keyed front end, same benefit.
- `'zh-TW'`, `'zh-HK'` → **do not convert** — these voices read traditional script
  natively; converting would be pointless and the OpenCC dependency is not needed for them.
- unknown/empty → `looks_chinese(text)` (unchanged); everything else → `False`.

`normalize` needs no change — it delegates to `wants_simplification`.

### 5. Core: `core.py`

Replace the three `voice[0]` derivations with `lang_code_for`:

- `core.main`: `lang_code = lang_code_for(voice, lang_code)` (feeds `initial_chars_per_sec`
  and `get_pipeline`).
- `gen_audio_segments`: same, so `chinese.normalize` sees the right code.
- `get_pipeline` (in backends.py) already uses it per step 3.

### 6. CLI: `cli.py`

- `-b/--backend` choices: `['auto', 'torch', 'mlx', 'edge']`.
- `--lang` help: mention edge locales (`zh-TW`, `zh-HK`, …) alongside Kokoro codes.
- Epilog: the edge voice section from `available_voices_str`.

### 7. GUI: `ui.py`

- Backend dropdown: `['auto'] + (['mlx'] if mlx_available() else []) +
  (['torch'] if torch_available() else []) + (['edge'] if edge_available() else [])`.
- **Language multi-selector**: a `wx.CheckListBox` row ("Languages:") listing the languages
  for the current backend — Kokoro codes with flags for mlx/torch, edge locales for edge.
  Default: all checked, so the voice dropdown shows everything as today. Unchecking a
  language rebuilds the voice dropdown to only that language's voices. This is the
  "model-dependent rows" pattern the removed Qwen code used (`update_speed_row` /
  `update_model_dependent_rows` in `691940f`).
- Voice dropdown: choices rebuilt from the checked languages of the current backend; stays
  editable (blends, `.pt` paths, typed edge voice names).
- `on_preview_chapter`: `lang_code = lang_code_for(self.get_selected_voice())` instead of
  `voice[0]`.
- `on_start`: pass `backend` through unchanged (already does).

### 8. Tests

See Test strategy below.

### 9. README

Document the edge backend: what it is, the network requirement, the privacy note (book
text is sent to Microsoft's servers), the `pip install ".[edge]"` step, the voice list, and
the `zh-TW`/`zh-HK` skip-conversion behaviour.

## Test strategy

Unit tests, no network, following the existing fake-module pattern in `test_backends.py`
(`FakeMlxModel`, `fake_mlx_audio`):

- **`EdgePipeline`** (fake `edge_tts` module whose `Communicate` records `text`/`voice`/
  `rate` and streams a real tiny MP3 written by `soundfile`):
  - yields `(None, None, np.ndarray)` triples that survive `np.concatenate`;
  - maps speed → rate: `1.0 → "+0%"`, `1.5 → "+50%"`, `0.5 → "-50%"`;
  - passes `voice` and `rate` through to `Communicate`;
  - a missing `edge_tts` raises the `.[edge]` install hint.
- **`resolve_backend`**: `'edge'` passes through; `'auto'` still never returns `'edge'`.
- **`edge_available`**: mocked import present/absent.
- **`lang_code_for`**: edge voice → locale; Kokoro voice → first letter; explicit wins;
  `.pt` path with explicit `--lang` unchanged.
- **`get_pipeline`**: `backend='edge'` with a Kokoro voice is rejected at construction.
- **`chinese.wants_simplification`**: `'zh-CN'` → True; `'zh-TW'`/`'zh-HK'` → False;
  `normalize(TRADITIONAL, 'zh-TW')` returns the text unchanged.
- **`test_cli`**: `--help` lists `edge` and an edge voice.

Manual, since no automated check covers it: synthesize one chapter with `zh-TW-HsiaoChenNeural`
and one with `zh-HK-HiuMaanNeural` and listen — the whole point is that traditional script
and Cantonese now sound right.

## Risks & trade-offs

- **Network required.** edge-tts is Microsoft's online service; a book's text is sent to
  their servers. This is the biggest trade-off and must be stated in the README and the
  GUI. Offline books still use Kokoro.
- **External service, not a pinned model.** Microsoft could change or rate-limit the
  service. The `edge-tts` package is actively maintained (7.2.x) and the sibling project
  already depends on it, but this is a live dependency on a third party.
- **Speed is network-bound.** Not 900 chars/sec like MLX Kokoro, but streams roughly
  real-time or faster — far ahead of the aborted CosyVoice2's measured 20–33 chars/sec.
  The ETA seed is conservative and recalibrates.
- **Dependency as an extra, not required.** `[edge]` matches the `[torch]` pattern and keeps
  the base install lean. Alternative: make it a required dependency (it is small, ~a few MB
  with aiohttp) — rejected to preserve the "MLX is the only required backend" contract.
- **Curated voice list is a hand-maintained subset.** The full catalog is ~400 voices; the
  dropdown lists ~50. Voices outside the list are still usable by typing them. The list
  will drift as Microsoft adds voices; that is acceptable for a dropdown.
- **`('a', 'b')` sentence-split branch.** Edge English voices take the long-sentence split
  path in `gen_audio_segments`. Harmless; noted as a follow-up to gate on the pipeline
  type instead of the lang code.

## Open questions

1. **Voice curation.** The list above is a proposal. Trim or extend it? The Chinese voices
   are the point; the rest is convenience.
2. **Language selector widget.** `wx.CheckListBox` (checkboxes, all visible) is proposed.
   A `wx.ListBox` with extended selection (ctrl+click) is the alternative if the language
   list grows past ~10 entries.
3. **Should `--lang` accept edge locales in the CLI?** Yes per this plan (`lang_code_for`
   handles it). Confirm the help text wording.
4. **Default language selection in the GUI.** All checked (current behaviour) is proposed.
   An alternative is to default to the book's detected language — out of scope, noted only
   because the selector makes it easy later.
