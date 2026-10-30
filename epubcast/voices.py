# -*- coding: utf-8 -*-
import platform
import re

flags = {'a': '🇺🇸', 'b': '🇬🇧', 'e': '🇪🇸', 'f': '🇫🇷', 'h': '🇮🇳', 'i': '🇮🇹', 'j': '🇯🇵', 'p': '🇧🇷', 'z': '🇨🇳'}

flags_win = {'a': 'american', 'b': 'british', 'e': 'spanish', 'f': 'french', 'h': 'hindi', 'i': 'italian',
             'j': 'japanese', 'p': 'portuguese', 'z': 'chinese'}

voices = {
    'a': ['af_alloy', 'af_aoede', 'af_bella', 'af_heart', 'af_jessica', 'af_kore', 'af_nicole', 'af_nova',
          'af_river', 'af_sarah', 'af_sky', 'am_adam', 'am_echo', 'am_eric', 'am_fenrir', 'am_liam',
          'am_michael', 'am_onyx', 'am_puck', 'am_santa'],
    'b': ['bf_alice', 'bf_emma', 'bf_isabella', 'bf_lily', 'bm_daniel', 'bm_fable', 'bm_george', 'bm_lewis'],
    'e': ['ef_dora', 'em_alex', 'em_santa'],
    'f': ['ff_siwis'],
    'h': ['hf_alpha', 'hf_beta', 'hm_omega', 'hm_psi'],
    'i': ['if_sara', 'im_nicola'],
    'j': ['jf_alpha', 'jf_gongitsune', 'jf_nezumi', 'jf_tebukuro', 'jm_kumo'],
    'p': ['pf_dora', 'pm_alex', 'pm_santa'],
    'z': ['zf_xiaobei', 'zf_xiaoni', 'zf_xiaoxiao', 'zf_xiaoyi', 'zm_yunjian', 'zm_yunxi', 'zm_yunxia',
          'zm_yunyang']
}

# Microsoft Edge's online TTS voices, keyed by locale. A curated subset of the ~400-voice
# catalog: the languages Kokoro already supports plus the Chinese variants that matter
# (zh-TW reads traditional script natively, zh-HK is real Cantonese). Voices outside this
# list are still usable by typing the name -- the dropdown stays editable.
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

edge_flags = {
    'en-US': '🇺🇸', 'en-GB': '🇬🇧', 'en-AU': '🇦🇺', 'en-CA': '🇨🇦', 'en-IN': '🇮🇳',
    'zh-CN': '🇨🇳', 'zh-TW': '🇹🇼', 'zh-HK': '🇭🇰',
    'es-ES': '🇪🇸', 'es-MX': '🇲🇽', 'fr-FR': '🇫🇷', 'de-DE': '🇩🇪', 'it-IT': '🇮🇹',
    'ja-JP': '🇯🇵', 'pt-BR': '🇧🇷', 'hi-IN': '🇮🇳',
}

# Edge voice names carry their locale: 'zh-TW-HsiaoChenNeural'. Kokoro names ('af_sky'),
# blends ('af_heart,af_bella') and .pt paths never match.
# Deliberately the same pattern edge-tts validates against (edge_tts/data_classes.py):
# subtags are not always two letters -- 'yue-CN-XiaoMinNeural' and 'fil-PH-AngeloNeural'
# are real voices, and a stricter {2} rejected them before the request was ever made.
_EDGE_VOICE_RE = re.compile(r'^[a-z]{2,}-[A-Z]{2,}-.+Neural$')

# Ticked by default in the GUI's language filter. Ticking every language put all 9 Kokoro
# languages -- or all 16 Edge locales, ~50 voices -- into one dropdown, burying the handful
# anyone actually narrates in. The rest stay one click away.
DEFAULT_LANGUAGES = frozenset({'a', 'b', 'z'})   # Kokoro: american, british, chinese
# Edge defaults to every locale ticked: the voice dropdown is filtered to the multilingual
# voices anyway, so there is no reason to hide any locale by default.
DEFAULT_LOCALES = frozenset(edge_voices.keys())


def is_default_language(code):
    """True for the codes ticked on startup, in either naming scheme.

    Kokoro codes are single letters and Edge codes are 'en-US'-shaped, and the two sets
    cannot collide, so one predicate covers both: the caller does not need to know which
    backend is selected.
    """
    return code in DEFAULT_LANGUAGES or code in DEFAULT_LOCALES


def default_languages(codes):
    """The subset of `codes` ticked on startup, or all of them if none qualify.

    The fallback matters for a backend whose codes are named some third way: better to
    open with everything ticked than with an empty voice dropdown.
    """
    chosen = {c for c in codes if is_default_language(c)}
    return chosen or set(codes)


def is_edge_voice(voice):
    """True when `voice` names an Edge TTS voice rather than a Kokoro one."""
    return bool(voice) and bool(_EDGE_VOICE_RE.match(voice))


def is_catalog_voice(voice):
    """True when `voice` is one epubcast lists, rather than one the user typed in.

    A .pt path, a blend and an Edge voice outside the curated locales are all legal to
    type but appear in no list, so callers must not treat their absence as "gone".
    """
    if not voice:
        return False
    return (any(voice in names for names in voices.values())
            or any(voice in names for names in edge_voices.values()))


def lang_code_for(voice, lang_code=None):
    """The language code `voice` narrates in.

    An explicit code wins. Edge voices carry their locale in the name
    ('zh-TW-HsiaoChenNeural' -> 'zh-TW'); Kokoro voices use the first letter
    ('af_sky' -> 'a'). A .pt path has no language in the name, so callers pass
    --lang explicitly for those.
    """
    if lang_code:
        return lang_code
    if is_edge_voice(voice):
        return '-'.join(voice.split('-')[:2])
    return voice[0] if voice else ''


if platform.system() == 'Windows':
    available_voices_str = '\n'.join([f'  {flags_win[lang]}:\t{", ".join(voices[lang])}' for lang in voices])
    edge_voices_str = '\n'.join([f'  {locale}:\t{", ".join(edge_voices[locale])}' for locale in edge_voices])
else:
    available_voices_str = '\n'.join([f'  {flags[lang]}:\t{", ".join(voices[lang])}' for lang in voices])
    edge_voices_str = '\n'.join([f'  {edge_flags[locale]} {locale}:\t{", ".join(edge_voices[locale])}' for locale in edge_voices])
