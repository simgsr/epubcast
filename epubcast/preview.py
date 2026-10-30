# -*- coding: utf-8 -*-
"""Voice preview: choose a short script for the selected voice and play it back.

Kept out of ui.py so the parts worth testing -- which script a voice should be given, and
how a .wav gets played on this machine -- can be checked without wxPython or a display.
"""
import os
import shutil
import subprocess
import sys

# The two built-in samples. Long enough to judge tone and pacing, short enough that a
# preview stays a second or two of audio.
SAMPLE_EN = ('The harbour was quiet that evening, and the last of the light had gone '
             'behind the hills.')
SAMPLE_ZH = '傍晚的港口很安静，最后一抹天色已经落到山后面去了。'

# Offered in the UI in this order. Auto is the default and picks whatever the voice can
# actually read.
MODE_AUTO = 'Auto'
MODE_ENGLISH = 'English'
MODE_CHINESE = 'Chinese'
MODE_CHAPTER = 'Current chapter'
SAMPLE_MODES = (MODE_AUTO, MODE_ENGLISH, MODE_CHINESE, MODE_CHAPTER)

# Silence between the two languages of a bilingual preview, in seconds.
SAMPLE_GAP_SECONDS = 0.35

# A preview is not the place for a whole chapter, so chapter mode reads only its opening.
MAX_SAMPLE_CHARS = 220


def speaks_english(lang_code):
    """True for Kokoro's English codes ('a', 'b') and for English Edge locales."""
    return lang_code in ('a', 'b') or (lang_code or '').startswith('en')


def speaks_chinese(lang_code):
    """True for Kokoro's Chinese code ('z'), plus zh-* locales and Cantonese (yue-*)."""
    code = lang_code or ''
    return code == 'z' or code.startswith('zh') or code.startswith('yue')


def is_multilingual(voice):
    """Edge's multilingual voices are the only ones that can read both scripts well.

    The voice dropdown offers these by name, so the capability is readable straight off it
    -- there is no separate catalogue of what a voice can pronounce.
    """
    return 'Multilingual' in (voice or '')


def clip_text(text, limit=MAX_SAMPLE_CHARS):
    """The opening of `text`, cut back to a sentence end when one is close enough."""
    text = ' '.join((text or '').split())
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind('.'), window.rfind('。'), window.rfind('!'), window.rfind('！'),
              window.rfind('?'), window.rfind('？'))
    # Only honour the sentence end if it does not throw away most of the window.
    return window[:cut + 1] if cut > limit // 3 else window


def sample_segments(voice, lang_code, mode=MODE_AUTO, chapter_text=''):
    """The [(label, text)] segments a preview should speak for this voice.

    'Auto' stays inside what the voice can really pronounce, because the alternative sounds
    broken rather than merely accented: an English Kokoro voice handed Chinese characters
    mangles them through an English G2P, which is worse than no preview at all.
    """
    if mode == MODE_ENGLISH:
        return [('English', SAMPLE_EN)]
    if mode == MODE_CHINESE:
        return [('Chinese', SAMPLE_ZH)]
    if mode == MODE_CHAPTER:
        opening = clip_text(chapter_text)
        if opening:
            return [('Chapter', opening)]
        # Nothing is open, so fall through to the sensible default rather than refusing.

    if is_multilingual(voice):
        # The one case where both scripts belong in one preview: it is the whole reason to
        # pick a multilingual voice.
        return [('English', SAMPLE_EN), ('Chinese', SAMPLE_ZH)]
    if speaks_chinese(lang_code):
        return [('Chinese', SAMPLE_ZH)]
    if speaks_english(lang_code):
        return [('English', SAMPLE_EN)]
    # Any other language: only the open chapter is an honest sample of it.
    opening = clip_text(chapter_text)
    if opening:
        return [('Chapter', opening)]
    return [('English', SAMPLE_EN)]


def player_command(path, platform_name=None, which=None):
    """A command that plays `path`, or None when nothing here can.

    macOS gets `afplay`, which ships with the OS and decodes the .wav directly. Everything
    else falls back to ffplay -- part of ffmpeg, which the real run already requires -- and
    then to the platform's own player. Returning None instead of guessing lets the caller
    explain the situation rather than surfacing a confusing traceback.
    """
    platform_name = platform_name or sys.platform
    which = which or shutil.which
    if platform_name == 'darwin' and which('afplay'):
        return ['afplay', str(path)]
    if which('ffplay'):
        return ['ffplay', '-autoexit', '-nodisp', '-loglevel', 'quiet', str(path)]
    if platform_name.startswith('linux'):
        for candidate in ('paplay', 'aplay'):
            if which(candidate):
                return [candidate, str(path)]
        if which('xdg-open'):
            return ['xdg-open', str(path)]
    return None


def play(path, platform_name=None, which=None):
    """Start playing `path` without blocking. Returns the process, or None if it could not."""
    platform_name = platform_name or sys.platform
    if platform_name == 'win32':
        try:
            os.startfile(str(path))
        except OSError:
            return None
        return None
    command = player_command(path, platform_name, which)
    if command is None:
        return None
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_preview_wav(audio):
    """Write preview audio to a temporary .wav and return its path.

    The player needs a file on disk, and this is the one place that knows how the project
    stores audio, so callers do not have to reach for core.sample_rate themselves.
    """
    import soundfile
    from tempfile import NamedTemporaryFile

    from epubcast.core import sample_rate

    with NamedTemporaryFile(suffix='.wav', delete=False) as handle:
        soundfile.write(handle, audio, sample_rate)
        return handle.name


def render_preview(voice, lang_code, backend, speed, segments):
    """Synthesize `segments` into one float32 array, with a short beat between them.

    Runs through the same pipeline and text normalization as the real synthesis, so a
    preview predicts the audiobook rather than approximating it.
    """
    import numpy as np

    import epubcast.core as core
    from epubcast.backends import get_pipeline

    core.set_espeak_library()
    pipeline = get_pipeline(voice, lang_code=lang_code, backend=backend)
    core.load_spacy()
    gap = np.zeros(int(core.sample_rate * SAMPLE_GAP_SECONDS), dtype=np.float32)
    parts = []
    for index, (_label, text) in enumerate(segments):
        if index:
            parts.append(gap)
        parts.append(np.concatenate(core.gen_audio_segments(
            pipeline, text, voice=voice, speed=speed, lang_code=lang_code)))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)
