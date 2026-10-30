import unittest

from epubcast.core import (MIN_LENGTH_TO_SPLIT, extract_sections_from_html, extract_text_from_html,
                           escape_metadata, find_document_chapters_and_extract_texts)


class FakeLink:
    def __init__(self, href, title):
        self.href, self.title = href, title


class FakeItem:
    def __init__(self, name, body):
        self.name, self.body = name, body

    def get_type(self):
        return 9  # ebooklib.ITEM_DOCUMENT

    def get_name(self):
        return self.name

    def get_id(self):
        return self.name

    def get_body_content(self):
        return self.body


class FakeBook:
    def __init__(self, items, toc, spine=None):
        self.items, self.toc, self.spine = items, toc, spine or []

    def get_items(self):
        return self.items


def document(name, *paragraphs):
    body = ''.join(f'<p>{p}</p>' for p in paragraphs)
    return FakeItem(name, f'<body>{body}</body>')


def prose(words=40):
    return 'More prose. ' * words


def body_with_anchors(*sections, padding=0):
    """Build a document whose sections start at <h2 id="anchorN">, optionally padded to be long."""
    html = ''
    for i, (anchor, heading, prose) in enumerate(sections, start=1):
        filler = f' {"padding words here." * padding}' if padding else ''
        html += f'<h2 id="{anchor}">{heading}</h2><p>{prose}{filler}</p>'
    return f'<body>{html}</body>'


class SplitSectionsTest(unittest.TestCase):
    def test_sections_are_cut_at_anchors(self):
        html = body_with_anchors(('a1', 'One', 'First body.'), ('a2', 'Two', 'Second body.'))
        sections = extract_sections_from_html(html, ['a1', 'a2'])
        self.assertEqual([anchor for anchor, _ in sections], ['a1', 'a2'])
        self.assertEqual(sections[0][1], 'One.\nFirst body.\n')
        self.assertEqual(sections[1][1], 'Two.\nSecond body.\n')

    def test_text_before_the_first_anchor_is_its_own_section(self):
        html = '<body><p>Front text.</p><h2 id="a1">One</h2><p>Body.</p></body>'
        sections = extract_sections_from_html(html, ['a1'])
        self.assertEqual(sections[0], (None, 'Front text.\n'))
        self.assertEqual(sections[1], ('a1', 'One.\nBody.\n'))

    def test_anchors_on_inline_spans_still_split(self):
        html = '<body><p><span id="a1"></span>First.</p><p><span id="a2"></span>Second.</p></body>'
        sections = extract_sections_from_html(html, ['a1', 'a2'])
        self.assertEqual([text for _, text in sections], ['First.\n', 'Second.\n'])

    def test_no_anchors_means_one_section(self):
        sections = extract_sections_from_html('<body><p>Just prose.</p></body>')
        self.assertEqual(sections, [(None, 'Just prose.\n')])


class SplitBookTest(unittest.TestCase):
    def build(self, padding, extra_items=()):
        body = body_with_anchors(('a1', 'One', 'First.'), ('a2', 'Two', 'Second.'),
                                 ('a3', 'Contents', 'Third.'), padding=padding)
        item = FakeItem('book.xhtml', body)
        toc = [FakeLink('book.xhtml#a1', 'One'), FakeLink('book.xhtml#a2', 'Two'),
               FakeLink('book.xhtml#a3', 'Contents')]
        return FakeBook([item, *extra_items], toc)

    def test_single_file_book_is_split(self):
        chapters = find_document_chapters_and_extract_texts(self.build(padding=15))
        self.assertEqual([c.get_name() for c in chapters],
                         ['book.xhtml#a1', 'book.xhtml#a2', 'book.xhtml#a3'])
        self.assertEqual([c.title for c in chapters], ['One', 'Two', 'Contents'])
        self.assertEqual([c.chapter_index for c in chapters], [0, 1, 2])

    def test_split_sections_named_as_front_matter_are_flagged(self):
        chapters = find_document_chapters_and_extract_texts(self.build(padding=15))
        self.assertEqual([c.is_front_matter for c in chapters], [False, False, True])

    def test_short_sections_are_dropped_as_stubs(self):
        # Without padding every section is a heading plus a few words: all stubs, so no split.
        chapters = find_document_chapters_and_extract_texts(self.build(padding=0))
        self.assertEqual([c.get_name() for c in chapters], ['book.xhtml'])

    def test_documents_split_at_toc_anchors_even_in_a_multi_file_book(self):
        # The contents, not the file layout, decides where the chapters are: book.xhtml holds
        # three entries and becomes three chapters however long it is. other.xhtml trails the
        # last entry in a book that keeps one file per chapter, so it stands on its own.
        other = FakeItem('other.xhtml', '<body><p>%s</p></body>' % prose())
        chapters = find_document_chapters_and_extract_texts(self.build(padding=15, extra_items=[other]))
        self.assertEqual([c.get_name() for c in chapters],
                         ['book.xhtml#a1', 'book.xhtml#a2', 'book.xhtml#a3', 'other.xhtml'])

    def test_long_documents_split_even_in_a_multi_file_book(self):
        padding = MIN_LENGTH_TO_SPLIT // 20  # length no longer decides: the contents does
        other = FakeItem('other.xhtml', '<body><p>%s</p></body>' % prose())
        chapters = find_document_chapters_and_extract_texts(self.build(padding=padding, extra_items=[other]))
        self.assertEqual([c.get_name() for c in chapters],
                         ['book.xhtml#a1', 'book.xhtml#a2', 'book.xhtml#a3', 'other.xhtml'])


