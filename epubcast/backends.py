# -*- coding: utf-8 -*-
"""TTS backends.

Kokoro runs through torch (portable, works everywhere, now the default engine) or through
MLX (Apple Silicon only, measured ~3.8x faster on an M5 Max but historically linked to
kernel panics on some machines). Both produce the same voices from the same weights, so
the choice is purely about speed and platform.

The MLX pipeline is adapted to Kokoro's own call signature, so `gen_audio_segments` --
and everything downstream of it -- does not need to know which engine is running.
"""
import os
import platform
import re
import threading
import time
from glob import glob
from pathlib import Path

from epubcast.voices import is_edge_voice, lang_code_for

BACKENDS = ('auto', 'torch', 'mlx', 'edge')

DEFAULT_REPOS = {
    'torch': 'hexgrad/Kokoro-82M',
    'mlx': 'mlx-community/Kokoro-82M-bf16',
}

# The commit each repo above was validated against. A Hugging Face repo is mutable, so
# without this a repo update silently swaps the weights under unchanged code and the same
# book can come out sounding different -- the exact reproducibility hole a `repo_id` alone
# leaves open. Both engines honour it: mlx-audio through load_model's revision argument, and
# torch through PinnedTorchPipeline below, since kokoro's own loader takes none.
DEFAULT_REVISIONS = {
    'torch': 'f3ff3571791e39611d31c381e3a41a3af07b4987',
    'mlx': 'a71e4d38b236d968966a2002c4c895dbd12b1c3c',
}

# Starting guesses for the time estimate, in characters of text per second. These only seed
# the estimate: it is recalibrated from real throughput once synthesis is under way.
CHARS_PER_SEC_GUESS = {
    'mlx': 900,        # measured ~906 on an M5 Max
    'torch_cuda': 500,
    'torch_cpu': 238,  # measured on an M5 Max; upstream's 50 mis-reported the ETA by ~4x
    'edge': 150,       # network-bound; conservative, recalibrates within the first chapter
}

# Per-language overrides. Characters are not equal units of speech: a CJK character carries
# far more phonemes than a Latin one, so Kokoro measures ~150 chars/sec on Chinese against
# ~666 on English on the same machine. Without this the first ETA shown for a Chinese book
# is optimistic by roughly 4-6x, which is worst precisely when the run is longest.
CHARS_PER_SEC_BY_LANG = {
    'mlx': {'z': 150},   # measured on an M5 Max
}


def is_apple_silicon():
    return platform.system() == 'Darwin' and platform.machine() == 'arm64'


def mlx_available():
    """True when this machine can run the MLX backend."""
    if not is_apple_silicon():
        return False
    try:
        import mlx_audio  # noqa: F401
    except ImportError:
        return False
    return True


def torch_available():
    """True when the torch backend is installed.

    Kokoro on torch is the default engine, so its absence is unusual; the accelerated
    MLX backend is the optional extra in this fork.
    """
    try:
        import kokoro  # noqa: F401
    except ImportError:
        return False
    return True


def edge_available():
    """True when the edge-tts package is installed.

    Edge is an optional extra: it needs network at synthesis time, so its absence is
    normal rather than an error.
    """
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_backend(backend='auto'):
    """Turn 'auto' into a concrete backend name.

    'auto' now prefers the portable torch engine; MLX (Apple Silicon only) is used only
    when torch is not installed. MLX is faster, but on some machines it is tied to kernel
    panics, so torch is the safe default.
    """
    if backend not in BACKENDS:
        raise ValueError(f"Unknown backend {backend!r}. Choose one of: {', '.join(BACKENDS)}")
    if backend != 'auto':
        return backend
    if torch_available():
        return 'torch'
    if mlx_available():
        return 'mlx'
    # Nothing installed: name the default engine so the error tells the truth.
    return 'torch'


def initial_chars_per_sec(backend, lang_code=None):
    """Seed value for the time estimate, before real throughput is known."""
    resolved = resolve_backend(backend)
    if resolved == 'mlx':
        key = 'mlx'
    elif resolved == 'edge':
        key = 'edge'
    else:
        key = 'torch_cpu'
        try:
            import torch
            if torch.cuda.is_available():
                key = 'torch_cuda'
        except ImportError:
            pass
    by_lang = CHARS_PER_SEC_BY_LANG.get(key, {})
    return by_lang.get(str(lang_code or '')[:1], CHARS_PER_SEC_GUESS[key])


