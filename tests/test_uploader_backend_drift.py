"""支付宝 / 百家号上传器针对现版后台的结构性回归测试。

这四处改动都来自真实失败：
- 支付宝「开启AI智能优化」浮层遮住「上传封面」，点击被拦截
- 支付宝封面弹窗是 tab 结构，图片 input 的 accept 是 ".jpg, .jpeg, .png" 而不是 image/*
- 支付宝作者声明的 radio input 是 opacity:0，check() 会超时
- 百家号创作声明点完选项后按 Escape 会把弹窗关掉，选择丢失
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger as loguru_logger

import uploader.alipay_uploader.main as alipay_main
import uploader.baijiahao_uploader.main as bjh_main
import uploader.toutiao_uploader.main as toutiao_main
import sau_cli
import sau_commands.toutiao as toutiao_command


class FakeLocator:
    """按需返回固定 count/value；所有动作记进共享 calls。"""

    def __init__(self, name, calls, count=1, value="", checked=True):
        self.name = name
        self.calls = calls
        self._count = count
        self._value = value
        self._checked = checked

    @property
    def first(self):
        return self

    def get_by_text(self, text, exact=False):
        return FakeLocator(f"{self.name}>>text:{text}", self.calls,
                           count=self._count, value=self._value, checked=self._checked)

    def filter(self, **kwargs):
        return self

    def locator(self, selector):
        return FakeLocator(f"{self.name}>>{selector}", self.calls,
                           count=self._count, value=self._value, checked=self._checked)

    async def count(self):
        return self._count

    async def is_visible(self):
        return True

    async def wait_for(self, **kwargs):
        self.calls.append(f"WAIT {self.name}")

    async def scroll_into_view_if_needed(self, **kwargs):
        self.calls.append(f"SCROLL {self.name}")

    async def click(self, **kwargs):
        self.calls.append(f"CLICK {self.name} force={kwargs.get('force', False)}")

    async def input_value(self):
        return self._value

    async def is_checked(self):
        return self._checked


class FakePage:
    """selector 前缀 → FakeLocator，便于断言「用了哪个选择器」。"""

    def __init__(self, routes, calls):
        self.routes = routes
        self.calls = calls
        self.wait_for_timeout = AsyncMock()
        self.keyboard = MagicMock()
        self.keyboard.press = AsyncMock(
            side_effect=lambda k: self.calls.append(f"KEY {k}")
        )

    def _match(self, selector):
        for prefix, loc in self.routes.items():
            if selector.startswith(prefix):
                return loc
        return FakeLocator(f"unmatched:{selector}", self.calls, count=0)

    def locator(self, selector, **kwargs):
        self.calls.append(f"LOC {selector}")
        return self._match(selector)

    def get_by_text(self, text, exact=False):
        self.calls.append(f"TEXT {text}")
        return self._match(f"TEXT:{text}")

    def get_by_role(self, role, name=None):
        self.calls.append(f"ROLE {role}:{name}")
        return self._match(f"ROLE:{name}")


class AlipayPopoverTests(unittest.IsolatedAsyncioTestCase):
    """浮层必须先关掉，否则封面的点击会被 <html> 拦截。"""

    async def test_popover_dismissed_via_skip_button(self):
        calls = []
        # 第一次 :visible 查到浮层，关掉后复查为 0
        seq = [FakeLocator("popover", calls, count=1), FakeLocator("popover", calls, count=0)]

        class SeqPage(FakePage):
            def locator(self, selector, **kwargs):
                self.calls.append(f"LOC {selector}")
                return seq.pop(0) if seq else FakeLocator("popover", self.calls, count=0)

        page = SeqPage({}, calls)
        video = MagicMock()

        await alipay_main.AlipayVideo.dismiss_ai_optimize_popover(video, page)

        self.assertTrue(any("CLICK" in c and "暂不体验" in c for c in calls), calls)

    async def test_no_popover_returns_without_clicking(self):
        calls = []
        page = FakePage({".ai-optimization-guide-popover:visible":
                         FakeLocator("popover", calls, count=0)}, calls)
        video = MagicMock()

        await alipay_main.AlipayVideo.dismiss_ai_optimize_popover(video, page)

        self.assertFalse(any(c.startswith("CLICK") for c in calls), calls)


class AlipayCoverTabTests(unittest.IsolatedAsyncioTestCase):
    """封面弹窗是「截取封面 / 上传封面」两个 tab，图片 input 用 jpg/png 后缀匹配。"""

    async def test_cover_uses_tab_and_jpg_input(self):
        calls = []
        page = FakePage({
            "input[type=\"file\"][accept*=\"jpg\"": FakeLocator("img-input", calls),
            ".antd5-modal-body .antd5-tabs-tab": FakeLocator("cover-tab", calls),
            "TEXT:上传封面": FakeLocator("cover-entry", calls),
            "ROLE:完 成": FakeLocator("done", calls),
        }, calls)
        video = MagicMock()
        video.thumbnail_path = "/tmp/cover.jpg"

        await alipay_main.AlipayVideo.upload_thumbnail(video, page)

        used = " ".join(c for c in calls if c.startswith("LOC "))
        self.assertIn("antd5-tabs-tab", used)
        # 图片 input 的 accept 是 ".jpg, .jpeg, .png"，不能按 image/* 匹配
        self.assertIn("jpg", used)
        self.assertNotIn('accept*="image"', used)
        self.assertTrue(any(c.startswith("CLICK") and "cover-tab" in c for c in calls), calls)


class AlipayAiLabelTests(unittest.IsolatedAsyncioTestCase):
    """radio input 是 opacity:0 覆盖层，必须 force 点击 label 并回读选中态。"""

    async def test_ai_label_clicked_with_force_and_verified(self):
        calls = []
        page = FakePage({
            "label.antd5-radio-wrapper": FakeLocator("ai-label", calls, checked=True),
        }, calls)
        video = MagicMock()
        video.dismiss_ai_optimize_popover = AsyncMock()

        await alipay_main.AlipayVideo.check_ai_label(video, page)

        self.assertTrue(any("CLICK ai-label" in c and "force=True" in c for c in calls), calls)

    async def test_unchecked_after_click_is_reported_as_failure(self):
        calls = []
        page = FakePage({
            "label.antd5-radio-wrapper": FakeLocator("ai-label", calls, checked=False),
        }, calls)
        video = MagicMock()
        video.dismiss_ai_optimize_popover = AsyncMock()

        messages = []
        sink = loguru_logger.add(lambda m: messages.append(m), level="WARNING")
        try:
            await alipay_main.AlipayVideo.check_ai_label(video, page)
        finally:
            loguru_logger.remove(sink)

        self.assertTrue(any("仍未选中" in str(m) for m in messages), messages)

    async def test_retries_when_first_click_blocked_by_popover(self):
        """封面弹窗关闭后浮层会再次弹出，第一次点击可能不生效，必须重试到选中。"""
        calls = []

        class FlakyLabel(FakeLocator):
            """前两次点击没生效，第三次才选中。"""

            def __init__(self):
                super().__init__("ai-label", calls, checked=False)
                self.clicks = 0

            async def click(self, **kwargs):
                self.clicks += 1
                self._checked = self.clicks >= 3
                calls.append(f"CLICK ai-label force={kwargs.get('force', False)}")

        page = FakePage({"label.antd5-radio-wrapper": FlakyLabel()}, calls)
        video = MagicMock()
        video.dismiss_ai_optimize_popover = AsyncMock()

        await alipay_main.AlipayVideo.check_ai_label(video, page)

        clicks = [c for c in calls if c.startswith("CLICK ai-label")]
        self.assertEqual(len(clicks), 3, calls)


class BaijiahaoDeclarationTests(unittest.IsolatedAsyncioTestCase):
    """创作声明：点完选项必须点「确定」，绝不能按 Escape。"""

    def _page(self, calls, trigger_value="含AI生成内容", modal_left=0):
        return FakePage({
            'input[placeholder="请选择创作声明"]':
                FakeLocator("trigger", calls, value=trigger_value),
            ".cheetah-modal-content":
                FakeLocator("modal", calls, count=modal_left),
            "TEXT:含AI生成内容":
                FakeLocator("ai-option", calls),
        }, calls)

    async def test_confirm_clicked_and_never_escape(self):
        calls = []
        page = self._page(calls)
        video = MagicMock()

        await bjh_main.BaiJiaHaoVideo._check_ai_declaration(video, page)

        self.assertTrue(any(c.startswith("CLICK") and "trigger" in c for c in calls), calls)
        # 关键回归点：不能再按 Escape，否则弹窗关闭、选择丢弃
        self.assertFalse(any("KEY Escape" in c for c in calls), calls)
        self.assertTrue(any("ui-dialog-confirm" in c or "确定" in c for c in calls)
                        or any(c.startswith("LOC ") and "确定" in c for c in calls), calls)

    async def test_missing_value_in_form_raises_warning(self):
        calls = []
        page = self._page(calls, trigger_value="")
        video = MagicMock()

        messages = []
        sink = loguru_logger.add(lambda m: messages.append(m), level="WARNING")
        try:
            await bjh_main.BaiJiaHaoVideo._check_ai_declaration(video, page)
        finally:
            loguru_logger.remove(sink)

        self.assertTrue(any("未写入表单" in str(m) for m in messages), messages)


class BaijiahaoPublishRetryTests(unittest.IsolatedAsyncioTestCase):
    """上传到 100% ≠ 转码完成；被判「确保视频已经上传完毕」时要重试发布，不能干等到超时。"""

    async def test_retries_publish_while_video_still_processing(self):
        calls = []
        publishes = []

        class ToastLocator:
            def __init__(self, text, count=1):
                self.text = text
                self._count = count

            @property
            def first(self):
                return self

            def locator(self, selector, **kwargs):
                return self

            async def count(self):
                return self._count

            async def is_visible(self):
                return True

            async def inner_text(self):
                return self.text

        class PublishBtn:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def wait_for(self, **kwargs):
                calls.append("BTN wait")

            async def click(self, **kwargs):
                publishes.append(kwargs.get("force"))
                calls.append("BTN click")

        class RetryPage:
            """前 3 次查询提示「确保视频已经上传完毕」，之后跳转到成功页。"""

            def __init__(self):
                self.url = "https://baijiahao.baidu.com/builder/rc/edit?type=videoV2"
                self.polls = 0
                self.keyboard = MagicMock()
                self.keyboard.press = AsyncMock()

            async def wait_for_timeout(self, ms):
                self.polls += 1
                # 轮询够多轮（节流阈值 15s 按轮询次数折算）后跳转到成功页
                if self.polls > 30:
                    self.url = "https://baijiahao.baidu.com/builder/rc/content"

            def locator(self, selector, **kwargs):
                if selector.startswith('[data-testid="publish-btn"]'):
                    return PublishBtn()
                if "cheetah-message" in selector:
                    return ToastLocator("确保视频已经上传完毕")
                # 残留弹窗 / 百度安全验证等一律不存在
                return ToastLocator("", count=0)

        page = RetryPage()
        video = MagicMock()
        # 让节流判定每次都放行
        with patch.object(bjh_main.time, "monotonic", side_effect=[0] + list(range(1000))):
            try:
                await bjh_main.BaiJiaHaoVideo._submit_publish(video, page)
            except RuntimeError:
                pass

        # 关键点：等不到跳转时必须重试点击，而不是只点一次
        self.assertGreaterEqual(len(publishes), 2, calls)


class ToutiaoImmediatePublishSelectorTests(unittest.IsolatedAsyncioTestCase):
    """立即发布不能误命中同样含「发布」的定时发布按钮。"""

    async def test_uses_submit_button_not_schedule_button(self):
        calls = []

        class PublishButton:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                calls.append("WAIT submit")

            async def click(self, **kwargs):
                calls.append("CLICK submit")
                page.url = "https://mp.toutiao.com/profile_v4/manage/content/all"

        class MissingButton:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                calls.append("WAIT unexpected")

            async def click(self, **kwargs):
                calls.append("CLICK unexpected")

            async def count(self):
                return 0

        class PublishPage:
            url = "https://mp.toutiao.com/profile_v4/xigua/upload-video"

            def locator(self, selector, **kwargs):
                calls.append(f"LOC {selector}")
                if selector == ".action-footer-btn.submit":
                    return PublishButton()
                return MissingButton()

            async def wait_for_timeout(self, ms):
                calls.append(f"WAIT {ms}")

        page = PublishPage()
        video = MagicMock()

        # 用正确立即发布按钮时，页面应离开上传页，不应打开定时发布弹窗。
        with patch.object(toutiao_main.time, "monotonic", side_effect=range(200)):
            await toutiao_main.ToutiaoVideo._submit_publish(video, page)

        self.assertIn("LOC .action-footer-btn.submit", calls)
        self.assertIn("CLICK submit", calls)
        self.assertNotIn("CLICK unexpected", calls)

    async def test_page_validation_error_stops_after_one_submit_click(self):
        class PublishButton:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                pass

            async def click(self, **kwargs):
                page.submit_clicks += 1

        class PublishPage:
            url = "https://mp.toutiao.com/profile_v4/xigua/upload-video"

            def __init__(self):
                self.submit_clicks = 0

            def locator(self, selector, **kwargs):
                return PublishButton()

            async def inner_text(self, selector):
                self.assertEqual(selector, "body")
                return "请选择视频分类"

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError(f"{left!r} != {right!r}")

            async def wait_for_timeout(self, ms):
                raise AssertionError("页面已给出校验错误，不应继续等待")

        page = PublishPage()
        with self.assertRaisesRegex(RuntimeError, "请选择视频分类"):
            await toutiao_main.ToutiaoVideo._submit_publish(MagicMock(), page)
        self.assertEqual(page.submit_clicks, 1)

    async def test_unknown_submit_result_never_clicks_publish_twice(self):
        class PublishButton:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                pass

            async def click(self, **kwargs):
                page.submit_clicks += 1

            async def count(self):
                return 0

        class PublishPage:
            url = "https://mp.toutiao.com/profile_v4/xigua/upload-video"

            def __init__(self):
                self.submit_clicks = 0

            def locator(self, selector, **kwargs):
                return PublishButton()

            async def inner_text(self, selector):
                return ""

            async def wait_for_timeout(self, ms):
                pass

        page = PublishPage()
        with patch.object(toutiao_main.time, "monotonic", side_effect=range(200)):
            with self.assertRaises(toutiao_main.ToutiaoPublishUnknownError):
                await toutiao_main.ToutiaoVideo._submit_publish(MagicMock(), page)
        self.assertEqual(page.submit_clicks, 1)


class ToutiaoCliDefaultsTests(unittest.TestCase):
    """头条号二维码会自动刷新，login 默认必须打开可见浏览器窗口。"""

    def test_login_defaults_to_headed_but_upload_defaults_to_headless(self):
        parser = sau_cli.build_parser()

        login_args = parser.parse_args(["toutiao", "login", "--account", "erjia"])
        upload_args = parser.parse_args([
            "toutiao", "upload-video", "--account", "erjia", "--file", __file__, "--title", "标题",
        ])

        self.assertFalse(login_args.headless)
        self.assertTrue(upload_args.headless)


class ToutiaoCliMetadataTests(unittest.TestCase):
    """发布元数据必须来自单一来源，并且分类不能落回平台默认值。"""

    def test_direct_upload_without_category_is_rejected_before_browser_starts(self):
        parser = sau_cli.build_parser()
        args = parser.parse_args([
            "toutiao", "upload-video", "--account", "erjia", "--file", __file__,
            "--title", "标题", "--desc", "简介", "--tags", "典故",
        ])

        with self.assertRaisesRegex(ValueError, "--category"):
            toutiao_command.build_toutiao_upload_request(args)

    def test_metadata_file_builds_complete_request_without_reusing_cli_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "video.mp4"
            video.touch()
            metadata = folder / "publish.json"
            metadata.write_text(json.dumps({
                "video_file": "video.mp4",
                "title": "朝三暮四：3加4与4加3为何不同",
                "description": "框架效应会改变选择。",
                "tags": ["朝三暮四", "框架效应"],
                "category": "文化",
                "generate_article": False,
            }), encoding="utf-8")

            request = toutiao_command.load_toutiao_metadata(metadata, "erjia")

        self.assertEqual(request.video_file, video.resolve())
        self.assertEqual(request.title, "朝三暮四：3加4与4加3为何不同")
        self.assertEqual(request.description, "框架效应会改变选择。")
        self.assertEqual(request.tags, ["朝三暮四", "框架效应"])
        self.assertEqual(request.category, "文化")
        self.assertFalse(request.generate_article)

    def test_metadata_mode_rejects_direct_metadata_flags(self):
        parser = sau_cli.build_parser()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video = folder / "video.mp4"
            video.touch()
            metadata = folder / "publish.json"
            metadata.write_text(json.dumps({
                "video_file": "video.mp4", "title": "标题", "description": "简介",
                "tags": ["典故"], "category": "文化",
            }), encoding="utf-8")
            args = parser.parse_args([
                "toutiao", "upload-video", "--account", "erjia", "--metadata", str(metadata),
                "--title", "不应混用",
            ])
            with self.assertRaisesRegex(ValueError, "--metadata"):
                toutiao_command.build_toutiao_upload_request(args)


class ToutiaoFormReadbackTests(unittest.IsolatedAsyncioTestCase):
    """头条号表单的选择/写入操作必须回读控件真实状态。"""

    async def test_title_is_written_and_read_back(self):
        calls = []

        class TitleField:
            @property
            def first(self):
                return self

            async def wait_for(self, **kwargs):
                calls.append("WAIT title")

            async def click(self):
                calls.append("CLICK title")

            async def fill(self, value):
                self.value = value
                calls.append(f"FILL {value}")

            async def input_value(self):
                return self.value

            value = ""

        class TitlePage:
            def __init__(self):
                self.field = TitleField()
                self.keyboard = MagicMock()
                self.keyboard.press = AsyncMock(side_effect=lambda key: calls.append(f"KEY {key}"))

            def locator(self, selector, **kwargs):
                self.assertEqual(selector, toutiao_main.TITLE_SELECTOR)
                return self.field

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError(f"{left!r} != {right!r}")

            async def wait_for_timeout(self, ms):
                pass

        page = TitlePage()
        video = MagicMock()
        video.title = "江雪：他钓的不是鱼，是一整个冬天"
        video.max_title_length = 30

        await toutiao_main.ToutiaoVideo._fill_title(video, page)

        self.assertEqual(page.field.value, video.title)
        self.assertIn(f"FILL {video.title}", calls)

    async def test_description_readback_mismatch_stops_before_submit(self):
        class DescriptionField:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def wait_for(self, **kwargs):
                pass

            async def scroll_into_view_if_needed(self):
                pass

            async def fill(self, value):
                self.filled = value

            async def input_value(self):
                return "上一条视频的简介"

        class DescriptionPage:
            def locator(self, selector, **kwargs):
                self.assertEqual(selector, toutiao_main.DESC_SELECTOR)
                return DescriptionField()

            def assertEqual(self, left, right):
                if left != right:
                    raise AssertionError(f"{left!r} != {right!r}")

            async def wait_for_timeout(self, ms):
                pass

        video = MagicMock()
        video.desc = "《庄子·齐物论》的朝三暮四。"
        with self.assertRaisesRegex(RuntimeError, "视频简介未写入表单"):
            await toutiao_main.ToutiaoVideo._fill_desc(video, DescriptionPage())

    async def test_category_readback_mismatch_stops_before_submit(self):
        class CategoryControl:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self, **kwargs):
                pass

        class CategorySection:
            def __init__(self):
                self.control = CategoryControl()

            @property
            def first(self):
                return self

            def locator(self, selector):
                return self.control

            async def inner_text(self):
                return "分类\n默认分类"

        class CategoryOption:
            @property
            def last(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self, **kwargs):
                pass

        class CategoryLabel:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            def locator(self, selector):
                return CategorySection()

        class CategoryPage:
            def get_by_text(self, text, exact=False):
                return CategoryLabel() if text == "分类" else CategoryOption()

        video = MagicMock()
        video.category = "文化"
        with self.assertRaisesRegex(RuntimeError, "视频分类未写入表单"):
            await toutiao_main.ToutiaoVideo._select_category(video, CategoryPage())

    async def test_ai_declaration_requires_checked_class_after_click(self):
        calls = []

        class DeclarationLabel:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def scroll_into_view_if_needed(self):
                calls.append("SCROLL AI")

            async def click(self, **kwargs):
                self.checked = True
                calls.append("CLICK AI")

            async def get_attribute(self, name):
                return "byte-checkbox checkbot-item byte-checkbox-checked" if self.checked else "byte-checkbox checkbot-item"

            checked = False

        class DeclarationPage:
            def __init__(self):
                self.label = DeclarationLabel()

            def locator(self, selector, **kwargs):
                calls.append(f"LOC {selector}")
                return self.label

            async def wait_for_timeout(self, ms):
                pass

        page = DeclarationPage()
        video = MagicMock()

        await toutiao_main.ToutiaoVideo._check_ai_declaration(video, page)

        self.assertIn('LOC label.byte-checkbox.checkbot-item:has-text("AI生成")', calls)
        self.assertIn("CLICK AI", calls)
        self.assertTrue(page.label.checked)

    async def test_topic_is_retried_until_tag_count_increases(self):
        """React 偶尔吞掉第一次 Enter，日志必须以真实标签数而不是尝试数计。"""
        calls = []

        class TopicField:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def scroll_into_view_if_needed(self):
                calls.append("SCROLL topic")

            async def click(self):
                calls.append("CLICK topic")

            async def fill(self, value):
                calls.append(f"FILL {value}")

        class TagList:
            @property
            def first(self):
                return self

            async def count(self):
                return page.tag_count

            async def all_inner_texts(self):
                return ["江雪"] if page.tag_count else []

        class TopicPage:
            def __init__(self):
                self.tag_count = 0
                self.enter_count = 0
                self.field = TopicField()
                self.tags = TagList()
                self.keyboard = MagicMock()
                self.keyboard.press = AsyncMock(side_effect=self.press)

            async def press(self, key):
                calls.append(f"KEY {key}")
                if key == "Enter":
                    self.enter_count += 1
                    # 模拟后台首次 Enter 被 React 状态更新吞掉。
                    if self.enter_count == 2:
                        self.tag_count += 1

            def locator(self, selector, **kwargs):
                if selector == toutiao_main.TOPIC_SELECTOR:
                    return self.field
                if selector == ".arco-input-tag-tag":
                    return self.tags
                raise AssertionError(f"unexpected selector: {selector}")

            async def wait_for_timeout(self, ms):
                pass

        page = TopicPage()
        video = MagicMock()
        video.tags = ["江雪"]

        await toutiao_main.ToutiaoVideo._fill_topics(video, page)

        self.assertEqual(page.tag_count, 1)
        self.assertEqual(calls.count("KEY Enter"), 2, calls)

    async def test_missing_topic_blocks_publish_instead_of_only_logging_warning(self):
        class TopicField:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def scroll_into_view_if_needed(self):
                pass

            async def click(self):
                pass

            async def fill(self, value):
                pass

        class TagList:
            async def count(self):
                return 0

        class TopicPage:
            def __init__(self):
                self.keyboard = MagicMock()
                self.keyboard.press = AsyncMock()

            def locator(self, selector, **kwargs):
                if selector == toutiao_main.TOPIC_SELECTOR:
                    return TopicField()
                if selector == ".arco-input-tag-tag":
                    return TagList()
                raise AssertionError(f"unexpected selector: {selector}")

            async def wait_for_timeout(self, ms):
                pass

        video = MagicMock()
        video.tags = ["朝三暮四"]
        with self.assertRaisesRegex(RuntimeError, "话题未写入表单"):
            await toutiao_main.ToutiaoVideo._fill_topics(video, TopicPage())

    async def test_wrong_topic_chip_text_blocks_publish(self):
        class TopicField:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def scroll_into_view_if_needed(self):
                pass

            async def click(self):
                pass

            async def fill(self, value):
                pass

        class TagList:
            def __init__(self):
                self.count_calls = 0

            async def count(self):
                self.count_calls += 1
                return 0 if self.count_calls == 1 else 1

            async def all_inner_texts(self):
                return ["上一条视频的话题"]

        class TopicPage:
            def __init__(self):
                self.keyboard = MagicMock()
                self.keyboard.press = AsyncMock()
                self.tags = TagList()

            def locator(self, selector, **kwargs):
                if selector == toutiao_main.TOPIC_SELECTOR:
                    return TopicField()
                if selector == ".arco-input-tag-tag":
                    return self.tags
                raise AssertionError(f"unexpected selector: {selector}")

            async def wait_for_timeout(self, ms):
                pass

        video = MagicMock()
        video.tags = ["朝三暮四"]
        with self.assertRaisesRegex(RuntimeError, "话题未写入表单"):
            await toutiao_main.ToutiaoVideo._fill_topics(video, TopicPage())


if __name__ == "__main__":
    unittest.main()
