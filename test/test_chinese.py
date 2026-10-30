# -*- coding: utf-8 -*-
import sys
import types
import unittest
from unittest import mock

from epubcast import chinese


TRADITIONAL = '他發現裡面的東西乾乾淨淨。歷史上，臺灣的鐵路系統很發達。'
SIMPLIFIED = '他发现里面的东西干干净净。历史上，台湾的铁路系统很发达。'


class TestToSimplified(unittest.TestCase):
    def setUp(self):
        chinese.reset_notice()

    def test_converts_traditional(self):
        self.assertEqual(chinese.to_simplified(TRADITIONAL), SIMPLIFIED)

    def test_leaves_simplified_alone(self):
        self.assertEqual(chinese.to_simplified(SIMPLIFIED), SIMPLIFIED)

    def test_leaves_latin_alone(self):
        self.assertEqual(chinese.to_simplified('Hello, world.'), 'Hello, world.')

    def test_missing_opencc_is_a_warning_not_a_crash(self):
        chinese._converter = None
        chinese._converter_failed = False
        try:
            with mock.patch.dict('sys.modules', {'opencc': None}):
                with self.assertWarns(UserWarning):
                    self.assertEqual(chinese.to_simplified(TRADITIONAL), TRADITIONAL)
        finally:
            chinese._converter = None
            chinese._converter_failed = False


class TestWantsSimplification(unittest.TestCase):
    def test_chinese_language_codes(self):
        for code in ('z', 'Z'):
            self.assertTrue(chinese.wants_simplification(code, TRADITIONAL), code)

    def test_other_languages_are_left_alone(self):
        # Japanese kanji are shinjitai, not Chinese traditional characters: t2s would be
        # rewriting a script it does not apply to.
        for code in ('a', 'b', 'j'):
            self.assertFalse(chinese.wants_simplification(code, TRADITIONAL), code)

    def test_unknown_language_falls_back_to_the_text(self):
        # A caller that names no language at all: the text has to speak for itself.
        self.assertTrue(chinese.wants_simplification(None, TRADITIONAL))
        self.assertTrue(chinese.wants_simplification('', TRADITIONAL))
        self.assertFalse(chinese.wants_simplification(None, 'Hello, world.'))

    def test_unknown_language_skips_japanese_and_korean(self):
        self.assertFalse(chinese.wants_simplification(None, 'これは日本語の文章です。'))
        self.assertFalse(chinese.wants_simplification(None, '한국어 문장 漢字 포함.'))


class TestEdgeLocales(unittest.TestCase):
    """The Edge backend: zh-CN behaves like Kokoro's 'z', zh-TW/zh-HK read traditional natively."""

    def test_zh_cn_converts_like_kokoro(self):
        for code in ('zh-CN', 'zh-cn'):
            self.assertTrue(chinese.wants_simplification(code, TRADITIONAL), code)

    def test_zh_tw_and_zh_hk_do_not_convert(self):
        for code in ('zh-TW', 'zh-HK', 'zh-tw', 'zh-hk'):
            self.assertFalse(chinese.wants_simplification(code, TRADITIONAL), code)

    def test_normalize_leaves_traditional_alone_for_zh_tw(self):
        chinese.reset_notice()
        self.assertEqual(chinese.normalize(TRADITIONAL, 'zh-TW'), TRADITIONAL)

    def test_normalize_converts_for_zh_cn(self):
        chinese.reset_notice()
        self.assertEqual(chinese.normalize(TRADITIONAL, 'zh-CN'), SIMPLIFIED)


class TestNormalize(unittest.TestCase):
    def setUp(self):
        chinese.reset_notice()

    def test_converts_for_chinese(self):
        self.assertEqual(chinese.normalize(TRADITIONAL, 'z'), SIMPLIFIED)

    def test_does_not_convert_for_japanese(self):
        self.assertEqual(chinese.normalize(TRADITIONAL, 'j'), TRADITIONAL)

    def test_notifies_once_and_only_when_something_changed(self):
        notices = []
        chinese.normalize(SIMPLIFIED, 'z', notify=notices.append)
        self.assertEqual(notices, [])
        chinese.normalize(TRADITIONAL, 'z', notify=notices.append)
        chinese.normalize(TRADITIONAL, 'z', notify=notices.append)
        self.assertEqual(len(notices), 1)


