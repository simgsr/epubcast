# -*- coding: utf-8 -*-
"""Traditional Chinese support, by way of the simplified script.

There is no such thing as a "traditional Chinese voice" to add. A voice is a speaker's
timbre; the script a book is printed in is a property of the *text*, and it is handled
before the model ever sees it, in the grapheme-to-phoneme front end. Kokoro's Chinese front
end (misaki[zh]) is jieba for word segmentation plus pypinyin for readings, and both of
those dictionaries are keyed on simplified characters. Feed them traditional text and they
mostly limp along character by character, which costs exactly the two things the phrase
dictionaries were there to provide:

- **Polyphone disambiguation.** 乾乾淨淨 is read `qián qián jìng jìng` rather than
  `gān gān jìng jìng`, because 乾 collapses two different simplified characters (干 and 乾)
  and the phrase that would settle which one is meant is not in the dictionary. The worst
  offender is the aspect particle 著 (她笑著說), read `zhù` rather than the neutral-tone
  `zhe` -- and that one turns up in nearly every paragraph of a novel.
- **Word segmentation.** 臺灣的 segments as 臺 / 灣的 and 個人罷了 as 個 / 人罷 / 了, so the
  tone sandhi and the pauses land in the wrong places.

Converting to simplified before phonemization fixes both, and costs nothing in fidelity:
Mandarin pronunciation does not depend on which script the sentence was written in. The
audio you get is what a Mandarin reader would say when reading the traditional page aloud.

The conversion applies to the narration path only. Titles, chapter marks and everything
else written into the .m4b keep the book's original characters.

The Edge backend changes the picture for two locales: zh-TW and zh-HK voices read
traditional script natively, so for them the conversion is skipped entirely (see
wants_simplification). zh-CN behaves like Kokoro's 'z'.
"""
import os
import re
import warnings

# OpenCC's own config name. tw2s over the plainer t2s because of one very common word: the
# aspect particle 著 (她笑著說). t2s leaves it alone -- 著 is a simplified character in its own
# right, read zhù -- so pypinyin says "xiào zhù shuō" in every progressive sentence in the
# book. tw2s maps the particle to 着 and still leaves 著作 and 著名 as they are.
#
# Deliberately *not* tw2sp, which additionally rewrites regional vocabulary
# (軟體 -> 软件, 計程車 -> 出租车). That changes the author's words, not just their script.
OPENCC_CONFIG = 'tw2s'

# Kokoro's language letter for Chinese, and the Edge locale for mainland Mandarin. Both
# front ends are simplified-keyed, so traditional text is converted before them.
CHINESE_LANG_CODES = frozenset({'z', 'zh-cn'})

# Edge locales whose voices read traditional script natively. Converting would be
# pointless: a Taiwan or Hong Kong voice already knows 臺灣 and 著.
TRADITIONAL_NATIVE_LOCALES = frozenset({'zh-tw', 'zh-hk'})

# Codes that name no language at all. A caller that passes none at all arrives here, and
# the text has to speak for itself.
UNKNOWN_LANG_CODES = frozenset({''})

_HAN_RE = re.compile(r'[一-鿿㐀-䶿]')
# Kana and hangul mark a document as Japanese or Korean. Both scripts embed Han characters
# whose traditional/simplified mapping is a different question -- Japanese shinjitai are not
# Chinese simplified characters -- so t2s must not be let loose on them.
_KANA_RE = re.compile(r'[぀-ヿ]')
_HANGUL_RE = re.compile(r'[ᄀ-ᇿ가-힯]')

_converter = None
_converter_failed = False
_announced = False


