import shutil
import socket
import subprocess
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ebooklib import epub

from epubcast.core import main, find_document_chapters_and_extract_texts


def _network_available():
    """The integration tests download books from the internet; skip cleanly offline."""
    try:
        socket.create_connection(('www.gutenberg.org', 443), timeout=5).close()
        return True
    except OSError:
        return False


def _download(url, dest):
    """Fetch `url` into `dest`, returning False if it cannot be fetched.

    These tests used to shell out to `wget`, which macOS does not ship, and os.system
    reported nothing about the failure -- so every test then died later on a bare
    FileNotFoundError naming the .epub, far from the cause. urllib is in the stdlib and the
    outcome is checked here, so a missing network fails as a skip rather than an error.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        urllib.request.urlretrieve(url, dest)
    except (urllib.error.URLError, OSError):
        return False
    return dest.exists() and dest.stat().st_size > 0


@unittest.skipUnless(_network_available(), 'network required to download test books')
class MainTest(unittest.TestCase):
    def base(self, name, url='', **kwargs):
        book = Path(f'{name}.epub')
        if url and not _download(url, book):
            self.skipTest(f'could not download {url}')
        if not book.exists():
            self.skipTest(f'{book} is not available locally')
        Path(f'{name}.m4b').unlink(missing_ok=True)
        for leftover in Path('.').glob(f'{name}_chapter_*.wav'):
            leftover.unlink(missing_ok=True)
        merged_args = dict(voice='af_sky', pick_manually=False, speed=1.0, max_chapters=1, max_sentences=2)
        merged_args.update(kwargs)
        main(f'{name}.epub', **merged_args)
        m4b_file = Path(f'{name}.m4b')
        self.assertTrue(m4b_file.exists())
        self.assertTrue(m4b_file.stat().st_size > 1024)

    def test_poe(self):
        url = 'https://www.gutenberg.org/ebooks/1064.epub.images'
        self.base('poe', url)

    def test_orwell(self):
        url = 'https://archive.org/download/AnimalFarmByGeorgeOrwell/Animal%20Farm%20by%20George%20Orwell.epub'
        self.base('orwell', url)

    def test_italian_pirandello_and_filename_with_spaces(self):
        url = 'https://www.liberliber.eu/mediateca/libri/p/pirandello/cosi_e_se_vi_pare_1925/epub/pirandello_cosi_e_se_vi_pare_1925.epub'
        self.base('pirandello e spazio', url, voice='im_nicola')
        self.assertTrue(Path('pirandello e spazio.m4b').exists())

    def test_italian_manzoni(self):
        url = 'https://www.liberliber.eu/mediateca/libri/m/manzoni/i_promessi_sposi/epub/manzoni_i_promessi_sposi.epub'
        self.base('manzoni', url, voice='im_nicola')

    def test_french_baudelaire(self):
        url = 'http://gallica.bnf.fr/ark:/12148/bpt6k70861t.epub'
        self.base('baudelaire', url, voice='ff_siwis')

    def test_chinese(self):
        url = 'https://www.gutenberg.org/ebooks/24225.epub3.images'
        self.base('chinese', url, voice='zf_xiaobei')

    @unittest.skipUnless(Path('leigh.epub').exists(),
                         'leigh.epub is a local fixture and is not tracked in the repo')
    def test_leigh_and_play_result(self):
        book = epub.read_epub('leigh.epub')
        document_chapters = find_document_chapters_and_extract_texts(book)
        chapters = [c for c in document_chapters if c.get_name() == 'Text/Chap07.xhtml']
        self.base('leigh', voice='af_heart', selected_chapters=chapters, max_sentences=5)
        # Playback is a convenience for a human running this by hand; ffplay is not part of
        # this project's dependencies, so its absence must not fail the assertion above.
        if shutil.which('ffplay'):
            subprocess.run(['ffplay', '-nodisp', '-autoexit', 'leigh.m4b'], check=True)


if __name__ == '__main__':
    unittest.main()
