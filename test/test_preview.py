# -*- coding: utf-8 -*-
"""Tests for the voice preview: which script a voice is given, and how it is played.

No wxPython involved, so these run in CI (which does not install the [gui] extra).
"""
import unittest
from unittest import mock

from epubcast import preview


class LanguageDetectionTest(unittest.TestCase):
    def test_kokoro_english_codes(self):
        for code in ('a', 'b'):
            self.assertTrue(preview.speaks_english(code), code)

    def test_edge_english_locales(self):
        for code in ('en-US', 'en-GB', 'en-AU', 'en-IN'):
            self.assertTrue(preview.speaks_english(code), code)

    def test_kokoro_chinese_code(self):
        self.assertTrue(preview.speaks_chinese('z'))

    def test_chinese_locales_and_cantonese(self):
        # 'yue' is Cantonese, which Kokoro has no voice for but Edge does.
        for code in ('zh-CN', 'zh-TW', 'zh-HK', 'yue-CN'):
            self.assertTrue(preview.speaks_chinese(code), code)

    def test_other_languages_are_neither(self):
        for code in ('e', 'f', 'h', 'i', 'j', 'p', 'it-IT', 'ja-JP'):
            self.assertFalse(preview.speaks_english(code), code)
            self.assertFalse(preview.speaks_chinese(code), code)

    def test_empty_code_does_not_explode(self):
        self.assertFalse(preview.speaks_english(''))
        self.assertFalse(preview.speaks_chinese(None))

    def test_multilingual_only_when_the_name_says_so(self):
        self.assertTrue(preview.is_multilingual('en-US-AndrewMultilingualNeural'))
        self.assertFalse(preview.is_multilingual('en-US-AriaNeural'))
        self.assertFalse(preview.is_multilingual('af_sky'))
        self.assertFalse(preview.is_multilingual(None))


class ClipTextTest(unittest.TestCase):
    def test_short_text_is_returned_whole(self):
        self.assertEqual(preview.clip_text('A short line.'), 'A short line.')

    def test_long_text_is_cut_back_to_a_sentence_end(self):
        text = ('This first sentence is here. ' * 20).strip()
        clipped = preview.clip_text(text, limit=60)
        self.assertLessEqual(len(clipped), 60)
        self.assertTrue(clipped.endswith('.'), clipped)

    def test_no_sentence_end_nearby_falls_back_to_a_hard_cut(self):
        clipped = preview.clip_text('x' * 500, limit=40)
        self.assertEqual(clipped, 'x' * 40)

    def test_whitespace_is_normalized(self):
        self.assertEqual(preview.clip_text('a\n\n b\t c'), 'a b c')

    def test_empty_text_stays_empty(self):
        for value in ('', None, '   \n '):
            self.assertEqual(preview.clip_text(value), '')


class SampleSegmentsTest(unittest.TestCase):
    def test_english_voice_gets_only_the_english_sample(self):
        # An English Kokoro voice handed Chinese characters runs them through an English
        # G2P and mangles them, so auto must not offer Chinese here.
        segments = preview.sample_segments('af_sky', 'a')
        self.assertEqual([label for label, _ in segments], ['English'])
        self.assertEqual(segments[0][1], preview.SAMPLE_EN)

    def test_chinese_voice_gets_only_the_chinese_sample(self):
        segments = preview.sample_segments('zf_xiaobei', 'z')
        self.assertEqual([label for label, _ in segments], ['Chinese'])
        self.assertEqual(segments[0][1], preview.SAMPLE_ZH)

    def test_multilingual_voice_gets_both_scripts(self):
        segments = preview.sample_segments('en-US-AndrewMultilingualNeural', 'en-US')
        self.assertEqual([label for label, _ in segments], ['English', 'Chinese'])

    def test_other_language_with_an_open_chapter_reads_the_chapter(self):
        segments = preview.sample_segments('im_nicola', 'i', chapter_text='Il testo del libro.')
        self.assertEqual([label for label, _ in segments], ['Chapter'])
        self.assertEqual(segments[0][1], 'Il testo del libro.')

    def test_other_language_with_nothing_open_falls_back_to_english(self):
        segments = preview.sample_segments('im_nicola', 'i')
        self.assertEqual([label for label, _ in segments], ['English'])

    def test_explicit_modes_win_over_the_voice_language(self):
        self.assertEqual([l for l, _ in preview.sample_segments('af_sky', 'a', preview.MODE_CHINESE)],
                         ['Chinese'])
        self.assertEqual([l for l, _ in preview.sample_segments('zf_xiaobei', 'z', preview.MODE_ENGLISH)],
                         ['English'])

    def test_chapter_mode_uses_the_chapter_when_there_is_one(self):
        segments = preview.sample_segments('af_sky', 'a', preview.MODE_CHAPTER, 'Chapter text.')
        self.assertEqual([label for label, _ in segments], ['Chapter'])

    def test_chapter_mode_with_nothing_open_falls_back_to_auto(self):
        # Rather than refusing to preview: the user picked a mode, not a broken state.
        segments = preview.sample_segments('af_sky', 'a', preview.MODE_CHAPTER, '')
        self.assertEqual([label for label, _ in segments], ['English'])

    def test_every_mode_is_offered_and_honoured(self):
        for mode in preview.SAMPLE_MODES:
            with self.subTest(mode=mode):
                segments = preview.sample_segments('af_sky', 'a', mode, 'Chapter text.')
                self.assertTrue(segments, f'{mode} produced nothing to say')
                for _label, text in segments:
                    self.assertTrue(text)


