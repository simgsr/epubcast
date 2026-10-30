import unittest

from epubcast.core import extract_text_from_html


class ExtractTextTest(unittest.TestCase):
    def test_drops_inline_page_numbers(self):
        html = '''<body><p>The first half of the sentence
            <span epub:type="pagebreak" id="page_217" title="217">217</span>
            and the second half.</p></body>'''
        self.assertEqual(extract_text_from_html(html),
                         'The first half of the sentence and the second half.\n')

    def test_drops_page_numbers_marked_only_by_class(self):
        html = '<body><p>Before<span class="pagenum">42</span>after.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'Beforeafter.\n')

    def test_drops_superscript_note_markers(self):
        html = '<body><p>As Darwin noted<sup><a href="#fn12">12</a></sup> the following year.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'As Darwin noted the following year.\n')

    def test_drops_bare_symbol_note_markers(self):
        html = '<body><p>A claim<sup>&#8224;</sup> worth checking.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'A claim worth checking.\n')

    def test_keeps_meaningful_superscripts(self):
        html = '<body><p>The 1<sup>st</sup> attempt.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'The 1st attempt.\n')

    def test_keeps_exponents(self):
        html = '<body><p>About 10<sup>6</sup> cells.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'About 106 cells.\n')

    def test_keeps_subscript_digits(self):
        html = '<body><p>A glass of H<sub>2</sub>O.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'A glass of H2O.\n')

    def test_keeps_page_shaped_class_wrapping_real_text(self):
        html = ('<body><div class="page"><p>A whole page of prose that must survive '
                'the page-marker heuristic.</p></div></body>')
        self.assertIn('A whole page of prose', extract_text_from_html(html))

    def test_drops_note_bodies_and_navigation(self):
        html = '''<body>
            <nav epub:type="toc"><ol><li><a href="ch1.xhtml">Chapter 1</a></li></ol></nav>
            <p>Real prose.</p>
            <aside epub:type="footnote"><p>1. A footnote nobody wants read aloud.</p></aside>
        </body>'''
        self.assertEqual(extract_text_from_html(html), 'Real prose.\n')

    def test_drops_inline_table_of_contents_links(self):
        html = '''<body>
            <p class="toc-entry"><a href="#anchor1"><span>Dedication</span></a>&#160;</p>
            <span><a href="#anchor2">Harvard School Commencement Speech</a></span>
            <p>Now that Headmaster Berrisford has selected me.</p>
        </body>'''
        self.assertEqual(extract_text_from_html(html),
                         'Now that Headmaster Berrisford has selected me.\n')

    def test_keeps_cross_references_inside_prose(self):
        html = '<body><p>See <a href="#ch3">chapter three</a> for the details.</p></body>'
        self.assertEqual(extract_text_from_html(html),
                         'See chapter three for the details.\n')

    def test_nested_blocks_are_not_read_twice(self):
        html = '<body><ol><li><p>Risk: measure it first.</p></li></ol></body>'
        self.assertEqual(extract_text_from_html(html), 'Risk: measure it first.\n')

    def test_extracts_text_outside_the_old_tag_whitelist(self):
        html = '<body><div>First line.<br/>Second line.</div></body>'
        self.assertEqual(extract_text_from_html(html), 'First line.\nSecond line.\n')

    def test_extracts_table_cells(self):
        html = '<body><table><tr><td>Revenue</td><td>Ten million.</td></tr></table></body>'
        self.assertEqual(extract_text_from_html(html), 'Revenue.\nTen million.\n')

    def test_empty_paragraphs_do_not_become_stray_full_stops(self):
        html = '<body><p>&#160;</p><p></p><p>Actual text.</p><p> </p></body>'
        self.assertEqual(extract_text_from_html(html), 'Actual text.\n')

    def test_appends_full_stop_only_when_missing(self):
        html = '<body><h1>A Heading</h1><p>A question?</p><p>A list:</p></body>'
        self.assertEqual(extract_text_from_html(html), 'A Heading.\nA question?\nA list:\n')

    def test_cjk_lines_get_a_cjk_full_stop(self):
        html = '<body><div>臺北的冬夜<br/>經常是下著冷雨的。</div></body>'
        self.assertEqual(extract_text_from_html(html), '臺北的冬夜。\n經常是下著冷雨的。\n')

    def test_closing_quote_after_terminator_is_not_double_punctuated(self):
        html = '<body><p>&#8220;No nation was ever ruined by trade.&#8221;</p></body>'
        self.assertEqual(extract_text_from_html(html),
                         '“No nation was ever ruined by trade.”\n')

    def test_inline_markup_does_not_split_sentences(self):
        html = '<body><p>He <em>really</em> meant <strong>that</strong>.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'He really meant that.\n')

    def test_comments_and_scripts_are_ignored(self):
        html = '<body><!-- a comment --><script>var x = 1;</script><p>Prose.</p></body>'
        self.assertEqual(extract_text_from_html(html), 'Prose.\n')


if __name__ == '__main__':
    unittest.main()
