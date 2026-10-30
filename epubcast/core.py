#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# epubcast - A program to convert e-books into audiobooks using
# Kokoro-82M model for high-quality text-to-speech synthesis.
# by Claudio Santini 2025 - https://claudio.uk
import concurrent.futures
import os
import threading
import traceback
from glob import glob

import spacy
import ebooklib
import soundfile
import numpy as np
import time
import shutil
import subprocess
import platform
import re
from types import SimpleNamespace
from tabulate import tabulate
from pathlib import Path
from string import Formatter
from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction
from ebooklib import epub
from pick import pick

from epubcast import DEFAULT_OUTPUT_FOLDER
from epubcast import chinese
from epubcast import power
from epubcast.backends import EdgeNoAudio, EdgePipeline, get_pipeline, initial_chars_per_sec, resolve_backend
from epubcast.voices import lang_code_for

sample_rate = 24000


def safe_filename_part(value):
    """Voices may be a .pt path or a comma-blended list; neither is safe in a filename."""
    return re.sub(r'[^\w.-]+', '_', str(value)).strip('_')


def load_spacy():
    if not spacy.util.is_package("xx_ent_wiki_sm"):
        print("Downloading Spacy model xx_ent_wiki_sm...")
        spacy.cli.download("xx_ent_wiki_sm")


def set_espeak_library():
    """Find the espeak library path"""
    try:

        if os.environ.get('ESPEAK_LIBRARY'):
            library = os.environ['ESPEAK_LIBRARY']
        elif platform.system() == 'Darwin':
            from subprocess import check_output
            try:
                cellar = Path(check_output(["brew", "--cellar"], text=True).strip())
                pattern = cellar / "espeak-ng" / "*" / "lib" / "*.dylib"
                if not (library := next(iter(glob(str(pattern))), None)):
                    raise RuntimeError("No espeak-ng library found; please set the path manually")
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                raise RuntimeError("Cannot locate Homebrew Cellar. Is 'brew' installed and in PATH?") from e
        elif platform.system() == 'Linux':
            library = glob('/usr/lib/*/libespeak-ng*')[0]
        elif platform.system() == 'Windows':
            library = 'C:\\Program Files*\\eSpeak NG\\libespeak-ng.dll'
        else:
            print('Unsupported OS, please set the espeak library path manually')
            return
        print('Using espeak library:', library)
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        EspeakWrapper.set_library(library)
    except Exception:
        traceback.print_exc()
        print("Error finding espeak-ng library:")
        print("Probably you haven't installed espeak-ng.")
        print("On Mac: brew install espeak-ng")
        print("On Linux: sudo apt install espeak-ng")