def find_espeak_data_path():
    """Locate espeak-ng's data directory.

    misaki's espeakng-loader ships a hardcoded path from its own build machine
    (/Users/runner/work/...), so on any other machine it fails with
    "Error processing file '.../phontab'". Pointing ESPEAK_DATA_PATH at the real
    install is the fix.
    """
    if os.environ.get('ESPEAK_DATA_PATH'):
        return os.environ['ESPEAK_DATA_PATH']
    candidates = []
    if platform.system() == 'Darwin':
        from subprocess import check_output, CalledProcessError
        try:
            cellar = Path(check_output(['brew', '--cellar'], text=True).strip())
            candidates += sorted(glob(str(cellar / 'espeak-ng' / '*' / 'share' / 'espeak-ng-data')))
        except (CalledProcessError, FileNotFoundError):
            pass
    elif platform.system() == 'Linux':
        candidates += ['/usr/share/espeak-ng-data', '/usr/lib/espeak-ng-data']
        candidates += sorted(glob('/usr/lib/*/espeak-ng-data'))
    elif platform.system() == 'Windows':
        candidates += sorted(glob('C:\\Program Files*\\eSpeak NG\\espeak-ng-data'))
    for candidate in candidates:
        if Path(candidate, 'phontab').exists():
            return candidate
    return None


def set_espeak_data_path():
    """Export ESPEAK_DATA_PATH if it is not already set. Returns the path used, if any."""
    if os.environ.get('ESPEAK_DATA_PATH'):
        return os.environ['ESPEAK_DATA_PATH']
    path = find_espeak_data_path()
    if path:
        os.environ['ESPEAK_DATA_PATH'] = path
        print('Using espeak data:', path)
    else:
        print('Could not locate espeak-ng data; MLX phonemization may fail. '
              'Set ESPEAK_DATA_PATH manually if so.')
    return path


class MlxKokoroPipeline:
    """mlx-audio's Kokoro, wearing Kokoro's KPipeline signature.

    Yields (graphemes, phonemes, audio) like KPipeline does. mlx-audio does not hand back
    grapheme/phoneme strings, so those are None -- `gen_audio_segments` discards them.
    Audio is converted to numpy so np.concatenate downstream is unaffected.
    """

    def __init__(self, lang_code, repo_id=None, revision=None):
        from mlx_audio.tts.utils import load_model
        set_espeak_data_path()
        self.lang_code = lang_code
        self.repo_id = repo_id or DEFAULT_REPOS['mlx']
        # The pinned commit names a commit in the *default* repo. A caller-supplied repo_id
        # is a different repo with its own history, so it stays on its default branch unless
        # the caller pins it explicitly -- passing our SHA there would not resolve.
        if revision is None and repo_id is None:
            revision = DEFAULT_REVISIONS['mlx']
        self.revision = revision
        self.model = load_model(self.repo_id, **({'revision': revision} if revision else {}))

    def __call__(self, text, voice, speed=1.0, split_pattern=r'\n\n\n'):
        import numpy as np
        for result in self.model.generate(text=text, voice=voice, speed=speed,
                                          lang_code=self.lang_code, split_pattern=split_pattern):
            yield None, None, np.asarray(result.audio)