def repair_jieba_resource_loader():
    """Let jieba load its dictionary on setuptools 81-83. Returns True if it patched.

    jieba reads its dictionary through `pkg_resources.resource_stream`, behind a bare
    `except ImportError`. setuptools 81 removed `resource_stream` while keeping the
    `pkg_resources` module, so the import succeeds and the *call* raises AttributeError --
    which that guard does not catch, and jieba dies on the first word it segments. Since
    misaki[zh] segments with jieba, that takes every Chinese book down with it.

    The window is narrow in both directions, which is what makes this easy to miss:
    setuptools < 81 still has the function, and >= 84 drops `pkg_resources` altogether so
    the guard fires and jieba's own file-open fallback works. jieba's last release was
    2020, so there is no fixed version to require.

    Worse, jieba caches its parsed dictionary in a temp file and only calls the loader on a
    cache miss, so a developer machine with a warm `jieba.cache` passes while a fresh CI
    runner fails on the same code -- which is exactly how this reached CI unnoticed.

    Called on import, because jieba initializes lazily on first use and misaki reaches it
    without passing back through this module.
    """
    try:
        import pkg_resources
    except ImportError:
        return False                                # >= 84: jieba's own fallback works
    if hasattr(pkg_resources, 'resource_stream'):
        return False                                # < 81: jieba's normal path works
    try:
        import jieba
        from jieba import _compat
    except ImportError:
        return False                                # no misaki[zh]; nothing to repair

    def open_module_res(*res):
        # The file jieba's own ImportError fallback would have opened.
        return open(os.path.join(os.path.dirname(_compat.__file__), *res), 'rb')

    # jieba does `from ._compat import *`, then calls get_module_res from its own
    # namespace, so that binding is the one that has to change; _compat is patched too so
    # anything reading it there sees the same thing.
    _compat.get_module_res = open_module_res
    jieba.get_module_res = open_module_res
    return True


repair_jieba_resource_loader()


def _get_converter():
    """The cached OpenCC converter, or None if the optional dependency is missing."""
    global _converter, _converter_failed
    if _converter is not None or _converter_failed:
        return _converter
    try:
        from opencc import OpenCC
    except ImportError:
        _converter_failed = True
        warnings.warn(
            'opencc is not installed, so traditional Chinese will be read with simplified-only '
            'dictionaries and some characters will get the wrong reading. '
            'Install it with: pip install opencc-python-reimplemented',
            stacklevel=3)
        return None
    _converter = OpenCC(OPENCC_CONFIG)
    return _converter


def to_simplified(text):
    """Convert traditional characters to simplified. A no-op if opencc is unavailable."""
    converter = _get_converter()
    if converter is None:
        return text
    return converter.convert(text)


def looks_chinese(text):
    """True for text that is Han script and not Japanese or Korean."""
    return bool(_HAN_RE.search(text)) and not _KANA_RE.search(text) and not _HANGUL_RE.search(text)


def wants_simplification(lang_code, text):
    """Whether `text` should be converted before being phonemized as `lang_code`.

    An explicit Chinese language code is enough on its own -- except for the Edge locales
    whose voices read traditional script natively (zh-TW, zh-HK), which never convert.
    When the caller named no language at all the text has to look Chinese, so that a
    Japanese or Korean book is left alone.
    """
    code = str(lang_code or '').lower()
    if code in CHINESE_LANG_CODES:
        return True
    if code in TRADITIONAL_NATIVE_LOCALES:
        return False
    if code in UNKNOWN_LANG_CODES:
        return looks_chinese(text)
    return False


def normalize(text, lang_code=None, notify=None):
    """Return `text` ready for a Mandarin front end, converting the script if needed.

    `notify` is called at most once per process with a one-line explanation, the first time
    a conversion actually changes something -- a book is dozens of chapters and this is one
    fact about the whole book, not news each chapter.
    """
    global _announced
    if not wants_simplification(lang_code, text):
        return text
    converted = to_simplified(text)
    if converted != text and not _announced:
        _announced = True
        if notify:
            notify('Traditional Chinese detected: converting to simplified characters for '
                   'pronunciation. The audiobook is narrated in Mandarin; the text written into '
                   'the .m4b is untouched.')
    return converted


def reset_notice():
    """Forget that the conversion notice was shown. For tests, and for a fresh GUI run."""
    global _announced
    _announced = False