@power.keep_awake()  # a book takes hours; don't let an idle machine suspend halfway
def main(file_path, voice, pick_manually, speed, output_folder=DEFAULT_OUTPUT_FOLDER,
         max_chapters=None, max_sentences=None, selected_chapters=None, post_event=None,
         backend='auto', lang_code=None, repo_id=None):
    if post_event: post_event('CORE_STARTED')
    chinese.reset_notice()  # the GUI stays open across books; each run gets its own notice
    load_spacy()
    Path(output_folder).mkdir(parents=True, exist_ok=True)

    filename = Path(file_path).name

    extension = '.epub'
    book = epub.read_epub(file_path)
    meta_title = book.get_metadata('DC', 'title')
    title = meta_title[0][0] if meta_title else ''
    meta_creator = book.get_metadata('DC', 'creator')
    creator = meta_creator[0][0] if meta_creator else ''

    cover_maybe = find_cover(book)
    cover_image = cover_maybe.get_content() if cover_maybe else b""
    if cover_maybe:
        print(f'Found cover image {cover_maybe.file_name} in {cover_maybe.media_type} format')

    document_chapters = find_document_chapters_and_extract_texts(book)

    if not selected_chapters:
        if pick_manually is True:
            selected_chapters = pick_chapters(document_chapters)
        else:
            selected_chapters = find_good_chapters(document_chapters)
    print_selected_chapters(document_chapters, selected_chapters)
    texts = [c.extracted_text for c in selected_chapters]

    has_ffmpeg = shutil.which('ffmpeg') is not None
    if not has_ffmpeg:
        print('\033[91m' + 'ffmpeg not found. Please install ffmpeg to create mp3 and m4b audiobook files.' + '\033[0m')

    resolved_backend = resolve_backend(backend)
    stats = SimpleNamespace(
        total_chars=sum(map(len, texts)),
        processed_chars=0,
        # Only characters actually sent to the model, which is what the rate must be based on:
        # chapters skipped because their .wav already exists cost no time.
        synthesized_chars=0,
        chars_per_sec=initial_chars_per_sec(backend, lang_code_for(voice, lang_code)),
        start_time=time.time())
    print('Started at:', time.strftime('%H:%M:%S'))
    print(f'Total characters: {stats.total_chars:,}')
    print('Total words:', len(' '.join(texts).split()))
    eta = strfdelta((stats.total_chars - stats.processed_chars) / stats.chars_per_sec)
    print(f'Estimated time remaining (assuming {stats.chars_per_sec} chars/sec): {eta}')
    set_espeak_library()
    lang_code = lang_code_for(voice, lang_code)  # 'a' for american, 'zh-TW' for an Edge voice, etc.
    print(f'Using the {resolved_backend} backend.')
    pipeline = get_pipeline(voice, lang_code=lang_code, backend=backend, repo_id=repo_id)

    chapter_wav_files = []
    chapter_titles = {}
    voice_file_part = safe_filename_part(voice)
    for i, chapter in enumerate(selected_chapters, start=1):
        if max_chapters and i > max_chapters: break
        text = chapter.extracted_text
        xhtml_file_name = chapter.get_name().replace(' ', '_').replace('/', '_').replace('\\', '_')
        chapter_wav_path = Path(output_folder) / filename.replace(extension, f'_chapter_{i}_{voice_file_part}_{xhtml_file_name}.wav')
        chapter_wav_files.append(chapter_wav_path)
        chapter_titles[str(chapter_wav_path)] = getattr(chapter, 'title', '') or f'Chapter {i}'
        if Path(chapter_wav_path).exists():
            print(f'File for chapter {i} already exists. Skipping')
            stats.processed_chars += len(text)
            if post_event:
                post_event('CORE_CHAPTER_FINISHED', chapter_index=chapter.chapter_index)
            continue
        if len(text.strip()) < 10:
            print(f'Skipping empty chapter {i}')
            chapter_wav_files.remove(chapter_wav_path)
            continue
        if i == 1:
            # add intro text
            text = f'{title} – {creator}.\n\n' + text
        start_time = time.time()
        if post_event: post_event('CORE_CHAPTER_STARTED', chapter_index=chapter.chapter_index)
        try:
            audio_segments = gen_audio_segments(
                pipeline, text, voice, speed, stats, post_event=post_event, max_sentences=max_sentences,
                lang_code=lang_code)
        except EdgeNoAudio as e:
            # Write nothing for this chapter. A .wav on disk is what makes the next run
            # skip a chapter, so writing a truncated one here would bake the missing
            # speech in for good; leaving it absent lets a re-run try the chapter again.
            print(f'Warning: chapter {i} is incomplete and was not written: {e}')
            chapter_wav_files.remove(chapter_wav_path)
            continue
        if audio_segments:
            final_audio = np.concatenate(audio_segments)
            soundfile.write(chapter_wav_path, final_audio, sample_rate)
            end_time = time.time()
            delta_seconds = end_time - start_time
            chars_per_sec = len(text) / delta_seconds
            print('Chapter written to', chapter_wav_path)
            if post_event: post_event('CORE_CHAPTER_FINISHED', chapter_index=chapter.chapter_index)
            print(f'Chapter {i} read in {delta_seconds:.2f} seconds ({chars_per_sec:.0f} characters per second)')
        else:
            print(f'Warning: No audio generated for chapter {i}')
            chapter_wav_files.remove(chapter_wav_path)

    if has_ffmpeg:
        create_index_file(title, creator, chapter_wav_files, output_folder, chapter_titles)
        create_m4b(chapter_wav_files, filename, cover_image, output_folder)
        if post_event: post_event('CORE_FINISHED')


def find_cover(book):
    def is_image(item):
        return item is not None and item.media_type.startswith('image/')

    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        if is_image(item):
            return item

    # https://idpf.org/forum/topic-715
    for meta in book.get_metadata('OPF', 'cover'):
        if is_image(item := book.get_item_with_id(meta[1]['content'])):
            return item

    if is_image(item := book.get_item_with_id('cover')):
        return item

    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        if 'cover' in item.get_name().lower() and is_image(item):
            return item

    return None


def print_selected_chapters(document_chapters, chapters):
    ok = 'X' if platform.system() == 'Windows' else '✅'
    print(tabulate([
        [i, chapter_display_name(c), len(c.extracted_text), ok if c in chapters else '', chapter_beginning_one_liner(c)]
        for i, c in enumerate(document_chapters, start=1)
    ], headers=['#', 'Chapter', 'Text Length', 'Selected', 'First words']))

def split_long_sentence(text, max_length=400):
    """Split a long sentence around the 500 chars, picking the first whitespace after the 500th character. """
    if len(text) <= max_length:
        return [text]
    parts = []
    while len(text) > max_length:
        split_index = text.rfind(' ', 0, max_length)
        if split_index == -1:
            split_index = max_length
        parts.append(text[:split_index].strip())
        text = text[split_index:].strip()
    if text:
        parts.append(text)
    return parts


# Below these thresholds the measured rate is too noisy (model warm-up, first short sentence)
# to be worth trusting over the seed guess.
MIN_CHARS_TO_MEASURE = 2000
MIN_SECONDS_TO_MEASURE = 5


def measured_chars_per_sec(stats):
    """Real throughput so far, falling back to the seed guess until there is enough data.

    The old estimate was a fixed constant picked from whether CUDA was present, which on
    Apple Silicon meant every run advertised the 50 chars/sec CPU figure -- a 15 hour
    audiobook that actually took 87 minutes was announced as nearly five hours.

    Rate is measured on synthesized characters only. Resuming a run credits already-written
    chapters to processed_chars instantly, and dividing those by elapsed time would report a
    wildly inflated rate that the cumulative average would never recover from.
    """
    elapsed = time.time() - getattr(stats, 'start_time', time.time())
    synthesized = getattr(stats, 'synthesized_chars', stats.processed_chars)
    if synthesized < MIN_CHARS_TO_MEASURE or elapsed < MIN_SECONDS_TO_MEASURE:
        return stats.chars_per_sec
    return synthesized / elapsed


