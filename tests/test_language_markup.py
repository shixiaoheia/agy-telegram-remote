"""Known interface buttons must be translated without rewriting user labels."""
import unittest

from i18n import english_activity, english_callback_notice, localize_markup


class LanguageMarkupTests(unittest.TestCase):
    def test_buttons_are_copied_and_callback_identity_preserved(self):
        original = {"inline_keyboard": [[
            {"text": "🛑 取消任务", "callback_data": "cancel:abc"},
            {"text": "📖 查看结果", "callback_data": "history:abc"},
        ]]}
        translated = localize_markup(original, "en")
        self.assertEqual(translated["inline_keyboard"][0][0]["text"], "🛑 Cancel task")
        self.assertEqual(translated["inline_keyboard"][0][0]["callback_data"], "cancel:abc")
        self.assertEqual(original["inline_keyboard"][0][0]["text"], "🛑 取消任务")

    def test_user_history_title_is_not_translated(self):
        original = {"inline_keyboard": [[{"text": "任务：修复服务", "callback_data": "history:abc"}]]}
        self.assertEqual(localize_markup(original, "en"), original)

    def test_chinese_is_unchanged(self):
        original = {"inline_keyboard": [[{"text": "🛑 取消任务", "callback_data": "cancel:abc"}]]}
        self.assertEqual(localize_markup(original, "zh"), original)

    def test_progress_translation_keeps_command_text(self):
        self.assertEqual(english_activity("正在调用工具：read_file"), "Calling tool: read_file")
        self.assertEqual(english_activity("✅ 💻 执行命令 「echo 中文」（耗时 1.0 秒）"),
                         "✅ 💻 Executed command 「echo 中文」 (1.0 s)")

    def test_callback_notice_retains_selected_identifier(self):
        self.assertEqual(english_callback_notice("🎯 已切换至 gemini-3.8"),
                         "🎯 Switched to gemini-3.8")
        self.assertEqual(english_callback_notice("已请求取消"), "Cancellation requested")