class TestAspectParticle(unittest.TestCase):
    """著 is why the converter is configured tw2s rather than t2s.

    t2s leaves 著 alone -- it is a simplified character too, read zhù -- so every progressive
    sentence in a traditional novel comes out as "xiào zhù shuō". Both halves matter: the
    particle has to change, and the 著 of 著作/著名 has to not.
    """

    def test_particle_becomes_zhe(self):
        self.assertEqual(chinese.to_simplified('她笑著說'), '她笑着说')
        self.assertEqual(chinese.to_simplified('他坐著等'), '他坐着等')

    def test_the_other_zhu_is_left_alone(self):
        self.assertEqual(chinese.to_simplified('著名的著作'), '著名的著作')

    def test_vocabulary_is_not_rewritten(self):
        # tw2sp would turn these into 软件 and 计算机: the author's words, not their script.
        self.assertEqual(chinese.to_simplified('軟體、計程車'), '软体、计程车')


class TestPronunciation(unittest.TestCase):
    """The point of the conversion: the phonemes the model is asked to say.

    Skipped when misaki's Chinese extra is not installed -- the Linux/torch CI job does not
    need it to run the rest of the suite.
    """

    def _g2p(self):
        try:
            from misaki import zh
        except ImportError:
            self.skipTest('misaki[zh] not installed')
        return zh.ZHG2P()

    def test_traditional_gets_the_same_phonemes_as_simplified(self):
        g2p = self._g2p()
        # 乾乾淨淨 is read qián qián without the conversion, because 乾 merges 干 and 乾.
        self.assertNotEqual(g2p(TRADITIONAL)[0], g2p(SIMPLIFIED)[0])
        self.assertEqual(g2p(chinese.normalize(TRADITIONAL, 'z'))[0], g2p(SIMPLIFIED)[0])

    def test_particle_is_read_neutral_tone(self):
        g2p = self._g2p()
        raw = g2p('她笑著說')[0]
        fixed = g2p(chinese.normalize('她笑著說', 'z'))[0]
        self.assertIn('ꭧu↘', raw)      # zhù -- wrong
        self.assertNotIn('ꭧu↘', fixed)  # zhe -- neutral tone, no tone mark
        self.assertEqual(fixed, g2p('她笑着说')[0])


class TestJiebaResourceLoaderRepair(unittest.TestCase):
    """The setuptools 81-83 window, which jieba's own ImportError guard misses.

    Simulated rather than measured against a real setuptools, so that the test says the
    same thing on every runner regardless of what version happens to be installed.
    """

    def _fake_pkg_resources(self, with_resource_stream):
        mod = types.ModuleType('pkg_resources')
        if with_resource_stream:
            mod.resource_stream = lambda *a: None
        return {'pkg_resources': mod}

    def test_no_pkg_resources_needs_no_repair(self):
        # setuptools >= 84: the module is gone, so jieba's own fallback already works.
        with mock.patch.dict(sys.modules, {'pkg_resources': None}):
            self.assertFalse(chinese.repair_jieba_resource_loader())

    def test_working_pkg_resources_needs_no_repair(self):
        # setuptools < 81: resource_stream is present, jieba's normal path works.
        with mock.patch.dict(sys.modules, self._fake_pkg_resources(True)):
            self.assertFalse(chinese.repair_jieba_resource_loader())

    def test_broken_pkg_resources_is_repaired(self):
        try:
            import jieba
        except ImportError:
            self.skipTest('jieba not installed')
        original = jieba.get_module_res
        try:
            with mock.patch.dict(sys.modules, self._fake_pkg_resources(False)):
                self.assertTrue(chinese.repair_jieba_resource_loader())
            self.assertIsNot(jieba.get_module_res, original)
            # The replacement must actually open jieba's dictionary, not merely exist.
            with jieba.get_module_res('dict.txt') as handle:
                self.assertTrue(handle.read(1))
        finally:
            jieba.get_module_res = original


if __name__ == '__main__':
    unittest.main()