_sentencizer = None
# epubcast/ui.py can run gen_audio_segments from two threads at once -- the main
# book-synthesis thread and a chapter-preview thread -- and the sentencizer below is now a
# single instance shared across every call rather than one built fresh per call. spaCy's
# Language objects (this one includes a tok2vec/NER component, not just the rule-based
# sentencizer) are not documented as safe for concurrent nlp(text) calls, so both the lazy
# load and every use of the pipeline are serialized through this lock.
_sentencizer_lock = threading.Lock()


def _get_sentencizer():
    """The spaCy sentencizer, loaded once per process.

    gen_audio_segments used to load it fresh on every call, i.e. once per chapter --
    harmless for a handful of chapters but wasted load time repeated over a book with
    hundreds of them, for a component that carries no per-chapter state.
    """
    global _sentencizer
    if _sentencizer is None:
        # load_spacy() downloads the model if it is missing rather than assuming main()
        # or gen_text() already did: gen_audio_segments is called directly by tests (and
        # could be by other callers), and spacy.load() alone raises OSError E050 on a
        # machine that has never fetched xx_ent_wiki_sm.
        load_spacy()
        _sentencizer = spacy.load('xx_ent_wiki_sm')
        _sentencizer.add_pipe('sentencizer')
    return _sentencizer


# Edge pays a full network round trip -- websocket handshake plus streamed synthesis --
# for every pipeline call, so at one call per sentence a book with thousands of short
# sentences becomes thousands of serial round trips, most of that wall time spent waiting
# rather than synthesizing. Kokoro has no such per-call overhead worth amortizing, and
# batching would fight the manual long-sentence split it already needs (backends.py), so
# this only applies to pipelines that opt in via `batch_chars` (currently just Edge).
EDGE_BATCH_CHARS = EdgePipeline.batch_chars


def _batch_for_edge(sentences, max_chars=EDGE_BATCH_CHARS):
    """Group consecutive sentences into chunks up to `max_chars`, for one Edge call each.

    A sentence larger than `max_chars` still gets its own chunk rather than being split
    further -- Edge has no length limit worth working around, unlike Kokoro.
    """
    batches = []
    current = []
    current_len = 0
    for sent in sentences:
        if current and current_len + len(sent) > max_chars:
            batches.append(current)
            current = []
            current_len = 0
        current.append(sent)
        current_len += len(sent)
    if current:
        batches.append(current)
    return batches


def _record_progress(stats, chunk_text, post_event):
    stats.processed_chars += len(chunk_text)
    stats.synthesized_chars = getattr(stats, 'synthesized_chars', 0) + len(chunk_text)
    stats.progress = stats.processed_chars * 100 // stats.total_chars
    stats.chars_per_sec = measured_chars_per_sec(stats)
    stats.eta = strfdelta((stats.total_chars - stats.processed_chars) / stats.chars_per_sec)
    if post_event: post_event('CORE_PROGRESS', stats=stats)
    print(f'Estimated time remaining: {stats.eta}')
    print('Progress:', f'{stats.progress}%\n')


def gen_audio_segments(pipeline, text, voice, speed, stats=None, max_sentences=None, post_event=None,
                       lang_code=None):
    nlp = _get_sentencizer()
    audio_segments = []
    lang_code = lang_code_for(voice, lang_code)
    # Before sentence splitting, so the segmenter sees the same characters the model will.
    text = chinese.normalize(text, lang_code, notify=print)
    # Locked: this nlp is a single instance shared across every call (see _get_sentencizer),
    # and gen_audio_segments can run concurrently from more than one thread (epubcast/ui.py
    # runs book synthesis and chapter preview on separate threads), so calls into it are
    # serialized rather than racing on its internal state.
    with _sentencizer_lock:
        doc = nlp(text)

    # Tuple membership, not `in 'ab'`: the substring form also matched '' and 'ab', so a
    # missing language code silently took the English path and skipped the long-sentence
    # split that every other language depends on.
    if lang_code in ('a', 'b'):
        sentences = [s.text for s in doc.sents]
    else:
        # For non-english languages, Kokoro truncates long sentences, so we split them manually
        sentences = []
        for sent in list(doc.sents):
            if len(sent.text) > 400:
                print(f'Warning: Sentence too long ({len(sent.text)} chars), splitting into smaller sentences.')
                sents = split_long_sentence(sent.text, 400)
                sentences.extend(sents)
            else:
                sentences.append(sent.text)

    # Cap the sentence list itself, before batching, so the cap bounds how much text is
    # actually synthesized regardless of how the pipeline groups sentences into calls. A
    # post-hoc break on sent_count (the previous approach) is meaningless once a backend can
    # fold many sentences into one chunk: sent_count would still read 0 when the first,
    # possibly large, chunk gets synthesized. `+ 1` preserves the previous one-call-per-
    # sentence behavior of stopping just after the max_sentences'th sentence.
    if max_sentences is not None:
        sentences = sentences[:max_sentences + 1]

    # A pipeline opts into batching by exposing `batch_chars` (currently just EdgePipeline);
    # every other pipeline keeps the existing one-call-per-sentence behavior (singleton
    # "batches"). This amortizes Edge's per-call network round trip over several sentences.
    batch_chars = getattr(pipeline, 'batch_chars', None)
    if batch_chars:
        chunks = _batch_for_edge(sentences, max_chars=batch_chars)
    else:
        chunks = [[s] for s in sentences]

    def _synthesize_chunk(chunk):
        # Every sentence is stripped before joining, singleton chunks included, so a chunk's
        # text depends only on the sentences it contains, never on how batching grouped them.
        chunk_text = ' '.join(s.strip() for s in chunk)
        audios = [audio for _, _, audio in
                  pipeline(chunk_text, voice=voice, speed=speed, split_pattern=r'\n\n\n')]
        return chunk_text, audios

    # A pipeline opts into concurrent dispatch by exposing `max_concurrency` > 1 (currently
    # just EdgePipeline). Chunks are submitted to a thread pool up to that many at once, but
    # how many actually reach the network at the same time is governed by the pipeline's own
    # adaptive gate (EdgePipeline / _AdaptiveConcurrencyGate in backends.py): it starts fully
    # serial and only grows after a run of clean successes, dropping straight back to 1 on
    # any retry -- rather than firing every request back to back on purpose, which is exactly
    # the pattern Edge is documented to throttle. executor.map yields results in chunk order
    # even though the work underneath runs concurrently, so audio order and progress
    # reporting are unaffected by however many chunks actually overlapped.
    max_concurrency = getattr(pipeline, 'max_concurrency', 1)
    if max_concurrency > 1 and chunks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as pool:
            for chunk_text, audios in pool.map(_synthesize_chunk, chunks):
                audio_segments.extend(audios)
                if stats:
                    _record_progress(stats, chunk_text, post_event)
    else:
        for chunk in chunks:
            chunk_text, audios = _synthesize_chunk(chunk)
            audio_segments.extend(audios)
            if stats:
                _record_progress(stats, chunk_text, post_event)
    return audio_segments