class PinnedTorchPipeline:
    """Kokoro's KPipeline with every artifact resolved at a pinned commit.

    kokoro 0.9.4 fetches its weights through hf_hub_download(repo_id=...) with no revision
    argument, so an upstream repo update swaps them under unchanged code -- exactly the
    drift the MLX path does not have. The seams kokoro does expose are enough to close it:
    KModel takes explicit config/weights paths, KPipeline takes a ready KModel, and
    load_voice takes a tensor. So the config, the weights and the voice pack are each
    resolved here at DEFAULT_REVISIONS and still run through kokoro's own inference.

    Anything unexpected -- a missing file, no network, a kokoro upgrade that moves these
    seams -- falls back to the plain pipeline rather than failing the run. Unpinned weights
    are a far better outcome than no audiobook, and the fallback says so out loud.
    """

    def __init__(self, lang_code, repo_id=None, revision=None):
        self.lang_code = lang_code
        self.repo_id = repo_id or DEFAULT_REPOS['torch']
        # DEFAULT_REVISIONS['torch'] names a commit in the default repo; a caller-supplied
        # repo has its own history, so it is left on its branch unless pinned explicitly.
        if revision is None and repo_id is None:
            revision = DEFAULT_REVISIONS['torch']
        self.revision = revision
        self._voice_packs = {}
        self._pipeline = self._build(lang_code)

    def _build(self, lang_code):
        from kokoro import KPipeline
        if self.revision is None:
            return KPipeline(lang_code=lang_code, repo_id=self.repo_id)
        try:
            return self._build_pinned(lang_code)
        except Exception as error:
            print(f'Could not pin {self.repo_id} at {self.revision} '
                  f'({type(error).__name__}: {error}); using the repo default branch instead.')
            return KPipeline(lang_code=lang_code, repo_id=self.repo_id)

    def _build_pinned(self, lang_code):
        import torch
        from huggingface_hub import hf_hub_download
        from kokoro import KPipeline
        from kokoro.model import KModel

        weights_name = KModel.MODEL_NAMES.get(self.repo_id)
        if weights_name is None:
            raise ValueError(f'kokoro has no known weights filename for {self.repo_id}')
        config = hf_hub_download(self.repo_id, 'config.json', revision=self.revision)
        weights = hf_hub_download(self.repo_id, weights_name, revision=self.revision)
        # Same device choice KPipeline makes for itself, so pinning changes only the source
        # of the weights and not where the model runs.
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        model = KModel(repo_id=self.repo_id, config=config, model=weights).to(device).eval()
        return KPipeline(lang_code=lang_code, repo_id=self.repo_id, model=model)

    def _load_voice(self, name):
        """One voice pack, mirroring KPipeline.load_single_voice at the pinned commit."""
        import torch
        if name in self._voice_packs:
            return self._voice_packs[name]
        if name.endswith('.pt'):
            # A local custom pack, exactly as kokoro treats it.
            path = name
        else:
            from huggingface_hub import hf_hub_download
            path = hf_hub_download(self.repo_id, f'voices/{name}.pt', revision=self.revision)
        self._voice_packs[name] = torch.load(path, weights_only=True)
        return self._voice_packs[name]

    def _resolve_voice(self, voice):
        """A voice name, a comma blend, or a local .pt path -> the tensor KPipeline wants."""
        import torch
        if isinstance(voice, torch.Tensor):
            return voice
        packs = [self._load_voice(part) for part in voice.split(',')]
        if len(packs) == 1:
            return packs[0]
        # Blends are averaged, which is what kokoro's load_voice does for them.
        return torch.mean(torch.stack(packs), dim=0)

    def __call__(self, text, voice, speed=1.0, split_pattern=r'\n\n\n'):
        yield from self._pipeline(text, voice=self._resolve_voice(voice), speed=speed,
                                  split_pattern=split_pattern)


# Text with nothing to pronounce. Sentence splitting hands back bare newlines (the last
# "sentence" of a chapter is routinely just '\n') and punctuation-only fragments such as
# '「」', and edge-tts splits those into *zero* chunks: it then streams nothing and raises
# nothing, so an empty payload reached soundfile, which reported the unhelpful
# "Format not recognised" and killed the whole book. `\w` covers Han, kana and Latin alike.
_SPEAKABLE_RE = re.compile(r'\w', re.UNICODE)

# Edge is a network service, and a dropped response must not cost a multi-hour book.
# Measured: perfectly speakable text comes back with no audio when requests are issued back
# to back -- the service throttles -- and reads normally again after a pause. '②"。' failed
# three times running mid-book and synthesized fine (1.78s, peak 0.48) moments later, so
# the waits are sized to outlast throttling rather than a network round trip.
EDGE_ATTEMPTS = 4
EDGE_RETRY_WAITS = (3.0, 8.0, 20.0)


def _retry_wait(attempt):
    """Seconds to wait before the retry following `attempt`, holding at the last step."""
    return EDGE_RETRY_WAITS[min(attempt, len(EDGE_RETRY_WAITS)) - 1]


