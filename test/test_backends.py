import sys
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from epubcast import backends
from epubcast.backends import get_pipeline, mlx_available, resolve_backend
from epubcast.core import safe_filename_part
from epubcast.voices import (DEFAULT_LANGUAGES, DEFAULT_LOCALES, default_languages,
                             edge_voices, is_catalog_voice, is_default_language,
                             is_edge_voice, lang_code_for, voices)


class FakeResult:
    def __init__(self, audio):
        self.audio = audio


class FakeMlxModel:
    """Stands in for mlx-audio's Kokoro model, recording how it was called."""

    def __init__(self):
        self.calls = []

    def generate(self, text, voice, speed, lang_code, split_pattern=None):
        self.calls.append(dict(text=text, voice=voice, speed=speed,
                               lang_code=lang_code, split_pattern=split_pattern))
        yield FakeResult(np.zeros(120, dtype=np.float32))
        yield FakeResult(np.ones(80, dtype=np.float32))


def fake_mlx_audio(model, loaded=None):
    """Build a stub mlx_audio package tree whose load_model returns `model`.

    `loaded`, when given, collects the arguments load_model was called with, so tests can
    assert on the pinned revision without downloading anything.
    """
    pkg = types.ModuleType('mlx_audio')
    tts = types.ModuleType('mlx_audio.tts')
    utils = types.ModuleType('mlx_audio.tts.utils')

    def load_model(repo_id, **kwargs):
        if loaded is not None:
            loaded.append(dict(repo_id=repo_id, **kwargs))
        return model

    utils.load_model = load_model
    pkg.tts = tts
    tts.utils = utils
    return {'mlx_audio': pkg, 'mlx_audio.tts': tts, 'mlx_audio.tts.utils': utils}


class ResolveBackendTest(unittest.TestCase):
    def test_explicit_backends_pass_through(self):
        self.assertEqual(resolve_backend('torch'), 'torch')
        self.assertEqual(resolve_backend('mlx'), 'mlx')
        self.assertEqual(resolve_backend('edge'), 'edge')

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_backend('festival')
        self.assertIn('festival', str(ctx.exception))

    def test_auto_prefers_torch_when_both_are_available(self):
        with mock.patch.object(backends, 'mlx_available', return_value=True), \
             mock.patch.object(backends, 'torch_available', return_value=True):
            self.assertEqual(resolve_backend('auto'), 'torch')

    def test_auto_falls_back_to_mlx_only_when_torch_is_missing(self):
        with mock.patch.object(backends, 'mlx_available', return_value=True), \
             mock.patch.object(backends, 'torch_available', return_value=False):
            self.assertEqual(resolve_backend('auto'), 'mlx')

    def test_mlx_unavailable_off_apple_silicon(self):
        with mock.patch('platform.system', return_value='Linux'):
            self.assertFalse(mlx_available())
        with mock.patch('platform.system', return_value='Darwin'), \
             mock.patch('platform.machine', return_value='x86_64'):
            self.assertFalse(mlx_available())