def gen_text(text, voice='af_heart', output_file='text.wav', speed=1, play=False,
             backend='auto', lang_code=None, repo_id=None):
    # Not voice[:1]: that reads 'zh-TW-HsiaoChenNeural' as 'z', which sends a Taiwan voice
    # -- one that reads traditional script natively -- down the simplify-first path.
    lang_code = lang_code_for(voice, lang_code)
    set_espeak_library()
    pipeline = get_pipeline(voice, lang_code=lang_code, backend=backend, repo_id=repo_id)
    load_spacy()
    audio_segments = gen_audio_segments(pipeline, text, voice=voice, speed=speed, lang_code=lang_code)
    final_audio = np.concatenate(audio_segments)
    soundfile.write(output_file, final_audio, sample_rate)
    if play:
        subprocess.run(['ffplay', '-autoexit', '-nodisp', output_file])


# Tags that never hold narratable prose.
NON_CONTENT_TAGS = ['script', 'style', 'svg', 'nav', 'audio', 'video', 'head']

# epub:type / ARIA role values marking page markers, navigation and notes.
SKIP_EPUB_TYPES = {
    'pagebreak', 'page-list', 'noteref', 'note', 'footnote', 'footnotes', 'endnote', 'endnotes',
    'rearnote', 'rearnotes', 'annoref', 'toc', 'landmarks', 'index', 'glossary', 'colophon',
}
SKIP_ROLES = {
    'doc-pagebreak', 'doc-noteref', 'doc-footnote', 'doc-endnote', 'doc-endnotes',
    'doc-index', 'doc-toc', 'doc-glossary', 'navigation',
}

# class/id values publishers use for the little page numbers sprinkled through the text.
PAGE_MARKER_RE = re.compile(r'^(?:page[-_]?(?:num(?:ber)?s?|break)?|pagenum|pageno|folio)[-_]?\d*$', re.I)
# A page number is a couple of digits; anything longer under such a class is real text.
MAX_PAGE_MARKER_LENGTH = 20

# Note markers are usually a bare digit or a typographic dagger inside a <sup>.
NOTE_MARKER_RE = re.compile(r'^[\d\s.,;*†‡§¶\[\]()–—-]*$')

# Block-level tags: text on either side of them belongs to separate utterances.
BLOCK_TAGS = {
    'address', 'article', 'aside', 'blockquote', 'br', 'caption', 'center', 'dd', 'div', 'dl', 'dt',
    'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr',
    'li', 'main', 'ol', 'p', 'pre', 'section', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul',
}

# A line already ending in one of these does not need a full stop appended.
SENTENCE_TERMINALS = '.!?;:…⋯。！？；：'
# ...possibly followed by a closing quote or bracket.
CLOSING_MARKS = '”’"\'»›」』）)]}】》〉'

# A section shorter than this is a stub (a lone heading, or a body that was all navigation).
MIN_SECTION_LENGTH = 200
# Below this a single-document book is short enough to narrate as one chapter.
MIN_LENGTH_TO_SPLIT = 60_000

# Table-of-contents titles that mark front/back matter rather than a chapter to narrate.
FRONT_MATTER_TITLES = {
    'content', 'contents', 'table of contents', 'toc', 'index', 'copyright', 'copyright page', 'colophon',
    'acknowledgements', 'acknowledgments', 'bibliography', 'references', 'notes', 'endnotes', 'footnotes',
    'glossary', 'about the author', 'about the publisher', 'cover', 'title page', 'half title',
    '目录', '目錄', '版权', '版權', '版权页', '版權頁', '索引', '注释', '註釋', '附录', '附錄',
    '参考文献', '參考文獻', '封面', '扉页', '扉頁', '书名页', '書名頁',
}


def _is_internal_link(tag):
    href = tag.get('href') or ''
    return href.startswith('#') or '#' in href


