"""Markdown regressions checked against Telegram's ordinary HTML constraints."""
import html
from html.parser import HTMLParser
import random
import re
import unittest

from telegram_formatting import html_chunks, plain_html_text, render_inline_markdown, render_markdown_html


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}, tag
        if tag == "code":
            assert not self.stack or self.stack == ["pre"], self.stack
        elif tag == "pre":
            assert not self.stack, self.stack
        elif tag in {"a", "blockquote"}:
            assert not any(x in {"a", "blockquote", "pre", "code"} for x in self.stack), self.stack
        else:
            assert not any(x in {"code", "pre"} for x in self.stack), self.stack
        for name, value in attrs:
            assert (tag == "a" and name == "href" and value.startswith(("https://", "http://"))) or (
                tag == "code" and name == "class" and re.fullmatch(r"language-[A-Za-z0-9_+.#-]{1,32}", value)
            ), (tag, attrs)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, (self.stack, tag)

    def handle_data(self, value):
        assert not re.search(r"&(?![a-zA-Z0-9#]+;)", value), value
        self.text.append(value)

    def handle_entityref(self, name):
        assert name in {"amp", "lt", "gt", "quot"}, name
        self.text.append(html.unescape(f"&{name};"))

    def handle_charref(self, name):
        self.text.append(html.unescape(f"&#{name};"))


def validate(value):
    parser = TelegramHTML()
    parser.feed(value)
    parser.close()
    assert not parser.stack, parser.stack
    return "".join(parser.text)