class AutoBackendFallbackTest(unittest.TestCase):
    def test_auto_uses_torch_when_only_torch_is_installed(self):
        with mock.patch.object(backends, 'mlx_available', return_value=False), \
             mock.patch.object(backends, 'torch_available', return_value=True):
            self.assertEqual(resolve_backend('auto'), 'torch')

    def test_auto_names_the_default_torch_engine_when_nothing_is_installed(self):
        # With neither engine installed, 'auto' reports the default engine so the error
        # points the user at what they should install, regardless of machine.
        with mock.patch.object(backends, 'mlx_available', return_value=False), \
             mock.patch.object(backends, 'torch_available', return_value=False):
            self.assertEqual(resolve_backend('auto'), 'torch')

    def test_auto_names_torch_elsewhere_when_nothing_is_installed(self):
        with mock.patch.object(backends, 'mlx_available', return_value=False), \
             mock.patch.object(backends, 'torch_available', return_value=False), \
             mock.patch.object(backends, 'is_apple_silicon', return_value=False):
            self.assertEqual(resolve_backend('auto'), 'torch')

    def test_torch_backend_without_kokoro_explains_the_default_install(self):
        with mock.patch.object(backends, 'torch_available', return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                get_pipeline('af_sky', backend='torch')
        self.assertIn('pip install .', str(ctx.exception))

    def test_mlx_requested_off_apple_silicon_says_so(self):
        with mock.patch.object(backends, 'mlx_available', return_value=False), \
             mock.patch.object(backends, 'is_apple_silicon', return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                get_pipeline('af_sky', backend='mlx')
        self.assertIn('Apple Silicon', str(ctx.exception))


class InitialChoresPerSecTest(unittest.TestCase):
    def test_mlx_seed_is_the_measured_figure(self):
        with mock.patch.object(backends, 'resolve_backend', return_value='mlx'):
            self.assertEqual(backends.initial_chars_per_sec('auto'),
                             backends.CHARS_PER_SEC_GUESS['mlx'])

    def test_torch_without_cuda_uses_the_cpu_seed(self):
        with mock.patch.object(backends, 'resolve_backend', return_value='torch'), \
             mock.patch.dict(sys.modules, {'torch': None}):
            self.assertEqual(backends.initial_chars_per_sec('torch'),
                             backends.CHARS_PER_SEC_GUESS['torch_cpu'])

    def test_chinese_gets_its_own_seed(self):
        # A CJK character is worth far more speech than a Latin one, so the flat seed is
        # ~6x optimistic for Chinese and the first ETA is badly wrong without this.
        with mock.patch.object(backends, 'resolve_backend', return_value='mlx'):
            self.assertEqual(backends.initial_chars_per_sec('mlx', 'z'), 150)
            self.assertEqual(backends.initial_chars_per_sec('mlx', 'zf_xiaoxiao'), 150)

    def test_other_languages_keep_the_default_seed(self):
        with mock.patch.object(backends, 'resolve_backend', return_value='mlx'):
            for lang in ('a', 'b', 'f', None):
                self.assertEqual(backends.initial_chars_per_sec('mlx', lang),
                                 backends.CHARS_PER_SEC_GUESS['mlx'])


class MlxAdapterTest(unittest.TestCase):
    def build(self, lang_code='a', repo_id=None, revision=None):
        model = FakeMlxModel()
        loaded = []
        with mock.patch.dict(sys.modules, fake_mlx_audio(model, loaded)), \
             mock.patch.object(backends, 'set_espeak_data_path', return_value='/data'):
            pipeline = backends.MlxKokoroPipeline(lang_code, repo_id, revision)
        return pipeline, model, loaded

    def test_yields_kokoro_shaped_triples_with_numpy_audio(self):
        pipeline, _, _ = self.build()
        out = list(pipeline('Hello.', voice='af_sky', speed=1.0))
        self.assertEqual(len(out), 2)
        for graphemes, phonemes, audio in out:
            self.assertIsNone(graphemes)
            self.assertIsNone(phonemes)
            self.assertIsInstance(audio, np.ndarray)
        # gen_audio_segments concatenates these, so they must survive np.concatenate
        self.assertEqual(len(np.concatenate([a for _, _, a in out])), 200)

    def test_passes_voice_speed_and_lang_through(self):
        pipeline, model, _ = self.build(lang_code='z')
        list(pipeline('你好。', voice='zf_xiaobei', speed=1.5, split_pattern=r'\n\n\n'))
        self.assertEqual(model.calls, [dict(text='你好。', voice='zf_xiaobei', speed=1.5,
                                            lang_code='z', split_pattern=r'\n\n\n')])

    def test_defaults_to_the_mlx_repo(self):
        pipeline, _, _ = self.build()
        self.assertEqual(pipeline.repo_id, backends.DEFAULT_REPOS['mlx'])

    def test_explicit_repo_id_wins(self):
        pipeline, _, _ = self.build(repo_id='mlx-community/Kokoro-82M-4bit')
        self.assertEqual(pipeline.repo_id, 'mlx-community/Kokoro-82M-4bit')

    def test_default_repo_is_loaded_at_the_pinned_revision(self):
        # A mutable HF repo means identical code can synthesise differently after an
        # upstream update; the default load must therefore pin the validated commit.
        pipeline, _, loaded = self.build()
        self.assertEqual(pipeline.revision, backends.DEFAULT_REVISIONS['mlx'])
        self.assertEqual(loaded[0]['revision'], backends.DEFAULT_REVISIONS['mlx'])

    def test_explicit_revision_wins(self):
        pinned = 'b' * 40
        pipeline, _, loaded = self.build(revision=pinned)
        self.assertEqual(pipeline.revision, pinned)
        self.assertEqual(loaded[0]['revision'], pinned)

    def test_a_custom_repo_id_is_not_pinned_to_the_default_repo_commit(self):
        # DEFAULT_REVISIONS['mlx'] is a commit in the default repo; sending it to a
        # different repo would fail to resolve, so a custom repo floats on its branch
        # unless the caller pins it.
        pipeline, _, loaded = self.build(repo_id='mlx-community/Kokoro-82M-4bit')
        self.assertIsNone(pipeline.revision)
        self.assertNotIn('revision', loaded[0])


class PinnedRevisionsTest(unittest.TestCase):
    """The pin recorded for a backend must describe the repo that backend loads."""

    def test_every_repo_has_a_recorded_revision(self):
        self.assertEqual(set(backends.DEFAULT_REVISIONS), set(backends.DEFAULT_REPOS))

    def test_revisions_are_full_commit_hashes(self):
        for backend, revision in backends.DEFAULT_REVISIONS.items():
            with self.subTest(backend=backend):
                self.assertRegex(revision, r'^[0-9a-f]{40}$')


class GetPipelineTest(unittest.TestCase):
    def test_mlx_requested_but_unavailable_raises_actionable_error(self):
        with mock.patch.object(backends, 'mlx_available', return_value=False), \
             mock.patch.object(backends, 'is_apple_silicon', return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_pipeline('af_sky', backend='mlx')
        self.assertIn('.[mlx]', str(ctx.exception))

    def test_lang_code_defaults_to_first_letter_of_voice(self):
        with mock.patch.object(backends, 'mlx_available', return_value=True):
            model = FakeMlxModel()
            with mock.patch.dict(sys.modules, fake_mlx_audio(model)), \
                 mock.patch.object(backends, 'set_espeak_data_path', return_value='/data'):
                pipeline = get_pipeline('zf_xiaobei', backend='mlx')
        self.assertEqual(pipeline.lang_code, 'z')

    def test_explicit_lang_code_overrides_voice_prefix(self):
        # A custom .pt voice has no language prefix: 'voice[0]' would be '/'.
        with mock.patch.object(backends, 'mlx_available', return_value=True):
            model = FakeMlxModel()
            with mock.patch.dict(sys.modules, fake_mlx_audio(model)), \
                 mock.patch.object(backends, 'set_espeak_data_path', return_value='/data'):
                pipeline = get_pipeline('/voices/custom.pt', lang_code='b', backend='mlx')
        self.assertEqual(pipeline.lang_code, 'b')


class FakeEdgeTTS:
    """Stands in for the edge_tts package, recording how Communicate was called.

    stream() yields one audio chunk holding `mp3_bytes` -- a real MP3, so the pipeline's
    soundfile decode is exercised rather than mocked away.
    """

    def __init__(self, mp3_bytes):
        self.mp3_bytes = mp3_bytes
        self.calls = []

    def Communicate(self, text, voice, rate=None, **kwargs):
        self.calls.append(dict(text=text, voice=voice, rate=rate, **kwargs))
        return self

    async def stream(self):
        yield {'type': 'audio', 'data': self.mp3_bytes}


class FakeNoAudioReceived(Exception):
    """Stands in for edge_tts.exceptions.NoAudioReceived.

    Defined here rather than imported from edge_tts: it is an optional extra that the CI
    jobs do not install, so importing the real class made these tests error there.
    _is_no_audio resolves the class through sys.modules, so the stand-in is what it finds.
    """


def fake_edge_tts_module(fake):
    pkg = types.ModuleType('edge_tts')
    pkg.Communicate = fake.Communicate
    # _is_no_audio does `from edge_tts.exceptions import NoAudioReceived`, so the submodule
    # has to be registered too, not just hung off the package.
    exceptions = types.ModuleType('edge_tts.exceptions')
    exceptions.NoAudioReceived = FakeNoAudioReceived
    pkg.exceptions = exceptions
    return {'edge_tts': pkg, 'edge_tts.exceptions': exceptions}


def make_mp3():
    import io
    import numpy as np
    import soundfile
    buf = io.BytesIO()
    soundfile.write(buf, np.zeros(1000, dtype=np.float32), 24000, format='MP3')
    return buf.getvalue()


class EdgePipelineTest(unittest.TestCase):
    def build(self, lang_code='zh-TW'):
        fake = FakeEdgeTTS(make_mp3())
        pipeline = backends.EdgePipeline(lang_code)
        return pipeline, fake

    def run_pipeline(self, pipeline, fake, *args, **kwargs):
        # The pipeline imports edge_tts inside __call__, so the fake must be in
        # sys.modules for the call itself, not just for construction.
        with mock.patch.dict(sys.modules, fake_edge_tts_module(fake)):
            return list(pipeline(*args, **kwargs))

    def test_yields_kokoro_shaped_triples_with_numpy_audio(self):
        pipeline, fake = self.build()
        out = self.run_pipeline(pipeline, fake, '你好。', voice='zh-TW-HsiaoChenNeural', speed=1.0)
        self.assertEqual(len(out), 1)
        for graphemes, phonemes, audio in out:
            self.assertIsNone(graphemes)
            self.assertIsNone(phonemes)
            self.assertIsInstance(audio, np.ndarray)
        # gen_audio_segments concatenates these, so they must survive np.concatenate
        self.assertGreater(len(np.concatenate([a for _, _, a in out])), 0)

    def test_maps_speed_to_rate(self):
        pipeline, fake = self.build()
        self.run_pipeline(pipeline, fake, 'Hello.', voice='en-US-AriaNeural', speed=1.5)
        self.run_pipeline(pipeline, fake, 'Hello.', voice='en-US-AriaNeural', speed=0.5)
        self.run_pipeline(pipeline, fake, 'Hello.', voice='en-US-AriaNeural', speed=1.0)
        self.assertEqual([c['rate'] for c in fake.calls], ['+50%', '-50%', '+0%'])

    def test_passes_text_and_voice_through(self):
        pipeline, fake = self.build()
        self.run_pipeline(pipeline, fake, 'Hello.', voice='en-US-AriaNeural', speed=1.0)
        self.assertEqual(fake.calls[0]['text'], 'Hello.')
        self.assertEqual(fake.calls[0]['voice'], 'en-US-AriaNeural')

    def test_ignores_split_pattern(self):
        # edge-tts needs no sentence-level split pattern; the argument must not explode.
        pipeline, fake = self.build()
        out = self.run_pipeline(pipeline, fake, 'Hello.', voice='en-US-AriaNeural',
                                speed=1.0, split_pattern=r'\n\n\n')
        self.assertEqual(len(out), 1)


class ScriptedEdgeTTS:
    """edge_tts stand-in whose outcome is scripted, one entry per Communicate call.

    An entry is either the bytes to stream back or an exception to raise. Empty bytes
    reproduce what the real service does for text it finds nothing to say in: it streams no
    chunk at all and raises nothing.
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []
        self._next = b''

    def Communicate(self, text, voice, rate=None, **kwargs):
        self.calls.append(dict(text=text, voice=voice, rate=rate))
        self._next = self.script.pop(0) if self.script else b''
        return self

    async def stream(self):
        if isinstance(self._next, Exception):
            raise self._next
        if self._next:
            yield {'type': 'audio', 'data': self._next}


class EdgeUnspeakableTextTest(unittest.TestCase):
    """The bug that killed a 15-chapter book 8% in.

    Sentence splitting hands back a bare '\\n' as the last "sentence" of a chapter. edge-tts
    splits that into zero chunks, then streams nothing and raises nothing, so empty bytes
    reached soundfile and surfaced as 'Format not recognised'.
    """

    def run_text(self, text, script=()):
        fake = ScriptedEdgeTTS(script)
        pipeline = backends.EdgePipeline('zh-CN')
        with mock.patch.dict(sys.modules, fake_edge_tts_module(fake)):
            return list(pipeline(text, voice='zh-CN-XiaoxiaoNeural', speed=1.0)), fake

    def test_bare_newline_is_skipped_without_a_request(self):
        out, fake = self.run_text('\n')
        self.assertEqual(out, [])
        self.assertEqual(fake.calls, [], 'nothing to say means nothing to ask the service')

    def test_punctuation_and_whitespace_only_is_skipped(self):
        for text in ['「」', '……', '   ', '', '—', '　　']:
            out, fake = self.run_text(text)
            self.assertEqual(out, [], f'{text!r} should produce no audio')
            self.assertEqual(fake.calls, [], f'{text!r} should not be sent')

    def test_real_text_is_still_synthesized(self):
        out, fake = self.run_text('一九六五年春于美爱荷华城。', script=[make_mp3()])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(fake.calls), 1)

    def test_speakable_covers_han_kana_latin_and_digits(self):
        for text in ['你', 'カナ', 'a', '7']:
            out, fake = self.run_text(text, script=[make_mp3()])
            self.assertEqual(len(fake.calls), 1, f'{text!r} should be sent')


class EdgeRetryTest(unittest.TestCase):
    """Edge is a network service; one dropped response must not cost a multi-hour book."""

    def call(self, script, text='你好。'):
        fake = ScriptedEdgeTTS(script)
        pipeline = backends.EdgePipeline('zh-CN')
        with mock.patch.dict(sys.modules, fake_edge_tts_module(fake)), \
             mock.patch.object(backends, 'time'):  # no real backoff in tests
            return list(pipeline(text, voice='zh-CN-XiaoxiaoNeural', speed=1.0)), fake

    def test_empty_response_is_retried_then_succeeds(self):
        out, fake = self.call([b'', make_mp3()])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(fake.calls), 2)

    def test_persistent_silence_fails_the_chapter_rather_than_truncating_it(self):
        # Yielding nothing here used to write a short .wav that the next run skipped over,
        # so the dropped sentence could never be recovered. EdgeNoAudio costs the chapter,
        # which a re-run can redo.
        with self.assertRaises(backends.EdgeNoAudio):
            self.call([b''] * backends.EDGE_ATTEMPTS)

    def test_transient_exception_is_retried(self):
        out, fake = self.call([RuntimeError('websocket dropped'), make_mp3()])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(fake.calls), 2)

    def test_no_audio_received_is_retried_then_succeeds(self):
        # The reported crash: '②"。' raised NoAudioReceived three times mid-book and read
        # fine moments later. It is speakable -- the service was throttling.
        out, fake = self.call([FakeNoAudioReceived('no audio'), make_mp3()], text='②"。')
        self.assertEqual(len(out), 1)
        self.assertEqual(len(fake.calls), 2)

    def test_persistent_no_audio_fails_the_chapter_after_exhausting_retries(self):
        # However often the service says "no audio", it is never a reason to lose the
        # finished chapters -- but it must not silently truncate this one either.
        with self.assertRaises(backends.EdgeNoAudio) as ctx:
            self.call([FakeNoAudioReceived('no audio')] * backends.EDGE_ATTEMPTS, text='②"。')
        self.assertIn('②"。', str(ctx.exception), 'the fragment must be named')

    def test_edge_no_audio_is_distinct_from_a_transport_failure(self):
        # core.main tells them apart: EdgeNoAudio drops one chapter and the run carries on,
        # an unreachable service ends the run.
        self.assertTrue(issubclass(backends.EdgeNoAudio, RuntimeError))
        with self.assertRaises(RuntimeError) as ctx:
            self.call([ConnectionError('unreachable')] * backends.EDGE_ATTEMPTS)
        self.assertNotIsInstance(ctx.exception, backends.EdgeNoAudio)

    def test_transport_failure_still_raises(self):
        # A service that cannot be reached is a broken run, not a skippable fragment, so
        # this must stay fatal rather than silently producing a book full of gaps.
        with self.assertRaises(RuntimeError):
            self.call([ConnectionError('unreachable')] * backends.EDGE_ATTEMPTS)

    def test_backoff_outlasts_throttling_and_holds_at_the_last_step(self):
        waits = [backends._retry_wait(a) for a in range(1, backends.EDGE_ATTEMPTS + 1)]
        self.assertEqual(waits[:3], [3.0, 8.0, 20.0])
        self.assertEqual(waits[-1], 20.0, 'later attempts hold at the longest wait')
        self.assertGreaterEqual(sum(waits[:backends.EDGE_ATTEMPTS - 1]), 30,
                                'total backoff must outlast throttling, not a round trip')

    def test_persistent_failure_raises_naming_the_text(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.call([RuntimeError('boom')] * backends.EDGE_ATTEMPTS)
        self.assertIn(f'after {backends.EDGE_ATTEMPTS} attempts', str(ctx.exception))
        self.assertIn('你好。', str(ctx.exception))


class AdaptiveConcurrencyGateTest(unittest.TestCase):
    """_AdaptiveConcurrencyGate: starts serial, ramps up only on clean streaks, drops to 1
    on any retry, and never lets more than `limit` callers through at once."""

    def test_starts_fully_serial(self):
        gate = backends._AdaptiveConcurrencyGate(ceiling=3, ramp_streak=2)
        self.assertEqual(gate._limit, 1)

    def test_ramps_up_after_a_streak_of_clean_successes(self):
        gate = backends._AdaptiveConcurrencyGate(ceiling=3, ramp_streak=2)
        for _ in range(2):
            gate.acquire()
            gate.release(needed_retry=False)
        self.assertEqual(gate._limit, 2)

    def test_never_exceeds_the_ceiling(self):
        gate = backends._AdaptiveConcurrencyGate(ceiling=2, ramp_streak=1)
        for _ in range(10):
            gate.acquire()
            gate.release(needed_retry=False)
        self.assertEqual(gate._limit, 2)

    def test_a_retry_drops_the_limit_back_to_one(self):
        gate = backends._AdaptiveConcurrencyGate(ceiling=3, ramp_streak=1)
        gate.acquire()
        gate.release(needed_retry=False)
        self.assertEqual(gate._limit, 2, 'sanity check: one clean call should have ramped it up')
        gate.acquire()
        gate.release(needed_retry=True)
        self.assertEqual(gate._limit, 1)

    def test_a_transport_failure_also_drops_the_limit(self):
        # release() is called from a `finally`, so a call that raised outright must still
        # report itself as needing a retry -- it is at least as strong a throttling signal
        # as one that succeeded on a later attempt.
        gate = backends._AdaptiveConcurrencyGate(ceiling=3, ramp_streak=1)
        gate.acquire()
        gate.release(needed_retry=False)
        self.assertEqual(gate._limit, 2)
        gate.acquire()
        try:
            raise RuntimeError('simulated transport failure')
        except RuntimeError:
            pass
        finally:
            gate.release(needed_retry=True)
        self.assertEqual(gate._limit, 1)

    def test_extra_callers_block_until_a_slot_frees(self):
        gate = backends._AdaptiveConcurrencyGate(ceiling=1, ramp_streak=1)
        gate.acquire()
        acquired = threading.Event()

        def waiter():
            gate.acquire()
            acquired.set()

        t = threading.Thread(target=waiter)
        t.start()
        try:
            self.assertFalse(acquired.wait(timeout=0.1),
                             'a second caller must not proceed while the gate is full')
            gate.release(needed_retry=False)
            self.assertTrue(acquired.wait(timeout=1),
                            'the waiting caller must proceed once a slot frees')
        finally:
            t.join(timeout=1)


class EdgePipelineConcurrencyGateTest(unittest.TestCase):
    """EdgePipeline wires a real call's outcome into its own gate."""

    def call(self, script, text='你好。'):
        fake = ScriptedEdgeTTS(script)
        pipeline = backends.EdgePipeline('zh-CN')
        with mock.patch.dict(sys.modules, fake_edge_tts_module(fake)), \
             mock.patch.object(backends, 'time'):  # no real backoff in tests
            list(pipeline(text, voice='zh-CN-XiaoxiaoNeural', speed=1.0))
        return pipeline

    def test_a_clean_first_attempt_counts_toward_the_ramp(self):
        pipeline = self.call([make_mp3()])
        self.assertEqual(pipeline._gate._streak, 1)
        self.assertEqual(pipeline._gate._limit, 1, 'one success is below the default ramp streak')

    def test_a_call_that_needed_a_retry_keeps_the_gate_at_one(self):
        pipeline = self.call([b'', make_mp3()])
        self.assertEqual(pipeline._gate._limit, 1)
        self.assertEqual(pipeline._gate._streak, 0)


class EdgeAvailableTest(unittest.TestCase):
    def test_true_when_edge_tts_installed(self):
        with mock.patch.dict(sys.modules, {'edge_tts': types.ModuleType('edge_tts')}):
            self.assertTrue(backends.edge_available())

    def test_false_when_not_installed(self):
        with mock.patch.dict(sys.modules, {'edge_tts': None}):
            self.assertFalse(backends.edge_available())


class LangCodeForTest(unittest.TestCase):
    def test_edge_voice_uses_its_locale(self):
        self.assertEqual(lang_code_for('zh-TW-HsiaoChenNeural'), 'zh-TW')
        self.assertEqual(lang_code_for('zh-HK-HiuMaanNeural'), 'zh-HK')
        self.assertEqual(lang_code_for('en-US-AriaNeural'), 'en-US')

    def test_kokoro_voice_uses_first_letter(self):
        self.assertEqual(lang_code_for('af_sky'), 'a')
        self.assertEqual(lang_code_for('zf_xiaobei'), 'z')

    def test_explicit_code_wins(self):
        self.assertEqual(lang_code_for('af_sky', 'b'), 'b')
        self.assertEqual(lang_code_for('zh-TW-HsiaoChenNeural', 'z'), 'z')


class EdgeGetPipelineTest(unittest.TestCase):
    def test_edge_backend_rejects_a_kokoro_voice(self):
        with mock.patch.object(backends, 'edge_available', return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_pipeline('af_sky', backend='edge')
        self.assertIn('not an Edge TTS voice', str(ctx.exception))

    def test_edge_backend_without_package_explains_the_extra(self):
        with mock.patch.object(backends, 'edge_available', return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                get_pipeline('zh-TW-HsiaoChenNeural', backend='edge')
        self.assertIn('.[edge]', str(ctx.exception))

    def test_edge_backend_returns_an_edge_pipeline(self):
        with mock.patch.object(backends, 'edge_available', return_value=True):
            pipeline = get_pipeline('zh-TW-HsiaoChenNeural', backend='edge')
        self.assertIsInstance(pipeline, backends.EdgePipeline)
        self.assertEqual(pipeline.lang_code, 'zh-TW')

    def test_kokoro_backend_rejects_an_edge_voice_up_front(self):
        # Forgetting --backend edge used to fail only after mlx had downloaded a 339 MB
        # repo, or inside KPipeline on a 'zh-TW' lang_code.
        for backend in ('mlx', 'torch'):
            with self.subTest(backend=backend):
                with self.assertRaises(RuntimeError) as ctx:
                    get_pipeline('zh-TW-HsiaoChenNeural', backend=backend)
                self.assertIn('--backend edge', str(ctx.exception))


class EdgeVoiceNameTest(unittest.TestCase):
    """Locale subtags are not always two letters; edge-tts accepts [a-z]{2,}-[A-Z]{2,}-."""

    def test_longer_subtags_are_recognised(self):
        for voice, locale in [('yue-CN-XiaoMinNeural', 'yue-CN'),
                              ('fil-PH-AngeloNeural', 'fil-PH')]:
            with self.subTest(voice=voice):
                self.assertTrue(is_edge_voice(voice))
                self.assertEqual(lang_code_for(voice), locale)

    def test_two_letter_subtags_still_work(self):
        self.assertTrue(is_edge_voice('zh-TW-HsiaoChenNeural'))
        self.assertTrue(is_edge_voice('en-GB-SoniaNeural'))

    def test_kokoro_names_blends_and_paths_are_not_edge_voices(self):
        for voice in ('af_sky', 'af_heart,af_bella', '/voices/custom.pt', '', 'zf_xiaobei'):
            with self.subTest(voice=voice):
                self.assertFalse(is_edge_voice(voice))

    def test_catalog_membership_distinguishes_typed_voices(self):
        self.assertTrue(is_catalog_voice('af_sky'))
        self.assertTrue(is_catalog_voice('zh-TW-HsiaoChenNeural'))
        for typed in ('/voices/custom.pt', 'af_heart,af_bella', 'yue-CN-XiaoMinNeural', ''):
            with self.subTest(typed=typed):
                self.assertFalse(is_catalog_voice(typed))


class MeasuredEtaTest(unittest.TestCase):
    def stats(self, processed, elapsed, seed=900):
        import time
        from types import SimpleNamespace
        return SimpleNamespace(processed_chars=processed, chars_per_sec=seed,
                               start_time=time.time() - elapsed, total_chars=100000)

    def test_keeps_the_seed_until_there_is_enough_data(self):
        from epubcast.core import measured_chars_per_sec
        self.assertEqual(measured_chars_per_sec(self.stats(processed=100, elapsed=60)), 900)
        self.assertEqual(measured_chars_per_sec(self.stats(processed=50000, elapsed=1)), 900)

    def test_switches_to_real_throughput(self):
        from epubcast.core import measured_chars_per_sec
        rate = measured_chars_per_sec(self.stats(processed=20000, elapsed=100))
        self.assertAlmostEqual(rate, 200, delta=5)

    def test_missing_start_time_does_not_explode(self):
        from types import SimpleNamespace
        from epubcast.core import measured_chars_per_sec
        stats = SimpleNamespace(processed_chars=50000, chars_per_sec=900, total_chars=100000)
        self.assertEqual(measured_chars_per_sec(stats), 900)

    def test_resumed_chapters_do_not_inflate_the_rate(self):
        # Re-running a half-finished book credits existing chapters to processed_chars
        # instantly; only characters actually synthesized may drive the rate.
        import time
        from types import SimpleNamespace
        from epubcast.core import measured_chars_per_sec
        stats = SimpleNamespace(processed_chars=800000, synthesized_chars=0,
                                chars_per_sec=900, total_chars=850000,
                                start_time=time.time() - 20)
        self.assertEqual(measured_chars_per_sec(stats), 900)
        stats.synthesized_chars = 4000
        self.assertAlmostEqual(measured_chars_per_sec(stats), 200, delta=5)


class ConcatListTest(unittest.TestCase):
    """The ffmpeg concat list resolves relative entries against its own directory."""

    def build_list(self, tmpdir, names):
        import os
        from pathlib import Path
        from epubcast.core import concat_wavs_with_ffmpeg
        from unittest import mock
        out = Path(tmpdir) / 'audiobooks'
        out.mkdir()
        files = []
        for n in names:
            p = out / n
            p.write_bytes(b'')
            files.append(p)
        written = {}

        def fake_run(cmd, *a, **k):
            list_path = [c for c in cmd if str(c).endswith('_wav_list.txt')][0]
            written['content'] = Path(list_path).read_text()
            Path(cmd[-1]).write_bytes(b'x')  # pretend ffmpeg produced the concat file
            return mock.Mock(returncode=0)

        with mock.patch('epubcast.core.subprocess.run', side_effect=fake_run), \
             mock.patch('epubcast.core.find_aac_encoder', return_value='aac'):
            concat_wavs_with_ffmpeg(files, str(out), 'book.epub')
        return written['content']

    def test_entries_are_absolute(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            content = self.build_list(tmp, ['a.wav', 'b.wav'])
        for line in content.strip().splitlines():
            self.assertRegex(line, r"^file '/", f'relative entry would be resolved twice: {line}')

    def test_single_quotes_are_escaped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            content = self.build_list(tmp, ["it's.wav"])
        self.assertIn(r"'\''", content)


class CreateM4bEncoderTest(unittest.TestCase):
    """The single lossy encode in create_m4b uses the best AAC encoder ffmpeg has.

    The concat step now writes a lossless PCM intermediate, so the only AAC encode is the
    final one; it must pick the encoder via find_aac_encoder rather than hardcoding 'aac'.
    """

    def test_uses_the_found_aac_encoder(self):
        import tempfile
        from pathlib import Path
        from epubcast.core import create_m4b
        with tempfile.TemporaryDirectory() as tmp:
            concat = Path(tmp) / 'book.tmp.wav'
            concat.write_bytes(b'x')
            calls = []

            def fake_run(cmd, *a, **k):
                calls.append(cmd)
                return mock.Mock(returncode=0)

            with mock.patch('epubcast.core.concat_wavs_with_ffmpeg', return_value=concat), \
                 mock.patch('epubcast.core.find_aac_encoder', return_value='libfdk_aac'), \
                 mock.patch('epubcast.core.subprocess.run', side_effect=fake_run):
                create_m4b([], 'book.epub', b'', tmp)
            m4b_cmd = calls[-1]
            self.assertIn('libfdk_aac', m4b_cmd)
            self.assertIn('64k', m4b_cmd)


class GenTextLangCodeTest(unittest.TestCase):
    """gen_text has to resolve the language the same way main() does.

    It used to take voice[:1], which reads 'zh-TW-HsiaoChenNeural' as Kokoro's 'z' -- so a
    Taiwan voice, which reads traditional script natively, got its text simplified first.
    """

    def gen_text_lang_code(self, voice, lang_code=None):
        """The lang_code gen_text hands to the pipeline and to gen_audio_segments."""
        from epubcast import core
        seen = {}

        def fake_get_pipeline(voice, lang_code=None, backend='auto', repo_id=None):
            seen['pipeline'] = lang_code
            return mock.Mock()

        def fake_gen_audio_segments(pipeline, text, voice, speed, **kwargs):
            seen['segments'] = kwargs.get('lang_code')
            return [np.zeros(4, dtype='float32')]

        with mock.patch.object(core, 'get_pipeline', side_effect=fake_get_pipeline), \
             mock.patch.object(core, 'gen_audio_segments', side_effect=fake_gen_audio_segments), \
             mock.patch.object(core, 'set_espeak_library'), \
             mock.patch.object(core, 'load_spacy'), \
             mock.patch.object(core.soundfile, 'write'):
            core.gen_text('hello', voice=voice, lang_code=lang_code, output_file='x.wav')
        self.assertEqual(seen['pipeline'], seen['segments'],
                         'the pipeline and the text normalizer must agree on the language')
        return seen['pipeline']

    def test_edge_voice_keeps_its_full_locale(self):
        self.assertEqual(self.gen_text_lang_code('zh-TW-HsiaoChenNeural'), 'zh-TW')
        self.assertEqual(self.gen_text_lang_code('zh-HK-HiuMaanNeural'), 'zh-HK')
        self.assertEqual(self.gen_text_lang_code('en-US-AriaNeural'), 'en-US')

    def test_kokoro_voice_still_uses_its_first_letter(self):
        self.assertEqual(self.gen_text_lang_code('af_sky'), 'a')
        self.assertEqual(self.gen_text_lang_code('zf_xiaoxiao'), 'z')

    def test_explicit_lang_code_wins(self):
        self.assertEqual(self.gen_text_lang_code('/voices/custom.pt', lang_code='j'), 'j')


class DefaultLanguagesTest(unittest.TestCase):
    """Which languages the GUI ticks on startup: Kokoro narrows to English and
    Chinese, while Edge ticks every locale (its voice dropdown is filtered to the
    multilingual voices anyway).

    Lives here rather than in a UI test because wxPython is an optional extra that CI does
    not install -- and the rule is pure data, with no widget involved.
    """

    def test_kokoro_codes_narrow_to_english_and_chinese(self):
        self.assertEqual(default_languages(list(voices.keys())), {'a', 'b', 'z'})

    def test_edge_locales_default_to_all(self):
        chosen = default_languages(list(edge_voices.keys()))
        self.assertEqual(chosen, set(edge_voices.keys()))

    def test_kokoro_other_languages_are_left_unticked(self):
        for code in ('e', 'f', 'h', 'i', 'j', 'p'):
            self.assertFalse(is_default_language(code), f'{code} should start unticked')

    def test_every_offered_language_is_still_available(self):
        # Kokoro narrows the ticks but not the list itself; Edge ticks everything.
        self.assertTrue(default_languages(list(voices.keys())).issubset(set(voices.keys())))
        self.assertLess(len(default_languages(list(voices.keys()))), len(voices))
        self.assertEqual(default_languages(list(edge_voices.keys())), set(edge_voices.keys()))

    def test_unrecognised_codes_fall_back_to_everything(self):
        # An empty tick set would mean an empty voice dropdown, which is worse than noise.
        self.assertEqual(default_languages(['q', 'x']), {'q', 'x'})

    def test_the_two_naming_schemes_cannot_collide(self):
        # One predicate serves both only because no Kokoro letter is a locale and vice
        # versa; a single-letter code must never match a locale entry.
        for letter in voices:
            self.assertNotIn(letter, DEFAULT_LOCALES)
        for locale in edge_voices:
            self.assertNotIn(locale, DEFAULT_LANGUAGES)


class SafeFilenameTest(unittest.TestCase):
    def test_blended_voice_is_safe(self):
        self.assertEqual(safe_filename_part('af_heart,af_bella'), 'af_heart_af_bella')

    def test_voice_path_is_safe(self):
        self.assertEqual(safe_filename_part('/voices/my custom.pt'), 'voices_my_custom.pt')

    def test_plain_voice_is_untouched(self):
        self.assertEqual(safe_filename_part('af_sky'), 'af_sky')


class BatchForEdgeTest(unittest.TestCase):
    """_batch_for_edge groups sentences for one Edge call each, without dropping any."""

    def test_short_sentences_are_merged_into_one_batch(self):
        from epubcast.core import _batch_for_edge
        sentences = ['一。', '二。', '三。']
        batches = _batch_for_edge(sentences, max_chars=100)
        self.assertEqual(batches, [sentences])

    def test_a_full_batch_starts_a_new_one(self):
        from epubcast.core import _batch_for_edge
        sentences = ['a' * 40, 'b' * 40, 'c' * 40]
        batches = _batch_for_edge(sentences, max_chars=50)
        self.assertEqual(batches, [['a' * 40], ['b' * 40], ['c' * 40]])

    def test_a_sentence_over_the_budget_gets_its_own_batch_rather_than_being_split(self):
        from epubcast.core import _batch_for_edge
        huge = 'x' * 500
        batches = _batch_for_edge(['short.', huge, 'short.'], max_chars=100)
        self.assertEqual(batches, [['short.'], [huge], ['short.']])

    def test_empty_input_yields_no_batches(self):
        from epubcast.core import _batch_for_edge
        self.assertEqual(_batch_for_edge([], max_chars=100), [])

    def test_every_sentence_survives_regardless_of_grouping(self):
        from epubcast.core import _batch_for_edge
        sentences = [f'sentence {i}.' for i in range(37)]
        batches = _batch_for_edge(sentences, max_chars=30)
        flattened = [s for batch in batches for s in batch]
        self.assertEqual(flattened, sentences)


class GenAudioSegmentsEdgeBatchingTest(unittest.TestCase):
    """gen_audio_segments batches consecutive sentences into one Edge call, and only Edge."""

    class RecordingEdgePipeline(backends.EdgePipeline):
        """A real EdgePipeline subclass (so it inherits batch_chars and max_concurrency)
        that never touches the network: it just records the text of each call."""

        def __init__(self):
            self.calls = []

        def __call__(self, text, voice, speed=1.0, split_pattern=None):
            self.calls.append(text)
            yield None, None, np.zeros(4, dtype='float32')

    # English so spaCy's sentencizer actually splits on '.': the xx_ent_wiki_sm sentencizer
    # used here does not split Chinese text on the full-width '。' (verified separately), so
    # a Chinese sample would land as a single spaCy "sentence" and prove nothing about
    # batching multiple sentences together.
    FIVE_SENTENCES = ('This is sentence one. This is sentence two. This is sentence three. '
                      'This is sentence four. This is sentence five.')

    def test_edge_pipeline_receives_fewer_calls_than_there_are_sentences(self):
        from epubcast.core import gen_audio_segments
        pipeline = self.RecordingEdgePipeline()
        gen_audio_segments(pipeline, self.FIVE_SENTENCES, voice='en-US-BrianNeural', speed=1.0)
        self.assertEqual(len(pipeline.calls), 1, pipeline.calls)
        self.assertEqual(pipeline.calls[0], self.FIVE_SENTENCES)

    def test_edge_pipeline_still_splits_once_the_batch_budget_is_exceeded(self):
        from epubcast.core import EDGE_BATCH_CHARS, gen_audio_segments
        sentence = 'This is a filler sentence used to pad the batch past its budget. '
        text = sentence * (EDGE_BATCH_CHARS // len(sentence) + 5)
        pipeline = self.RecordingEdgePipeline()
        gen_audio_segments(pipeline, text, voice='en-US-BrianNeural', speed=1.0)
        self.assertGreater(len(pipeline.calls), 1)
        for call in pipeline.calls:
            self.assertLessEqual(len(call), EDGE_BATCH_CHARS + len(sentence))
        expected_count = text.count('filler sentence')
        actual_count = sum(call.count('filler sentence') for call in pipeline.calls)
        self.assertEqual(actual_count, expected_count, 'no sentence should be dropped or duplicated')

    def test_non_edge_pipelines_keep_one_call_per_sentence(self):
        from epubcast.core import gen_audio_segments
        calls = []

        def fake_pipeline(sent_text, voice, speed=1.0, split_pattern=None):
            calls.append(sent_text)
            yield None, None, np.zeros(4, dtype='float32')

        gen_audio_segments(fake_pipeline, self.FIVE_SENTENCES, voice='af_heart', speed=1.0)
        self.assertEqual(len(calls), 5, calls)


class SlowConcurrentPipeline:
    """Fake pipeline that takes real wall-clock time per call and tracks how many calls
    were ever in flight together, to prove gen_audio_segments actually overlaps calls for
    a pipeline that opts in -- rather than just trusting that a thread pool was created."""

    batch_chars = None

    def __init__(self, max_concurrency, delay=0.05):
        self.max_concurrency = max_concurrency
        self.delay = delay
        self._lock = threading.Lock()
        self._active = 0
        self.max_active_seen = 0

    def __call__(self, text, voice, speed=1.0, split_pattern=None):
        with self._lock:
            self._active += 1
            self.max_active_seen = max(self.max_active_seen, self._active)
        time.sleep(self.delay)
        with self._lock:
            self._active -= 1
        yield None, None, np.zeros(4, dtype='float32')


class GenAudioSegmentsConcurrencyDispatchTest(unittest.TestCase):
    """gen_audio_segments dispatches to a thread pool sized from `max_concurrency`, for any
    pipeline that opts in -- not just EdgePipeline -- and stays single-threaded otherwise."""

    SIX_SENTENCES = ' '.join(f'This is sentence number {i}.' for i in range(6))

    def test_overlaps_calls_for_a_pipeline_that_opts_in(self):
        from epubcast.core import gen_audio_segments
        pipeline = SlowConcurrentPipeline(max_concurrency=3)
        gen_audio_segments(pipeline, self.SIX_SENTENCES, voice='en-US-BrianNeural', speed=1.0)
        self.assertGreater(pipeline.max_active_seen, 1,
                           'more than one call should have been in flight at once')
        self.assertLessEqual(pipeline.max_active_seen, 3, 'must not exceed max_concurrency')

    def test_stays_serial_when_max_concurrency_is_one(self):
        from epubcast.core import gen_audio_segments
        pipeline = SlowConcurrentPipeline(max_concurrency=1)
        gen_audio_segments(pipeline, self.SIX_SENTENCES, voice='en-US-BrianNeural', speed=1.0)
        self.assertEqual(pipeline.max_active_seen, 1)

    def test_stays_serial_for_a_pipeline_that_never_opts_in(self):
        from epubcast.core import gen_audio_segments
        calls = []

        def fake_pipeline(sent_text, voice, speed=1.0, split_pattern=None):
            calls.append(sent_text)
            yield None, None, np.zeros(4, dtype='float32')

        # No max_concurrency attribute at all -- gen_audio_segments must default to 1.
        gen_audio_segments(fake_pipeline, self.SIX_SENTENCES, voice='af_heart', speed=1.0)
        self.assertEqual(len(calls), 6)

    def test_audio_stays_in_chunk_order_despite_overlap(self):
        # The underlying calls run concurrently and can finish in any order (nothing
        # guarantees the 2nd chunk's network round trip returns before the 5th's) -- what
        # gen_audio_segments must still guarantee is that its *returned* audio comes back
        # in chunk order, since that is what gets concatenated into the chapter's .wav.
        from epubcast.core import gen_audio_segments

        class IndexTaggedPipeline(SlowConcurrentPipeline):
            """Each chunk's 'audio' is its own sentence index, so the order of
            gen_audio_segments's return value can be checked directly."""

            def __call__(self, text, voice, speed=1.0, split_pattern=None):
                for _ in super().__call__(text, voice, speed, split_pattern):
                    index = int(text.split('number ')[1].split('.')[0])
                    yield None, None, np.array([index], dtype='float32')

        pipeline = IndexTaggedPipeline(max_concurrency=3, delay=0.02)
        audio_segments = gen_audio_segments(pipeline, self.SIX_SENTENCES,
                                            voice='en-US-BrianNeural', speed=1.0)
        indices = [int(audio[0]) for audio in audio_segments]
        self.assertEqual(indices, list(range(6)),
                         'audio must come back in chunk order despite overlapping calls')


class SentencizerAutoDownloadTest(unittest.TestCase):
    """gen_audio_segments is called directly by tests (see above) and could be by other
    callers too -- it must not assume main() or gen_text() already downloaded the spaCy
    model. A bare spacy.load() raises OSError E050 on a machine that has never fetched
    xx_ent_wiki_sm; this broke CI the first time GenAudioSegmentsEdgeBatchingTest ran on a
    fresh runner, since nothing upstream of it had called load_spacy() yet."""

    def test_get_sentencizer_downloads_the_model_if_missing(self):
        from epubcast import core
        with mock.patch.object(core, '_sentencizer', None), \
             mock.patch.object(core, 'load_spacy') as load_spacy:
            core._get_sentencizer()
        load_spacy.assert_called_once()


class LanguageNameTest(unittest.TestCase):
    """The language filter reads as a language list, not a column of codes."""

    def test_kokoro_codes_get_names(self):
        from epubcast.voices import language_name
        self.assertEqual(language_name('a'), 'English (US)')
        self.assertEqual(language_name('z'), 'Chinese')

    def test_edge_locales_get_names(self):
        from epubcast.voices import language_name
        self.assertEqual(language_name('zh-TW'), 'Chinese (Traditional)')
        self.assertEqual(language_name('zh-HK'), 'Cantonese')

    def test_unknown_code_falls_back_to_the_code(self):
        # Better a bare code than a blank row for a locale added without a name.
        from epubcast.voices import language_name
        self.assertEqual(language_name('xx-YY'), 'xx-YY')

    def test_every_offered_code_has_a_name(self):
        from epubcast.voices import (KOKORO_LANGUAGE_NAMES, EDGE_LANGUAGE_NAMES, edge_voices,
                                     language_name, voices)
        for code in list(voices) + list(edge_voices):
            with self.subTest(code=code):
                self.assertNotEqual(language_name(code), code,
                                    f'{code} has no human name')
        self.assertEqual(set(KOKORO_LANGUAGE_NAMES), set(voices))
        self.assertEqual(set(EDGE_LANGUAGE_NAMES), set(edge_voices))


def fake_kokoro(pipeline_calls, model_calls, spoken):
    """A stub kokoro package: KPipeline and KModel record how they were built."""
    kokoro = types.ModuleType('kokoro')

    class FakePipeline:
        def __init__(self, **kwargs):
            pipeline_calls.append(kwargs)
            self.model = kwargs.get('model')

        def __call__(self, text, voice, speed=1.0, split_pattern=None):
            spoken.append(dict(text=text, voice=voice, speed=speed,
                               split_pattern=split_pattern))
            yield 'graphemes', 'phonemes', np.zeros(4, dtype=np.float32)

    kokoro.KPipeline = FakePipeline

    model_mod = types.ModuleType('kokoro.model')

    class FakeKModel:
        MODEL_NAMES = {'hexgrad/Kokoro-82M': 'kokoro-v1_0.pth'}

        def __init__(self, repo_id=None, config=None, model=None):
            model_calls.append(dict(repo_id=repo_id, config=config, model=model))
            self.device = 'cpu'

        def to(self, device):
            return self

        def eval(self):
            return self

    model_mod.KModel = FakeKModel
    kokoro.model = model_mod
    return {'kokoro': kokoro, 'kokoro.model': model_mod}


def fake_hub(downloads, fail=False):
    """A stub huggingface_hub recording each download and its revision."""
    hub = types.ModuleType('huggingface_hub')

    def hf_hub_download(repo_id, filename, revision=None):
        if fail:
            raise OSError('offline')
        downloads.append(dict(repo_id=repo_id, filename=filename, revision=revision))
        return f'/cache/{repo_id}/{filename}'

    hub.hf_hub_download = hf_hub_download
    return {'huggingface_hub': hub}


class PinnedTorchPipelineTest(unittest.TestCase):
    """The torch engine loads its weights at DEFAULT_REVISIONS, not the repo's branch.

    kokoro exposes no revision argument, so this adapter resolves the config, the weights
    and the voice pack itself and hands them to kokoro's own loader. These tests pin the
    contract of that adapter without touching the network or downloading a model.
    """

    def build(self, repo_id=None, revision=None, fail_download=False, spoken=None):
        spoken = spoken if spoken is not None else []
        downloads, model_calls, pipeline_calls = [], [], []
        modules = {**fake_kokoro(pipeline_calls, model_calls, spoken),
                   **fake_hub(downloads, fail=fail_download)}
        # Patched for the whole test, not just for construction: voice packs are resolved
        # lazily on the first call, so a narrowed context would send that call to the real
        # Hugging Face client.
        patchers = [
            mock.patch.dict(sys.modules, modules),
            mock.patch('torch.load', side_effect=lambda path, **kw: np.zeros(3, dtype=np.float32)),
            mock.patch('torch.stack', side_effect=lambda packs, dim=0: packs),
            mock.patch('torch.mean', side_effect=lambda stacked, dim=0: np.zeros(3)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        pipeline = backends.PinnedTorchPipeline('a', repo_id, revision)
        return pipeline, downloads, model_calls, pipeline_calls

    def test_default_repo_loads_at_the_pinned_commit(self):
        pipeline, downloads, model_calls, _ = self.build()
        self.assertEqual(pipeline.revision, backends.DEFAULT_REVISIONS['torch'])
        for download in downloads:
            self.assertEqual(download['revision'], backends.DEFAULT_REVISIONS['torch'])
        self.assertEqual({d['filename'] for d in downloads},
                         {'config.json', 'kokoro-v1_0.pth'})
        # kokoro is handed local paths, which is what makes the revision stick.
        self.assertEqual(model_calls[0]['config'], '/cache/hexgrad/Kokoro-82M/config.json')
        self.assertEqual(model_calls[0]['model'], '/cache/hexgrad/Kokoro-82M/kokoro-v1_0.pth')

    def test_an_explicit_revision_wins(self):
        pinned = 'c' * 40
        pipeline, downloads, _, _ = self.build(revision=pinned)
        self.assertEqual(pipeline.revision, pinned)
        self.assertTrue(all(d['revision'] == pinned for d in downloads))

    def test_a_custom_repo_is_left_on_its_own_branch(self):
        # DEFAULT_REVISIONS['torch'] names a commit in the default repo and would not
        # resolve in a different one, so a caller-supplied repo must not inherit it.
        pipeline, downloads, model_calls, pipeline_calls = self.build(repo_id='someone/Other-82M')
        self.assertIsNone(pipeline.revision)
        self.assertEqual(downloads, [])
        self.assertEqual(model_calls, [])
        self.assertEqual(pipeline_calls[0]['repo_id'], 'someone/Other-82M')

    def test_a_failed_download_falls_back_instead_of_failing_the_run(self):
        # Unpinned weights beat no audiobook, and the fallback says so out loud.
        pipeline, _, model_calls, pipeline_calls = self.build(fail_download=True)
        self.assertEqual(model_calls, [])
        self.assertEqual(pipeline_calls[0]['repo_id'], backends.DEFAULT_REPOS['torch'])
        self.assertNotIn('model', pipeline_calls[0])

    def test_a_voice_name_is_fetched_at_the_pinned_commit(self):
        spoken = []
        pipeline, downloads, _, _ = self.build(spoken=spoken)
        list(pipeline('Hello.', voice='af_sky'))
        voice_downloads = [d for d in downloads if d['filename'].startswith('voices/')]
        self.assertEqual(voice_downloads[0]['filename'], 'voices/af_sky.pt')
        self.assertEqual(voice_downloads[0]['revision'], backends.DEFAULT_REVISIONS['torch'])
        self.assertEqual(spoken[0]['text'], 'Hello.')

    def test_a_local_pt_path_is_not_downloaded(self):
        pipeline, downloads, _, _ = self.build()
        list(pipeline('Hello.', voice='/voices/custom.pt'))
        self.assertEqual([d for d in downloads if 'voices/' in d['filename']], [])

    def test_a_blend_downloads_each_part_and_averages_them(self):
        pipeline, downloads, _, _ = self.build()
        list(pipeline('Hello.', voice='af_sky,af_alloy'))
        blended = [d['filename'] for d in downloads if d['filename'].startswith('voices/')]
        self.assertEqual(blended, ['voices/af_sky.pt', 'voices/af_alloy.pt'])

    def test_a_voice_pack_is_cached_after_the_first_use(self):
        pipeline, downloads, _, _ = self.build()
        list(pipeline('One.', voice='af_sky'))
        list(pipeline('Two.', voice='af_sky'))
        fetched = [d for d in downloads if d['filename'] == 'voices/af_sky.pt']
        self.assertEqual(len(fetched), 1, 'the second call must reuse the loaded pack')

    def test_a_tensor_voice_is_passed_straight_through(self):
        pipeline, downloads, _, _ = self.build()
        import torch
        tensor = np.zeros(3, dtype=np.float32)
        with mock.patch.object(torch, 'Tensor', np.ndarray):
            list(pipeline('Hello.', voice=tensor))
        self.assertEqual([d for d in downloads if d['filename'].startswith('voices/')], [])


if __name__ == '__main__':
    unittest.main()