def _drop_non_content_elements(soup):
    """Remove page numbers, note markers/bodies and navigation links, in place."""
    for tag in soup.find_all(NON_CONTENT_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        # epub:type is namespaced; bs4's lxml parser flattens it to 'epub:type'.
        epub_types = (tag.get('epub:type') or '').lower().split()
        roles = (tag.get('role') or '').lower().split()
        if SKIP_EPUB_TYPES.intersection(epub_types) or SKIP_ROLES.intersection(roles):
            tag.decompose()
            continue
        names = list(tag.get('class') or [])
        if tag.get('id'):
            names.append(tag.get('id'))
        # Guard on length: a <div class="page"> wrapping a whole page is a container, not a marker.
        if (any(PAGE_MARKER_RE.match(n) for n in names)
                and len(tag.get_text(strip=True)) <= MAX_PAGE_MARKER_LENGTH):
            tag.decompose()

    # Superscript note markers: <sup><a href="#fn12">12</a></sup> or a bare <sup>†</sup>.
    for tag in soup.find_all(['sup', 'sub']):
        if tag.decomposed:
            continue
        if tag.find('a') and any(_is_internal_link(a) for a in tag.find_all('a')):
            tag.decompose()
            continue
        # Subscript digits are chemistry ("H2O"), not note markers, so only <sup> is pruned.
        if tag.name == 'sup' and NOTE_MARKER_RE.match(tag.get_text(strip=True)):
            # A superscript right after a digit is an exponent ("10^6"); after a word it is a note.
            previous = tag.find_previous(string=True)
            if not (previous and previous.strip()[-1:].isdigit()):
                tag.decompose()

    return soup


def _ends_sentence(line):
    probe = line.rstrip(CLOSING_MARKS)
    return bool(probe) and probe[-1] in SENTENCE_TERMINALS


def _terminator_for(line):
    """Use a CJK full stop after CJK text so the voice does not read a stray Latin dot."""
    return '。' if any(_is_cjk(char) for char in line) else '.'


def _is_cjk(char):
    return ('㐀' <= char <= '鿿' or '豈' <= char <= '﫿'
            or '぀' <= char <= 'ヿ')


def _anchor_at(tag, anchor_ids):
    """The table-of-contents anchor this tag carries, if any."""
    for attribute in ('id', 'name'):
        value = tag.get(attribute)
        if value and value in anchor_ids:
            return value
    return None


def _extract_lines(node, anchor_ids=frozenset()):
    """Walk the tree once, emitting one line per block element, grouped into anchor sections.

    Walking (rather than find_all over a tag whitelist) means text is never emitted twice for
    nested blocks such as <li><p>…</p></li>, and text in tags outside the whitelist -- bare
    <div>s, table cells, <br>-separated prose -- is no longer silently dropped.

    Returns a list of (anchor_id, lines) pairs. Without anchor_ids that is a single
    (None, lines) pair; with them, the document is cut wherever a listed anchor appears.
    """
    sections = [(None, [])]
    buffer = []  # (text, came_from_an_internal_link)

    def flush():
        if not buffer:
            return
        line = re.sub(r'\s+', ' ', ''.join(text for text, _ in buffer)).strip()
        link_only = re.sub(r'\s+', ' ', ''.join(text for text, is_link in buffer if is_link)).strip()
        buffer.clear()
        # A line made up entirely of internal links is navigation: an inline table of
        # contents, a note backlink. Cross-references inside prose keep their surrounding text.
        if line and line != link_only:
            sections[-1][1].append(line)

    def start_section(anchor):
        flush()
        if sections[-1][0] is None and not sections[-1][1]:
            sections[-1] = (anchor, sections[-1][1])  # nothing preceded the first anchor
        else:
            sections.append((anchor, []))

    def walk(element, in_link=False):
        for child in element.children:
            if isinstance(child, NavigableString):
                if isinstance(child, (Comment, Doctype, ProcessingInstruction, Declaration, CData)):
                    continue
                buffer.append((str(child), in_link))
                continue
            anchor = _anchor_at(child, anchor_ids)
            if anchor:
                start_section(anchor)
            if child.name in BLOCK_TAGS:
                flush()
                walk(child, in_link)
                flush()
            else:
                walk(child, in_link or (child.name == 'a' and _is_internal_link(child)))

    walk(node)
    flush()
    return sections


def _lines_to_text(lines):
    return ''.join(
        line + ('' if _ends_sentence(line) else _terminator_for(line)) + '\n'
        for line in lines
    )


def extract_sections_from_html(xml, anchor_ids=()):
    """Extract narratable text, cut into (anchor_id, text) sections at the given anchors."""
    soup = BeautifulSoup(xml, features='lxml')
    _drop_non_content_elements(soup)
    return [(anchor, _lines_to_text(lines)) for anchor, lines in _extract_lines(soup, frozenset(anchor_ids))]


def extract_text_from_html(xml):
    """Extract narratable text from a chapter's body, dropping page numbers, notes and navigation."""
    return ''.join(text for _, text in extract_sections_from_html(xml))


def toc_entries_by_file(book):
    """Map each document href to the [(anchor, title)] the table of contents gives it.

    ebooklib rewrites documents on read and drops <head>, so the document's own <title> is
    gone by the time we see it; the ncx/nav table of contents is what survives.
    """
    entries_by_file = {}

    def visit(entries):
        for entry in entries:
            if isinstance(entry, (tuple, list)):
                visit(entry)
                continue
            href, title = getattr(entry, 'href', None), getattr(entry, 'title', None)
            if not href or not title:
                continue
            file_name, _, anchor = href.partition('#')
            entries_by_file.setdefault(file_name, []).append((anchor, title))

    try:
        visit(book.toc)
    except Exception:
        pass
    return entries_by_file


def toc_entries_for(chapter_name, entries_by_file):
    if chapter_name in entries_by_file:
        return entries_by_file[chapter_name]
    basename = chapter_name.rsplit('/', 1)[-1]
    for href, entries in entries_by_file.items():
        if href.rsplit('/', 1)[-1] == basename:
            return entries
    return []


def looks_like_front_matter(title):
    return title.strip().lower().strip('《》「」『』()[]:：.、 ') in FRONT_MATTER_TITLES


def is_front_matter(chapter_name, entries_by_file):
    """True for documents the table of contents names as contents/index/copyright/notes."""
    entries = toc_entries_for(chapter_name, entries_by_file)
    # Several entries pointing at one file means it holds the whole book, not one front section.
    if len(entries) != 1:
        return False
    return looks_like_front_matter(entries[0][1])


class SplitChapter:
    """One section of a single-file book, standing in for an epub document.

    Books that ship as a single huge xhtml file would otherwise become one giant chapter with
    no chapter marks, so they are cut at the anchors their table of contents points to.
    """

    def __init__(self, source, anchor, title, extracted_text):
        self.source = source
        self.anchor = anchor
        self.title = title
        self.extracted_text = extracted_text
        self.is_front_matter = looks_like_front_matter(title)

    def get_name(self):
        return f'{self.source.get_name()}#{self.anchor}' if self.anchor else self.source.get_name()

    def get_type(self):
        return ebooklib.ITEM_DOCUMENT

    def get_id(self):
        return self.get_name()


def split_on_toc_anchors(chapter, entries, sections):
    """Turn the extracted (anchor, text) sections of one document into SplitChapters.

    Books commonly put a bare "Chapter 4" divider at its own anchor, just before the anchor
    holding the actual text. Such a stub is merged into the section that follows -- keeping its
    words and its more descriptive title -- rather than becoming a two-second chapter of its own.
    """
    titles = dict(entries)
    split_chapters = []
    pending_text, pending_anchor = '', None

    for anchor, text in sections:
        if not text.strip():
            continue  # an inline table of contents: nothing left once navigation is dropped
        combined = pending_text + text
        if len(combined.strip()) < MIN_SECTION_LENGTH:
            if pending_anchor is None:
                pending_anchor = anchor
            pending_text = combined
            continue
        anchor = pending_anchor or anchor
        title = titles.get(anchor) or chapter_display_name(chapter)
        split_chapters.append(SplitChapter(chapter, anchor, title, combined))
        pending_text, pending_anchor = '', None

    if pending_text.strip() and split_chapters:
        split_chapters[-1].extracted_text += pending_text  # a trailing stub
    elif pending_text.strip():
        title = titles.get(pending_anchor) or chapter_display_name(chapter)
        split_chapters.append(SplitChapter(chapter, pending_anchor, title, pending_text))
    return split_chapters


def chapter_display_name(chapter):
    return getattr(chapter, 'title', '') or chapter.get_name()


def spine_documents(book):
    """Every ITEM_DOCUMENT in reading order.

    book.get_items() yields the manifest, whose order is arbitrary; the spine is what the
    reader (and therefore the table of contents) follows. Anything the spine omits is kept,
    in manifest order, after the documents it does list.
    """
    documents = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_DOCUMENT]
    spine = getattr(book, 'spine', None) or []
    if not spine:
        return documents

    by_id = {}
    for document in documents:
        by_id.setdefault(document.get_id(), document)

    ordered, seen = [], set()
    for entry in spine:
        idref = entry[0] if isinstance(entry, (tuple, list)) else entry  # ('idref', 'yes'), or a bare idref
        if not isinstance(idref, str):
            idref = getattr(idref, 'id', '')  # or the item itself, when the book was built in memory
        document = by_id.get(idref)
        if document is not None and id(document) not in seen:
            ordered.append(document)
            seen.add(id(document))
    ordered.extend(d for d in documents if id(d) not in seen)
    return ordered