class MarkdownTests(unittest.TestCase):
    def test_fenced_code_never_parses_tables_or_diagrams(self):
        source = '| key | value |\n| --- | --- |\n| a | **literal** |\nflowchart TD\nA-->B\n> quote\n<br>'
        rendered = render_markdown_html('```text\n' + source + '\n```')
        self.assertEqual(rendered, '<pre><code class="language-text">' + html.escape(source) + '</code></pre>')
        self.assertEqual(validate(rendered), source)

    def test_long_fence_keeps_short_fences_literal(self):
        rendered = render_markdown_html('````markdown\n```python\nprint(1)\n```\n````\n**after**')
        self.assertIn('```python\nprint(1)\n```</code></pre>', rendered)
        self.assertTrue(rendered.endswith('<b>after</b>'))
        validate(rendered)

    def test_tilde_fence_closes_only_with_same_marker(self):
        self.assertEqual(validate(render_markdown_html('~~~python\n```\n**literal**\n~~~')), '```\n**literal**')

    def test_unfinished_fence_preserves_all_code(self):
        self.assertEqual(validate(render_markdown_html('```js\na < b && c > d\n**literal**')), 'a < b && c > d\n**literal**')

    def test_inline_code_handles_multiple_ticks_and_literal_br(self):
        rendered = render_inline_markdown('`` a ` b <br> **literal** ``')
        self.assertEqual(rendered, '<code>a ` b &lt;br&gt; **literal**</code>')
        validate(rendered)

    def test_nested_emphasis_and_triple_delimiters(self):
        for source, expected in (
            ('**bold *italic***', '<b>bold <i>italic</i></b>'),
            ('*italic **bold***', '<i>italic <b>bold</b></i>'),
            ('*italic **bold** tail*', '<i>italic <b>bold</b> tail</i>'),
            ('***both***', '<b><i>both</i></b>'),
            ('__bold _italic___', '<b>bold <i>italic</i></b>'),
            ('~~old **bold**~~', '<s>old <b>bold</b></s>'),
        ):
            with self.subTest(source=source):
                rendered = render_inline_markdown(source)
                self.assertEqual(rendered, expected)
                validate(rendered)

    def test_code_is_not_nested_under_emphasis(self):
        rendered = render_inline_markdown('**bold *and `code` tail* end**')
        self.assertEqual(validate(rendered), 'bold and code tail end')
        self.assertIn('<code>code</code>', rendered)

    def test_escapes_and_identifiers_stay_literal(self):
        source = r'\*literal\* foo_bar_baz snake_case x_y \[label\] C:\Users\name'
        rendered = render_inline_markdown(source)
        self.assertNotIn('<i>', rendered)
        self.assertEqual(validate(rendered), '*literal* foo_bar_baz snake_case x_y [label] C:\\Users\\name')

    def test_balanced_links_titles_and_formatted_labels(self):
        rendered = render_inline_markdown('[**Docs**](https://example.com/a_(b)?x=1&y=2 "title")')
        self.assertEqual(rendered, '<a href="https://example.com/a_(b)?x=1&amp;y=2"><b>Docs</b></a>')
        self.assertEqual(validate(rendered), 'Docs')

    def test_link_escape_and_nested_label(self):
        rendered = render_inline_markdown(r'[Docs [v2]](https://example.com/a_\(b\))')
        self.assertIn('href="https://example.com/a_(b)"', rendered)
        self.assertEqual(validate(rendered), 'Docs [v2]')

    def test_link_label_code_does_not_create_forbidden_nested_entities(self):
        rendered = render_inline_markdown('[`code`](https://example.com)')
        self.assertEqual(rendered, '<a href="https://example.com">code</a>')
        validate(rendered)

    def test_unsafe_and_broken_links_are_readable_text(self):
        for source in ('[unsafe](javascript:alert(1))', '[local](file:///etc/passwd)', '[broken](https://example.com', '[bad](https://[bad)'):
            with self.subTest(source=source):
                rendered = render_inline_markdown(source)
                self.assertNotIn('<a ', rendered)
                self.assertEqual(validate(rendered), source)

    def test_raw_html_is_literal_and_named_entities_are_supported(self):
        source = '<b>literal</b> &amp; &copy; <script>x</script>'
        rendered = render_inline_markdown(source)
        self.assertNotIn('<script>', rendered)
        self.assertEqual(validate(rendered), '<b>literal</b> & © <script>x</script>')

    def test_autolink_and_br_outside_code(self):
        rendered = render_inline_markdown('<https://example.com?a=1&b=2><br/>next')
        self.assertIn('<a href="https://example.com?a=1&amp;b=2">', rendered)
        self.assertEqual(validate(rendered), 'https://example.com?a=1&b=2\nnext')

    def test_quotes_use_one_native_blockquote_and_keep_following_text(self):
        rendered = render_markdown_html('> **first**\n> second\n>> nested\n\nnormal')
        self.assertEqual(rendered, '<blockquote><b>first</b>\nsecond\nnested</blockquote>\n\nnormal')
        validate(rendered)

    def test_quote_tip_and_link_destination_remain_visible(self):
        rendered = render_markdown_html('> [!TIP] Note\n> [`code`](https://example.com)')
        self.assertIn('<blockquote><b>💡 提示</b>：Note', rendered)
        self.assertIn('code (https://example.com)', validate(rendered))

    def test_quote_code_is_plain_instead_of_illegal_nested_entities(self):
        rendered = render_markdown_html('> ```text\n> **literal**\n> ```')
        self.assertEqual(rendered, '<blockquote>**literal**</blockquote>')
        validate(rendered)

    def test_table_escaped_pipes_and_inline_code(self):
        rendered = render_markdown_html('| A | B |\n| --- | --- |\n| x\\|y | `a|b` |')
        self.assertIn('<b>A</b>：x|y', rendered)
        self.assertIn('<b>B</b>：<code>a|b</code>', rendered)
        validate(rendered)

    def test_table_does_not_drop_extra_values(self):
        rendered = render_markdown_html('A | B\n--- | ---\n1 | 2 | 3')
        self.assertIn('列 3</b>：3', rendered)
        validate(rendered)

    def test_checklist_and_indented_list(self):
        rendered = render_markdown_html('- [x] done\n  - [ ] todo\n2) next')
        self.assertEqual(validate(rendered), '☑ done\n　☐ todo\n2. next')

    def test_chunk_entities_never_split_and_visible_text_is_exact(self):
        rendered = '<b>' + 'a&amp;&lt;&gt;&quot;&#39;&#x1F600;' * 80 + '</b>'
        chunks = html_chunks(rendered, units=17)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(validate(x) for x in chunks), html.unescape(re.sub('<[^>]+>', '', rendered)))
        self.assertTrue(all(len(validate(x).encode('utf-16-le')) // 2 <= 17 for x in chunks))

    def test_chunk_reopens_code_language_and_native_quote(self):
        for source in ('```python\n' + 'print("😀&<>")\n' * 50 + '```', '> ' + '你好😀 **重点** ' * 80):
            with self.subTest(source=source[:20]):
                rendered = render_markdown_html(source)
                chunks = html_chunks(rendered, units=29)
                self.assertEqual(''.join(validate(x) for x in chunks), validate(rendered))
                self.assertTrue(all(len(validate(x).encode('utf-16-le')) // 2 <= 29 for x in chunks))

    def test_long_link_attributes_do_not_split_or_count_as_message_text(self):
        url = 'https://example.com/' + 'x' * 4000
        rendered = render_inline_markdown('[Docs](' + url + ')')
        self.assertEqual(html_chunks(rendered, units=5), [rendered])
        validate(rendered)

    def test_plain_fallback_retains_link_targets_and_decodes_once(self):
        self.assertEqual(plain_html_text('<b>Docs</b> <a href="https://example.com?a=1&amp;b=2">link</a> &amp;lt;'),
                         'Docs link (https://example.com?a=1&b=2) &lt;')

    def test_heading_retains_literal_hash_in_language_name(self):
        self.assertEqual(render_markdown_html('## C#\n## Title ##'), '<b>C#</b>\n<b>Title</b>')

    def test_fuzz_keeps_all_pages_parseable(self):
        randomizer = random.Random(20261024)
        pieces = ['**a**', '*b*', '`x<>&`', '> quoted\n', '[x](https://example.com/a_(b))', r'\*',
                  'foo_bar_baz', '&amp;', '😀', '~~old~~', '<script>x</script>', '***both***', '\n', ' ']
        for _ in range(80):
            source = ''.join(randomizer.choice(pieces) for _ in range(35))
            rendered = render_markdown_html(source)
            self.assertEqual(''.join(validate(page) for page in html_chunks(rendered, units=41)), validate(rendered))

    def test_large_unmatched_delimiters_remain_readable(self):
        for source in ('*a ' * 10000, '_a ' * 10000, '[' * 15000 + ']' * 15000,
                       '[' * 10000 + '](https://example.com)', '`' * 30000):
            with self.subTest(prefix=source[:10]):
                self.assertEqual(validate(render_inline_markdown(source)), source)


if __name__ == '__main__':
    unittest.main()