class EdgeNoAudio(RuntimeError):
    """Edge accepted the text and returned no audio, repeatedly.

    Its own class so core.main can fail the one chapter rather than the whole book: a
    fragment the service truly has nothing to say for would otherwise wedge every
    re-run at the same sentence.
    """


def _is_no_audio(exc):
    """True when the service reported no speech, as opposed to failing to answer at all.

    edge-tts raises NoAudioReceived both for text it finds nothing to say in and, as
    observed, when it is simply throttling. Neither is a reason to abandon the book, so
    this is treated as an empty payload rather than as a fatal error.
    """
    try:
        from edge_tts.exceptions import NoAudioReceived
    except ImportError:
        return False
    return isinstance(exc, NoAudioReceived)


class _AdaptiveConcurrencyGate:
    """Bounds how many EdgePipeline calls actually hit the network at once, adaptively.

    Edge throttles -- returns empty audio -- when requests are issued back to back (see
    EDGE_ATTEMPTS/EDGE_RETRY_WAITS above), so this is not a fixed-size pool: it starts at
    1 (fully serial), and only a run of `ramp_streak` clean, first-attempt successes earns
    it one more slot, up to `ceiling`. Any call that needed a retry -- whether from a
    transport error or a throttled empty response, the two are not distinguished here --
    drops it straight back to 1. gen_audio_segments can submit more calls than the current
    limit to a thread pool; the extras simply block in acquire() until a slot opens, so the
    gate is the single source of truth for how much concurrency is actually happening.
    """

    def __init__(self, ceiling, ramp_streak=5):
        self._ceiling = ceiling
        self._ramp_streak = ramp_streak
        self._limit = 1
        self._in_flight = 0
        self._streak = 0
        self._cond = threading.Condition()

    def acquire(self):
        with self._cond:
            while self._in_flight >= self._limit:
                self._cond.wait()
            self._in_flight += 1

    def release(self, needed_retry):
        with self._cond:
            self._in_flight -= 1
            if needed_retry:
                self._limit = 1
                self._streak = 0
            else:
                self._streak += 1
                if self._streak >= self._ramp_streak and self._limit < self._ceiling:
                    self._limit += 1
                    self._streak = 0
            self._cond.notify_all()


class EdgePipeline:
    """Microsoft Edge's online TTS, wearing the pipeline callable signature.

    Yields (graphemes, phonemes, audio) like the Kokoro pipelines do; graphemes and
    phonemes are None. Audio is decoded from the MP3 edge-tts returns (24kHz, the same
    rate epubcast writes) to numpy so np.concatenate downstream is unaffected.

    Each __call__ is one network request, run with asyncio.run() in whichever thread calls
    it -- epubcast's core is synchronous, so there is no running event loop in any given
    thread for asyncio.run() to clash with, and a fresh event loop per thread is fine.
    split_pattern is accepted and ignored: edge-tts needs no sentence-level split pattern.
    """

    # gen_audio_segments groups consecutive sentences into chunks up to this many
    # characters per call, to amortize Edge's per-call network round trip. It reads this
    # attribute rather than special-casing EdgePipeline by isinstance, so any other backend
    # that wants the same amortization can opt in the same way.
    batch_chars = 1500

    # gen_audio_segments may dispatch up to this many chunks to a thread pool at once; the
    # instance's own _AdaptiveConcurrencyGate decides how many of them are actually allowed
    # to call the network concurrently at any given moment (starting at 1, see above).
    max_concurrency = 3

    def __init__(self, lang_code):
        self.lang_code = lang_code
        self._gate = _AdaptiveConcurrencyGate(self.max_concurrency)

    def __call__(self, text, voice, speed=1.0, split_pattern=None):
        import asyncio
        import io
        import soundfile
        import edge_tts

        # Nothing to narrate: yield no segment rather than ask the service to read silence.
        if not _SPEAKABLE_RE.search(text or ''):
            return

        rate = f"{int(round((speed - 1) * 100)):+d}%"

        async def synth():
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            chunks = []
            async for chunk in communicate.stream():
                if chunk['type'] == 'audio':
                    chunks.append(chunk['data'])
            return b''.join(chunks)

        excerpt = text.strip()[:40]
        mp3 = b''
        needed_retry = False
        self._gate.acquire()
        try:
            for attempt in range(1, EDGE_ATTEMPTS + 1):
                if attempt > 1:
                    needed_retry = True
                try:
                    mp3 = asyncio.run(synth())
                except Exception as e:
                    if not _is_no_audio(e):
                        # Could not reach the service at all: that breaks the run, not just
                        # this fragment. A chapter's .wav is only written once the chapter
                        # finishes, and finished chapters are skipped on the next run, so
                        # raising costs this chapter and not the book.
                        if attempt == EDGE_ATTEMPTS:
                            raise RuntimeError(
                                f'Edge TTS failed after {EDGE_ATTEMPTS} attempts on {excerpt!r}: '
                                f'{type(e).__name__}: {e}') from e
                        print(f'Edge TTS attempt {attempt} failed ({type(e).__name__}); retrying...')
                        time.sleep(_retry_wait(attempt))
                        continue
                    mp3 = b''   # answered, but with no speech: identical to an empty payload
                if mp3:
                    break
                if attempt < EDGE_ATTEMPTS:
                    print(f'Edge TTS returned no audio (attempt {attempt}); retrying...')
                    time.sleep(_retry_wait(attempt))
        finally:
            # Runs even when the loop above raised: a call that failed outright is at least
            # as strong a throttling signal as one that merely needed a retry, so it must
            # still drop the gate back to 1 rather than leaving a stale higher limit in
            # place (and, either way, in_flight must be released for waiters to proceed).
            self._gate.release(needed_retry)

        if not mp3:
            # The service took the text and sent nothing back, repeatedly. Yielding
            # nothing would be silent and permanent: gen_audio_segments would return a
            # short list, the chapter's .wav would be written from it, and the next run
            # skips chapters whose .wav already exists -- so the missing speech could
            # never be recovered. Raise instead; core.main drops this chapter without
            # writing it, so a re-run redoes it.
            raise EdgeNoAudio(
                f'Edge TTS returned no audio for {excerpt!r} after {EDGE_ATTEMPTS} '
                'attempts.')

        audio, _ = soundfile.read(io.BytesIO(mp3), dtype='float32')
        yield None, None, audio