def read_documents(book, entries_by_file):
    """Read every document once, cut into (anchor, text) sections at its own table-of-contents anchors."""
    read = []
    for document in spine_documents(book):
        entries = toc_entries_for(document.get_name(), entries_by_file)
        anchors = [anchor for anchor, _ in entries if anchor]
        sections = extract_sections_from_html(document.get_body_content(), anchors)
        read.append((document, entries, sections))
    return read


def toc_driven_chapters(read):
    """Cut the book where its table of contents says the chapters are.

    A chapter starts at every entry in the contents, and runs until the next one -- across as
    many documents as that takes. Publishers routinely spill one chapter over a dozen files
    (index_split_006.html holding nothing but the heading, _008 to _018 the text), so a
    document the contents never names is a continuation of the chapter before it, not a
    chapter of its own. The same walk splits the opposite kind of book, where the contents
    points at several anchors inside one big document.

    Returns [] when the contents is missing or too thin to segment the book, leaving the
    caller to fall back to one chapter per document.
    """
    if sum(len(entries) for _, entries, _ in read) < 2:
        return []  # no contents, or one that names a single place: nothing to segment by
    named = {i for i, (_, entries, _) in enumerate(read) if entries}
    # Does this book spill chapters over several files at all? If every entry in the contents
    # is followed straight away by the next one, it keeps one file per chapter, and a document
    # trailing the last entry is back matter the contents forgot -- an index or a notes
    # section -- which must not be glued onto the end of the final chapter. If instead the
    # book already runs chapters across unnamed files, a trailing unnamed file is just more
    # of the last chapter.
    spills_over_files = any(i not in named for i in range(min(named), max(named)))
    last_named = len(read) if spills_over_files else max(named)

    chapters = []
    for index, (document, entries, sections) in enumerate(read):
        titles = dict(entries)
        for anchor, text in sections:
            if not text.strip():
                continue  # an inline table of contents: nothing left once navigation is dropped
            title = titles.get(anchor or '')
            if title is None:
                if chapters and index <= last_named:
                    chapters[-1].extracted_text += text
                    continue
                title = chapter_display_name(document)
            elif (chapters and chapters[-1].source is document
                  and len(chapters[-1].extracted_text.strip()) < MIN_SECTION_LENGTH):
                # A bare "Chapter 4" divider at its own anchor, just before the anchor holding
                # the text: merge, keeping the divider's words and title, rather than making a
                # two-second chapter of it. Only within one document -- across documents a
                # short chapter (a dedication, say) is a real chapter.
                chapters[-1].extracted_text += text
                continue
            chapters.append(SplitChapter(document, anchor, title, text))
    return chapters if len(chapters) > 1 else []