class PlayerCommandTest(unittest.TestCase):
    def which(self, *available):
        """A stand-in for shutil.which that only knows `available`."""
        return lambda name: f'/usr/bin/{name}' if name in available else None

    def test_macos_prefers_afplay(self):
        command = preview.player_command('/tmp/a.wav', 'darwin', self.which('afplay', 'ffplay'))
        self.assertEqual(command, ['afplay', '/tmp/a.wav'])

    def test_macos_without_afplay_uses_ffplay(self):
        command = preview.player_command('/tmp/a.wav', 'darwin', self.which('ffplay'))
        self.assertEqual(command[0], 'ffplay')
        self.assertEqual(command[-1], '/tmp/a.wav')

    def test_linux_uses_paplay_then_aplay(self):
        self.assertEqual(preview.player_command('/tmp/a.wav', 'linux', self.which('paplay'))[0],
                         'paplay')
        self.assertEqual(preview.player_command('/tmp/a.wav', 'linux', self.which('aplay'))[0],
                         'aplay')

    def test_linux_without_a_player_can_still_open_the_file(self):
        self.assertEqual(preview.player_command('/tmp/a.wav', 'linux', self.which('xdg-open'))[0],
                         'xdg-open')

    def test_nothing_available_returns_none_rather_than_guessing(self):
        # The caller turns this into a clear message; a guessed command would fail obscurely.
        self.assertIsNone(preview.player_command('/tmp/a.wav', 'linux', self.which()))

    def test_a_path_with_spaces_survives(self):
        command = preview.player_command('/tmp/my preview.wav', 'darwin', self.which('afplay'))
        self.assertEqual(command[1], '/tmp/my preview.wav')


class RenderPreviewTest(unittest.TestCase):
    """render_preview stitches the segments together, with a beat between them."""

    def render(self, segments, audio_length=240):
        import numpy as np
        calls = []

        def fake_gen_audio_segments(pipeline, text, voice, speed, lang_code=None):
            calls.append(dict(text=text, voice=voice, speed=speed, lang_code=lang_code))
            return [np.zeros(audio_length, dtype=np.float32)]

        with mock.patch('epubcast.core.set_espeak_library'), \
             mock.patch('epubcast.core.load_spacy'), \
             mock.patch('epubcast.core.gen_audio_segments', side_effect=fake_gen_audio_segments), \
             mock.patch('epubcast.backends.get_pipeline', return_value=mock.Mock()):
            audio = preview.render_preview('af_sky', 'a', 'torch', 1.25, segments)
        return audio, calls

    def test_one_segment_produces_that_much_audio(self):
        audio, calls = self.render([('English', 'Hello.')])
        self.assertEqual(len(audio), 240)
        self.assertEqual(len(calls), 1)

    def test_two_segments_get_a_silent_beat_between_them(self):
        audio, calls = self.render([('English', 'Hello.'), ('Chinese', '你好。')])
        self.assertEqual(len(calls), 2)
        gap = int(24000 * preview.SAMPLE_GAP_SECONDS)
        self.assertEqual(len(audio), 240 * 2 + gap)

    def test_voice_speed_and_language_are_passed_through(self):
        _audio, calls = self.render([('English', 'Hello.')])
        self.assertEqual(calls[0], dict(text='Hello.', voice='af_sky', speed=1.25, lang_code='a'))

    def test_no_segments_produces_empty_audio(self):
        audio, calls = self.render([])
        self.assertEqual(len(audio), 0)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
