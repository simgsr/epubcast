# The package used to append its own directory to sys.path so that `from core import main`
# resolved. That made epubcast's modules importable under bare top-level names such as
# `core`, `ui` and `backends`, where they could shadow unrelated modules. The imports are
# fully qualified now, so the path hack is gone.

__version__ = '0.7.0'

# Where audiobooks land unless told otherwise. Kept here rather than in core so that cli.py
# can read it without importing core, which pulls in spacy and the TTS backend.
DEFAULT_OUTPUT_FOLDER = 'audiobooks'