class TocDrivenChaptersTest(unittest.TestCase):
    """A chapter runs from one entry in the contents to the next, over as many files as it takes."""

    def build(self, extra_items=(), extra_toc=()):
        items = [document('cover.xhtml', 'Cover'),
                 document('ch01.xhtml', 'One'),
                 document('ch01a.xhtml', prose()),
                 document('ch01b.xhtml', prose()),
                 document('ch02.xhtml', 'Two'),
                 document('ch02a.xhtml', prose()),
                 *extra_items]
        toc = [FakeLink('ch01.xhtml', 'Chapter One'), FakeLink('ch02.xhtml', 'Chapter Two'), *extra_toc]
        return FakeBook(items, toc)

    def test_untocked_documents_continue_the_previous_chapter(self):
        chapters = find_document_chapters_and_extract_texts(self.build())
        self.assertEqual([c.title for c in chapters], ['cover.xhtml', 'Chapter One', 'Chapter Two'])
        self.assertEqual(chapters[1].extracted_text.count('More prose.'), 80)  # ch01a + ch01b
        self.assertEqual(chapters[2].extracted_text.count('More prose.'), 40)  # ch02a

    def test_no_text_is_lost(self):
        book = self.build()
        chapters = find_document_chapters_and_extract_texts(book)
        joined = ''.join(c.extracted_text for c in chapters)
        self.assertEqual(len(joined),
                         sum(len(extract_text_from_html(i.get_body_content())) for i in book.get_items()))

    def test_a_trailing_file_continues_a_book_that_spills_chapters_over_files(self):
        chapters = find_document_chapters_and_extract_texts(
            self.build(extra_items=[document('ch02b.xhtml', prose())]))
        self.assertEqual([c.title for c in chapters], ['cover.xhtml', 'Chapter One', 'Chapter Two'])
        self.assertEqual(chapters[-1].extracted_text.count('More prose.'), 80)

    def test_back_matter_after_the_last_entry_stays_its_own_chapter(self):
        # One file per chapter, so an index the contents forgot is back matter, not the rest
        # of the last chapter: it must not be glued onto the end of it.
        book = FakeBook([document('ch01.xhtml', 'One', prose()),
                         document('ch02.xhtml', 'Two', prose()),
                         document('index.xhtml', prose())],
                        [FakeLink('ch01.xhtml', 'Chapter One'), FakeLink('ch02.xhtml', 'Chapter Two')])
        chapters = find_document_chapters_and_extract_texts(book)
        self.assertEqual([c.title for c in chapters], ['Chapter One', 'Chapter Two', 'index.xhtml'])

    def test_chapters_follow_the_spine_not_the_manifest(self):
        book = self.build()
        book.items = list(reversed(book.items))  # a manifest in an order of its own
        book.spine = [(i.get_name(), 'yes') for i in reversed(book.items)]
        chapters = find_document_chapters_and_extract_texts(book)
        self.assertEqual([c.title for c in chapters], ['cover.xhtml', 'Chapter One', 'Chapter Two'])

    def test_a_book_without_a_contents_keeps_one_chapter_per_document(self):
        book = self.build()
        book.toc = []
        chapters = find_document_chapters_and_extract_texts(book)
        self.assertEqual([c.get_name() for c in chapters],
                         ['cover.xhtml', 'ch01.xhtml', 'ch01a.xhtml', 'ch01b.xhtml',
                          'ch02.xhtml', 'ch02a.xhtml'])


class MetadataEscapingTest(unittest.TestCase):
    def test_special_characters_are_escaped(self):
        self.assertEqual(escape_metadata('Risk = reward; see #3'), r'Risk \= reward\; see \#3')

    def test_newlines_are_flattened(self):
        self.assertEqual(escape_metadata('Two\nlines'), 'Two lines')


if __name__ == '__main__':
    unittest.main()
