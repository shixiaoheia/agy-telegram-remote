"""Native Telegram card layout and delivery invariants for the visual refresh."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from .common import settings_at
    from .test_bot import FakeAPI, FakeRunner, attachment_update, callback_update, update
    from .test_telegram_formatting import validate
except ImportError:
    from common import settings_at
    from test_bot import FakeAPI, FakeRunner, attachment_update, callback_update, update
    from test_telegram_formatting import validate

from agy_runner import Result
from bot import Bridge, Job, describe_html, display_model, question_html, result_footer
from state_store import Store
from telegram_api import TelegramError
from telegram_formatting import html_chunks, render_markdown_html


class VisualCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = settings_at(Path(self.temp.name))
        self.store = Store(self.settings.state_dir, self.settings.allowed, self.settings.max_reply, 7, self.settings.token)
        self.api, self.runner = FakeAPI(), FakeRunner()
        self.bridge = Bridge(self.settings, self.api, self.store, self.runner)

    async def asyncTearDown(self):
        self.runner.finish.set()
        job = self.bridge.slot
        if job:
            job.cancel.set()
            if job.worker:
                await asyncio.wait_for(job.worker, 2)
        if self.bridge.steer_tasks:
            await asyncio.gather(*tuple(self.bridge.steer_tasks), return_exceptions=True)
        if self.bridge.reply_worker:
            await self.bridge.reply_worker
        self.store.close()
        self.temp.cleanup()

    def test_reference_layout_has_question_body_and_two_line_statistics(self):
        record = {"title": "1 + 1", "outcome": "success", "model": "gemini-3.8-flash-low",
                  "duration_seconds": 43, "text": "1 + 1 = 2", "input_tokens": 3512,
                  "output_tokens": 7, "total_tokens": 3519}
        rendered = describe_html(record)
        self.assertEqual(rendered, '<blockquote>❓ 提问：<b>1 + 1</b></blockquote>\n\n1 + 1 = 2\n\n'
                         '<blockquote>🤖 Gemini 3.8 Flash (Low) · ⏱ 43s\n'
                         '📊 输入 3,512 · 输出 7 · 共 3,519 tokens</blockquote>')
        validate(rendered)

    def test_question_escapes_markup_and_limits_echo(self):
        rendered = question_html('<b>literal</b> **question** ' + '长' * 300, '📎 中文日志.log')
        self.assertIn('&lt;b&gt;literal&lt;/b&gt;', rendered)
        self.assertIn('…', rendered)
        self.assertIn('中文日志.log', rendered)
        self.assertLess(len(validate(rendered)), 170)

    def test_model_names_and_missing_metrics_are_honest(self):
        self.assertEqual(display_model('claude-sonnet-5-5-medium'), 'Claude Sonnet 5.5 (Medium)')
        self.assertEqual(display_model('gemini-4-argon-high'), 'Gemini 4 Argon (High)')
        self.assertEqual(display_model('custom-model', 'high'), 'custom-model (High)')
        self.assertIn('未提供', result_footer({}))
        self.assertIn('输入 —', result_footer({}))
        for value in (float('nan'), float('inf'), -1, True, 10**400):
            self.assertIn('未提供', result_footer({'duration_seconds': value}))

    def test_error_and_retrieved_delivery_warning_stay_visible(self):
        rendered = describe_html({'outcome': 'error', 'title': '任务', 'detail': '<untrusted>',
                                  'delivery': 'failed_or_partial', 'metadata_warning': '统计可能缺失'})
        self.assertIn('❌ 执行异常', rendered)
        self.assertIn('&lt;untrusted&gt;', rendered)
        self.assertIn('上次发送未完成', rendered)
        self.assertIn('没有自动重跑', rendered)
        self.assertIn('统计可能缺失', rendered)
        validate(rendered)

    def test_success_has_no_duplicate_model_or_token_sections(self):
        rendered = describe_html({'outcome': 'success', 'model': 'gemini-3.8-flash-low', 'text': '**answer**'})
        self.assertEqual(rendered.count('Gemini 3.8 Flash'), 1)
        self.assertEqual(rendered.count('tokens'), 1)
        self.assertNotIn('运行统计', rendered)
        self.assertNotIn('━━━━━━━━', rendered)
        self.assertIn('<b>answer</b>', rendered)

    def test_model_and_effort_menus_have_two_columns_and_stable_footer(self):
        self.store.set_model(12345, 'claude-sonnet-5-5-medium')
        _, menu = self.bridge._model_picker(12345)
        self.assertTrue(all(1 <= len(row) <= 2 for row in menu['inline_keyboard']))
        selected = [b for row in menu['inline_keyboard'] for b in row if b['text'].startswith('✓')]
        self.assertEqual(selected[0]['callback_data'], 'model:claude-sonnet-5-5-medium')
        self.assertIn('✍️', selected[0]['text'])
        _, picker = self.bridge._effort_picker(12345, 'Sonnet', model='claude-sonnet-5-5-medium')
        self.assertEqual([len(row) for row in picker['inline_keyboard']], [2, 2, 2])
        self.assertEqual([b['callback_data'] for b in picker['inline_keyboard'][-1]], ['selector:models', 'selector:close'])
        self.assertTrue(any('⚖️ 均衡' in b['text'] and b['text'].startswith('✓')
                            for row in picker['inline_keyboard'] for b in row))
        self.assertEqual(self.runner.calls, 0)

    def test_unavailable_effort_is_not_reintroduced_by_grid(self):
        self.store.set_model_health({'gemini-3.8-flash-medium': {'outcome': 'error', 'category': 'entitlement'}})
        _, picker = self.bridge._effort_picker(12345, 'Flash', model='gemini-3.8-flash-low')
        callbacks = [b['callback_data'] for row in picker['inline_keyboard'] for b in row]
        self.assertNotIn('choose:gemini-3.8-flash-medium', callbacks)
        self.assertTrue(all(len(row) <= 2 for row in picker['inline_keyboard']))

    async def test_short_result_replaces_same_message_and_keeps_buttons(self):
        self.runner.result = Result('success', text='**final answer**', model='gemini-3.8-flash-low')
        await self.bridge.handle(update('original question'))
        job = self.bridge.slot
        await job.worker
        self.assertEqual(self.api.sent_count, 1)
        self.assertEqual(len(self.api.messages), 1)
        message = self.api.messages[self.api.message_indices[job.status_message_id]]
        self.assertIn('<b>final answer</b>', message[1])
        self.assertIn('original question', message[1])
        self.assertEqual(self.api.edit_parse_modes[-1], 'HTML')
        self.assertEqual(message[2]['inline_keyboard'][0][0]['callback_data'], f'history:{job.job_id}')
        self.assertNotIn('cancel:', str(message[2]))
        self.assertEqual(self.store.load(12345)['delivery'], 'sent')
        self.assertEqual(self.store.load(12345)['text'], '**final answer**')
        validate(message[1])

    async def test_acceptance_echoes_question_before_runner_finishes(self):
        self.runner.hold = True
        await self.bridge.handle(update('which task <now>?'))
        await self.runner.started.wait()
        first = self.api.sent_messages[0]
        self.assertIn('which task &lt;now&gt;?', first[1])
        self.assertIn('⏳ 任务执行中', first[1])
        self.assertIn('等待首个步骤', first[1])
        self.assertEqual(first[2]['inline_keyboard'][-1][0]['callback_data'], f'cancel:{self.bridge.slot.job_id}')
        self.assertEqual(self.api.parse_modes[0], 'HTML')
        validate(first[1])

    def test_progress_collapses_trace_redacts_and_bounds_content(self):
        job = Job(12345, 12345, history_title='task', activity='<tool>' + 'a' * 300,
                  execution_steps=['step ' + str(i) + self.settings.token for i in range(10)])
        rendered = self.bridge._progress_text(job)
        self.assertNotIn('expandable', rendered)
        self.assertNotIn('step 8', rendered)
        self.assertNotIn(self.settings.token, rendered)
        self.assertNotIn('step 0', rendered)
        self.assertIn('step 9', rendered)
        self.assertNotIn('a' * 100, rendered)
        job.cancel.set()
        self.assertIn('正在取消并清理', self.bridge._progress_text(job))
        validate(rendered)

    async def test_long_result_has_buttons_only_on_last_page(self):
        self.runner.result = Result('success', text='paragraph **bold** 😀\n' * 500)
        await self.bridge.handle(update('long question'))
        job = self.bridge.slot
        await job.worker
        first = self.api.messages[self.api.message_indices[job.status_message_id]]
        self.assertIn('第 1/', first[1])
        self.assertEqual(first[2], {'inline_keyboard': []})
        for message in self.api.sent_messages[1:-1]:
            self.assertIsNone(message[2])
        self.assertIn('第 ', self.api.sent_messages[-1][1])
        self.assertEqual(self.api.sent_messages[-1][2]['inline_keyboard'][0][0]['callback_data'], f'history:{job.job_id}')
        self.assertEqual(self.store.load(12345)['delivery'], 'sent')
        for _, body, _ in self.api.messages:
            validate(body)

    async def test_later_page_failure_preserves_already_replaced_first_page(self):
        self.runner.result = Result('success', text='UNIQUE_FIRST\n' + 'long text\n' * 1000)
        self.api.fail_at = {2}
        await self.bridge.handle(update('long question'))
        job = self.bridge.slot
        await job.worker
        first = self.api.messages[self.api.message_indices[job.status_message_id]]
        self.assertIn('UNIQUE_FIRST', first[1])
        self.assertIn('结果发送失败或不完整', self.api.messages[-1][1])
        self.assertEqual(self.store.load(12345)['delivery'], 'failed_or_partial')
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(sum('UNIQUE_FIRST' in body for _, body, _ in self.api.messages), 1)

    async def test_ambiguous_first_page_edit_never_resends_answer(self):
        self.api.edit = AsyncMock(side_effect=TelegramError())
        delivered = await self.bridge.send_html(12345, '<b>answer</b>', message_id=1)
        self.assertFalse(delivered)
        self.assertEqual(self.api.edit.await_count, 1)
        self.assertEqual(self.api.sent_count, 0)

    async def test_definite_rejection_downgrades_same_bubble_once(self):
        self.api.edit = AsyncMock(side_effect=[TelegramError(400), True])
        keyboard = {'inline_keyboard': [[{'text': 'result', 'callback_data': 'history:' + 'a' * 12}]]}
        self.assertTrue(await self.bridge.send_html(12345, '<b>answer</b>', message_id=7, reply_markup=keyboard))
        self.assertEqual(self.api.edit.await_count, 2)
        final = self.api.edit.await_args_list[-1]
        self.assertEqual(final.args, (12345, 7, 'answer'))
        self.assertEqual(final.kwargs['reply_markup'], keyboard)
        self.assertNotIn('parse_mode', final.kwargs)
        self.assertEqual(self.api.sent_count, 0)

    async def test_attachment_question_echo_and_history_preserve_name(self):
        await self.bridge.handle(attachment_update(caption='分析这个日志', document={
            'file_id': 'chinese', 'file_size': 4, 'file_name': '中文日志.log'}))
        job = self.bridge.slot
        await job.worker
        record = self.store.load(12345)
        self.assertIn('中文日志.log', record['attachment_summary'])
        self.assertNotIn(str(self.settings.workspace), record['attachment_summary'])
        self.assertIn('中文日志.log', describe_html(record))
        self.assertIn('分析这个日志', describe_html(record))
        self.assertFalse(job.attachment_dir.exists())

    async def test_execution_summary_is_saved_bounded_and_folded(self):
        self.runner.hold = True
        await self.bridge.handle(update('task'))
        await self.runner.started.wait()
        job = self.bridge.slot
        job.execution_steps = ['✅ step ' + str(i) + self.settings.token for i in range(6)]
        self.runner.finish.set()
        await job.worker
        record = self.store.load(12345)
        self.assertNotIn(self.settings.token, record['execution_summary'])
        self.assertIn('<blockquote expandable><b>执行过程</b>', describe_html(record))
        self.assertEqual(self.store.history_item(12345, job.job_id)['execution_summary'], record['execution_summary'])
        extra = self.store.save(12345, {'execution_summary': 'x' * 5000, 'attachment_summary': 'y' * 5000})
        self.assertLessEqual(len(extra['execution_summary']), 1200)
        self.assertLessEqual(len(extra['attachment_summary']), 400)

    async def test_history_retrieval_keeps_buttons_and_does_not_execute(self):
        await self.bridge.handle(update('task'))
        job = self.bridge.slot
        await job.worker
        await self.bridge.handle(callback_update(f'history:{job.job_id}'))
        await self.bridge.reply_worker
        self.assertEqual(self.api.messages[-1][2]['inline_keyboard'][0][0]['callback_data'], f'history:{job.job_id}')
        self.assertEqual(self.runner.calls, 1)

    async def test_edit_api_sends_html_and_disables_link_previews(self):
        from telegram_api import TelegramAPI
        api = TelegramAPI('fixture-token')
        api.call = AsyncMock(return_value=True)
        await api.edit(12345, 7, '<b>answer</b>', parse_mode='HTML', reply_markup={'inline_keyboard': []})
        payload = api.call.await_args.kwargs
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertEqual(payload['link_preview_options'], {'is_disabled': True})
        self.assertEqual(payload['reply_markup'], {'inline_keyboard': []})


class ReadingLayoutTests(unittest.TestCase):
    def test_source_footer_is_outside_quote_and_preserves_link(self):
        rendered = render_markdown_html('> quoted **text**\n> — 来源：[文档](https://example.com)')
        self.assertIn('</blockquote>\n— 来源：<a href="https://example.com">文档</a>', rendered)
        validate(rendered)

    def test_long_quote_is_native_expandable_but_short_quote_is_not(self):
        self.assertIn('<blockquote expandable>', render_markdown_html('\n'.join('> line ' + str(i) for i in range(6))))
        self.assertIn('<blockquote>', render_markdown_html('> short'))

    def test_source_looking_line_inside_unfinished_code_is_not_a_footer(self):
        rendered = render_markdown_html('> ```text\n> 来源：literal')
        self.assertEqual(rendered, '<blockquote>来源：literal</blockquote>')

    def test_paragraph_boundary_is_preferred_without_losing_text(self):
        rendered = 'a' * 13 + '\n' + 'b' * 13
        pages = html_chunks(rendered, units=20)
        self.assertTrue(pages[0].endswith('\n'))
        self.assertEqual(''.join(validate(page) for page in pages), rendered)

    def test_short_code_block_moves_whole_to_next_page(self):
        rendered = 'a' * 15 + '\n<pre><code class="language-python">' + 'x' * 20 + '</code></pre>'
        pages = html_chunks(rendered, units=30)
        self.assertEqual(len(pages), 2)
        self.assertNotIn('<pre>', pages[0])
        self.assertIn('x' * 20, pages[1])
        self.assertEqual(''.join(validate(page) for page in pages), 'a' * 15 + '\n' + 'x' * 20)

    def test_expandable_quote_survives_every_page(self):
        rendered = '<blockquote expandable><b>trace</b>\n' + 'step &amp; 😀\n' * 70 + '</blockquote>'
        pages = html_chunks(rendered, units=43)
        self.assertTrue(all('<blockquote expandable>' in page for page in pages))
        self.assertEqual(''.join(validate(page) for page in pages), validate(rendered))


if __name__ == '__main__':
    unittest.main()