def per_document_chapters(read, entries_by_file):
    """One chapter per document: the fallback for books without a usable table of contents.

    A book delivered as one big document is still split into a chapter per contents anchor,
    since leaving it whole would produce a single unusable chapter.
    """
    substantial = sum(1 for _, _, sections in read
                      if sum(len(text.strip()) for _, text in sections) > MIN_SECTION_LENGTH)

    document_chapters = []
    for chapter, entries, sections in read:
        full_text = ''.join(text for _, text in sections)
        should_split = (len(sections) > 1
                        and (substantial <= 1 or len(full_text) > MIN_LENGTH_TO_SPLIT))
        if should_split:
            split_chapters = split_on_toc_anchors(chapter, entries, sections)
            if len(split_chapters) > 1:
                print(f'Splitting {chapter.get_name()} into {len(split_chapters)} chapters '
                      f'on its table of contents anchors.')
                document_chapters.extend(split_chapters)
                continue
        chapter.extracted_text = full_text
        chapter.is_front_matter = is_front_matter(chapter.get_name(), entries_by_file)
        # Name the chapter after its table-of-contents entry. ebooklib leaves .title empty,
        # so without this the UI and the m4b chapter marks fall back to filenames like "0".
        if not getattr(chapter, 'title', '') and entries:
            chapter.title = entries[0][1]
        document_chapters.append(chapter)
    return document_chapters


def find_document_chapters_and_extract_texts(book):
    """Returns the book's chapters, each enriched with extracted_text.

    The table of contents decides where the chapters are; documents are only the unit when
    the book has no contents worth following.
    """
    entries_by_file = toc_entries_by_file(book)
    read = read_documents(book, entries_by_file)

    document_chapters = toc_driven_chapters(read)
    if document_chapters:
        print(f'Following the table of contents: {len(read)} documents, '
              f'{len(document_chapters)} chapters.')
    else:
        document_chapters = per_document_chapters(read, entries_by_file)

    for i, c in enumerate(document_chapters):
        c.chapter_index = i  # this is used in the UI to identify chapters
    return document_chapters


def is_chapter(c):
    name = c.get_name().lower()
    has_min_len = len(c.extracted_text) > 100
    title_looks_like_chapter = bool(
        'chapter' in name.lower()
        or re.search(r'part_?\d{1,3}', name)
        or re.search(r'split_?\d{1,3}', name)
        or re.search(r'ch_?\d{1,3}', name)
        or re.search(r'chap_?\d{1,3}', name)
    )
    return has_min_len and title_looks_like_chapter


def chapter_beginning_one_liner(c, chars=20):
    s = c.extracted_text[:chars].strip().replace('\n', ' ').replace('\r', ' ')
    return s + '…' if len(s) > 0 else ''


def find_good_chapters(document_chapters):
    narratable = [c for c in document_chapters
                  if c.get_type() == ebooklib.ITEM_DOCUMENT
                  and not getattr(c, 'is_front_matter', False)]
    chapters = [c for c in narratable if is_chapter(c)]
    if len(chapters) == 0:
        print('Not easy to recognize the chapters, defaulting to all non-empty documents.')
        chapters = [c for c in narratable if len(c.extracted_text) > 10]
    return chapters


def pick_chapters(chapters):
    # Display the document name, the length and first 50 characters of the text
    chapters_by_names = {
        f'{c.get_name()}\t({len(c.extracted_text)} chars)\t[{chapter_beginning_one_liner(c, 50)}]': c
        for c in chapters}
    title = 'Select which chapters to read in the audiobook'
    ret = pick(list(chapters_by_names.keys()), title, multiselect=True, min_selection_count=1)
    selected_chapters_out_of_order = [chapters_by_names[r[0]] for r in ret]
    selected_chapters = [c for c in chapters if c in selected_chapters_out_of_order]
    return selected_chapters