def get_pipeline(voice, lang_code=None, backend='auto', repo_id=None, revision=None):
    """Build a TTS pipeline callable for `voice`.

    lang_code defaults to the first letter of the voice name, which is Kokoro's own
    convention ('af_sky' -> 'a'); an Edge voice carries its locale instead
    ('zh-TW-HsiaoChenNeural' -> 'zh-TW'). Pass it explicitly for a voice whose name does
    not carry the language, such as a path to a custom .pt voice pack.

    repo_id/revision override which Kokoro weights are loaded. Left unset, both engines pin
    DEFAULT_REVISIONS for their repo, so an upstream update cannot silently change the audio.
    """
    resolved = resolve_backend(backend)
    lang_code = lang_code_for(voice, lang_code)
    if resolved != 'edge' and is_edge_voice(voice):
        # Otherwise this fails late and obscurely: mlx only after load_model has pulled
        # a 339 MB repo, torch on an assertion inside KPipeline about a 'zh-TW' lang_code.
        raise RuntimeError(
            f'{voice!r} is an Edge TTS voice, but the {resolved!r} backend was selected. '
            'Add --backend edge to use it, or pick a Kokoro voice such as af_heart.')
    if resolved == 'mlx':
        if not mlx_available():
            hint = ('Install it with: pip install ".[mlx]"' if is_apple_silicon()
                    else 'It needs Apple Silicon; use the default torch backend on this machine.')
            raise RuntimeError(f'The mlx backend is not available. {hint}')
        return MlxKokoroPipeline(lang_code, repo_id, revision)
    if resolved == 'edge':
        if not edge_available():
            raise RuntimeError(
                'The edge backend is not installed. Add it with: pip install ".[edge]"')
        if not is_edge_voice(voice):
            raise RuntimeError(
                f'{voice!r} is not an Edge TTS voice. Edge voices look like '
                "'zh-TW-HsiaoChenNeural'; pick one from the voice list or type a full name.")
        return EdgePipeline(lang_code)
    if not torch_available():
        raise RuntimeError(
            'The torch backend is not installed. Install the default engine with: '
            'pip install .   (or pip install -r requirements.txt)')
    return PinnedTorchPipeline(lang_code, repo_id, revision)