def strfdelta(tdelta, fmt='{D:02}d {H:02}h {M:02}m {S:02}s'):
    remainder = int(tdelta)
    f = Formatter()
    desired_fields = [field_tuple[1] for field_tuple in f.parse(fmt)]
    possible_fields = ('W', 'D', 'H', 'M', 'S')
    constants = {'W': 604800, 'D': 86400, 'H': 3600, 'M': 60, 'S': 1}
    values = {}
    for field in possible_fields:
        if field in desired_fields and field in constants:
            values[field], remainder = divmod(remainder, constants[field])
    return f.format(fmt, **values)


def find_aac_encoder():
    """Pick the best AAC encoder this ffmpeg actually has.

    libfdk_aac is non-free, so most distribution builds (including Homebrew's default) leave it
    out; hardcoding it makes the final m4b encode fail and no m4b is ever produced.
    """
    try:
        encoders = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                                  capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 'aac'
    for encoder in ('libfdk_aac', 'aac_at', 'aac'):
        if re.search(rf'^\s*\S+\s+{re.escape(encoder)}\s', encoders, re.MULTILINE):
            return encoder
    return 'aac'


def concat_wavs_with_ffmpeg(chapter_files, output_folder, filename):
    wav_list_txt = Path(output_folder) / filename.replace('.epub', '_wav_list.txt')
    with open(wav_list_txt, 'w') as f:
        for wav_file in chapter_files:
            # Absolute: ffmpeg's concat demuxer resolves relative entries against the list
            # file's own directory, so a relative output folder would be applied twice.
            # A literal ' is escaped by closing, escaping, and reopening the quote.
            path = str(Path(wav_file).resolve()).replace("'", r"'\''")
            f.write(f"file '{path}'\n")
    # Lossless intermediate: the chapters are concatenated as PCM and the single lossy
    # encode to AAC happens once, in create_m4b. Encoding to AAC here too would double-
    # encode the audio (192k -> 64k) and throw away the first generation.
    concat_file_path = Path(output_folder) / filename.replace('.epub', '.tmp.wav')
    print(f'Concatenating {len(chapter_files)} chapters into a lossless intermediate...')
    proc = subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', wav_list_txt,
        '-c:a', 'pcm_s16le',
        concat_file_path])
    Path(wav_list_txt).unlink()
    if proc.returncode != 0 or not Path(concat_file_path).exists():
        raise RuntimeError(f'ffmpeg failed to concatenate the chapter files (exit {proc.returncode}). '
                           'The .wav chapter files are still on disk.')
    return concat_file_path


def create_m4b(chapter_files, filename, cover_image, output_folder):
    concat_file_path = concat_wavs_with_ffmpeg(chapter_files, output_folder, filename)
    final_filename = Path(output_folder) / filename.replace('.epub', '.m4b')
    chapters_txt_path = Path(output_folder) / "chapters.txt"
    print('Creating M4B file...')

    if cover_image:
        cover_file_path = Path(output_folder) / 'cover'
        with open(cover_file_path, 'wb') as f:
            f.write(cover_image)
        cover_image_args = [
            '-i', f'{cover_file_path}',
            '-map', '2:v',  # Map cover image
            '-disposition:v', 'attached_pic',  # Ensure cover is embedded
            '-c:v', 'copy',  # Keep cover unchanged
        ]
    else:
        cover_image_args = []

    encoder = find_aac_encoder()
    proc = subprocess.run([
        'ffmpeg',
        '-y',  # Overwrite output

        '-i', f'{concat_file_path}',  # Input audio
        '-i', f'{chapters_txt_path}',  # Input chapters
        *cover_image_args,  # Cover image (if provided)

        '-map', '0:a',  # Map audio
        '-c:a', encoder,  # Convert to AAC
        '-b:a', '64k',  # Reduce bitrate for smaller size

        '-map_metadata', '1', # Map metadata

        # Write the moov box (which holds the chapter table) to the start of the
        # file. Without this, the moov is at the end, and Apple Books on iOS
        # has to seek through the whole audio to read it, so large books end up
        # with no chapter menu. macOS tolerates moov-at-end, iOS often does not.
        '-movflags', '+faststart',

        '-f', 'mp4',  # Output as M4B
        f'{final_filename}'  # Output file
    ])

    Path(concat_file_path).unlink()
    if proc.returncode == 0:
        print(f'{final_filename} created. Enjoy your audiobook.')
        print('Feel free to delete the intermediary .wav chapter files, the .m4b is all you need.')


def probe_duration(file_name):
    args = ['ffprobe', '-i', file_name, '-show_entries', 'format=duration', '-v', 'quiet', '-of', 'default=noprint_wrappers=1:nokey=1']
    proc = subprocess.run(args, capture_output=True, text=True, check=True)
    return float(proc.stdout.strip())


def escape_metadata(value):
    """ffmetadata treats =, ;, # and \\ as special, and does not allow raw newlines."""
    escaped = re.sub(r'([=;#\\])', r'\\\1', str(value))
    return escaped.replace('\n', ' ').strip()


def create_index_file(title, creator, chapter_mp3_files, output_folder, chapter_titles=None):
    chapter_titles = chapter_titles or {}
    with open(Path(output_folder) / "chapters.txt", "w", encoding="utf-8") as f:
        f.write(f";FFMETADATA1\ntitle={escape_metadata(title)}\nartist={escape_metadata(creator)}\n\n")
        start = 0
        i = 0
        for c in chapter_mp3_files:
            duration = probe_duration(c)
            end = start + (int)(duration * 1000)
            chapter_title = escape_metadata(chapter_titles.get(str(c)) or f'Chapter {i}')
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle={chapter_title}\n\n")
            i += 1
            start = end
