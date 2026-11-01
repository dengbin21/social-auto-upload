# -*- coding: utf-8 -*-
"""头条号（今日头条创作平台）视频上传 + 扫码登录。

为什么单独做：`sau` 原本 17 个平台里没有头条号，而头条号是唯一「零粉丝门槛、
播放量直接分成」的平台（开通视频创作收益只要信用分 100，无粉丝要求），
见 `pyproject` 之外的业务说明与 SOP 坑库 E75。

功能：
  - toutiao_cookie_gen: 扫码登录（headed 由用户扫窗口内的码，二维码会自动刷新）
  - cookie_auth: 验证 cookie 是否有效
  - toutiao_setup: 统一入口（检查/触发登录）
  - ToutiaoVideo: 视频上传类
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from pathlib import Path

from patchright.async_api import Page, Playwright, async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.log import toutiao_logger

TOUTIAO_LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
TOUTIAO_HOME_URL = "https://mp.toutiao.com/profile_v4/index"
TOUTIAO_PUBLISH_URL = "https://mp.toutiao.com/profile_v4/xigua/upload-video"
TOUTIAO_CONTENT_URL = "https://mp.toutiao.com/profile_v4/manage/content/all"

# 表单控件（实测 selector，见 SOP E75）
TITLE_SELECTOR = 'input.xigua-input.show-limit'
TOPIC_SELECTOR = 'input.arco-input-tag-input'
DESC_SELECTOR = 'textarea.byte-textarea.abstract'
DECLARATION_SELECTOR = 'label.byte-checkbox.checkbot-item'


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _build_login_result(success: bool, status: str, message: str, account_file: str, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "current_url": current_url,
    }


def _build_launch_kwargs(headless: bool) -> dict:
    launch_kwargs = {"headless": headless}
    if LOCAL_CHROME_PATH:
        launch_kwargs["executable_path"] = LOCAL_CHROME_PATH
    return launch_kwargs


def _resolve_account_file(account_file: str | Path) -> str:
    path = Path(account_file).expanduser()
    if path.is_absolute():
        return str(path)
    if len(path.parts) == 1:
        return str((Path(BASE_DIR) / "cookies" / "toutiao_uploader" / path).resolve())
    return str(path.resolve())


async def _switch_to_qr_tab(page: Page) -> None:
    """登录页默认可能是「验证码登录」，切到「扫码登录」。"""
    try:
        tab = page.get_by_text("扫码登录", exact=True).first
        if await tab.count():
            await tab.click(timeout=5000)
            await page.wait_for_timeout(2000)
    except Exception:
        pass


async def _save_qr_from_dom(page: Page, qrcode_path: Path) -> bool:
    """把页面里的 data-url 二维码落成图片。

    注意：头条二维码刷新很快（实测静态图几十秒后扫码会提示「已过期」），
    所以这条路径只给 headless 场景兜底，正常应走 headed 让用户扫窗口内的码。
    """
    for _ in range(20):
        try:
            src = await page.evaluate(
                """() => {
                    const imgs = [...document.querySelectorAll('img')];
                    const hit = imgs.find(i => (i.src || '').startsWith('data:image/')
                        && i.naturalWidth >= 200);
                    return hit ? hit.src : null;
                }"""
            )
        except Exception:
            src = None
        if src:
            m = re.match(r"data:image/(png|jpeg);base64,(.+)", src)
            if m:
                qrcode_path.parent.mkdir(parents=True, exist_ok=True)
                qrcode_path.write_bytes(base64.b64decode(m.group(2)))
                return True
        await page.wait_for_timeout(1000)
    return False


async def _is_login_completed(page: Page) -> bool:
    """离开 login 页即视为登录完成。"""
    return "login" not in page.url.lower()


async def toutiao_cookie_gen(account_file, poll_interval: int = 2, max_checks: int = 150,
                             headless: bool = False):
    """扫码登录头条号并保存 storage_state。

    headless=False 时用户直接扫浏览器窗口内的二维码——窗口内二维码由页面自行刷新，
    不会出现「贴出去的静态图已过期」问题。
    """
    account_file = _resolve_account_file(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)
    result = _build_login_result(False, "failed", "头条号登录失败", account_file)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=headless))
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = None
        try:
            page = await context.new_page()
            await page.goto(TOUTIAO_LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await _switch_to_qr_tab(page)

            if headless:
                qrcode_path = Path(account_file).with_suffix(".login_qrcode.png")
                if await _save_qr_from_dom(page, qrcode_path):
                    toutiao_logger.warning(_msg("🖼️", f"二维码已保存到: {qrcode_path}（有效期很短，请立刻扫）"))
                else:
                    toutiao_logger.warning(_msg("😵", "未能提取二维码，建议改用 --headed"))
            else:
                toutiao_logger.info(_msg("🧍", "请在弹出的浏览器窗口内扫码（窗口内二维码会自动刷新）"))

            for _ in range(max_checks):
                if await _is_login_completed(page):
                    toutiao_logger.info(_msg("🥳", f"扫码成功，已跳转到: {page.url}"))
                    result = _build_login_result(True, "success", "头条号扫码登录成功", account_file, page.url)
                    break
                await page.wait_for_timeout(poll_interval * 1000)
            else:
                result = _build_login_result(False, "timeout", "等待头条号扫码登录超时", account_file, page.url)

            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                toutiao_logger.success(_msg("🥳", f"cookie 已保存: {account_file}"))
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file,
                                         page.url if page else "")
        finally:
            if not result["success"]:
                toutiao_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
    return result


async def cookie_auth(account_file):
    """验证 cookie：访问后台首页，被重定向到 login 即失效。"""
    account_file = _resolve_account_file(account_file)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=True))
        try:
            context = await browser.new_context(storage_state=account_file)
            page = await context.new_page()
            await page.goto(TOUTIAO_HOME_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)
            if "login" in page.url.lower():
                toutiao_logger.info(_msg("🥹", "cookie 已失效（跳转到登录页）"))
                return False
            toutiao_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            toutiao_logger.warning(_msg("😵", f"cookie 校验出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def toutiao_setup(account_file, handle=False, return_detail=False, headless: bool = False):
    """统一入口：检查 cookie → 如无效且 handle=True 则触发扫码登录。"""
    account_file = _resolve_account_file(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie 文件不存在或已失效", account_file)
            return result if return_detail else False
        toutiao_logger.info(_msg("🥹", "cookie 文件不存在或已失效，打开浏览器请扫码登录"))
        result = await toutiao_cookie_gen(account_file, headless=headless)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie 有效", account_file)
    return result if return_detail else True


class ToutiaoVideo(BaseVideoUploader):
    """头条号视频上传。

    流程：打开发布页 → 选视频 → 等表单 → 关引导弹窗 → 填标题/简介/话题
          → 勾「生成图文」（额外图文收益）→ 勾「AI生成」声明 → 上传封面
          → 等上传完成 → 点发布 → 确认成功。
    """

    def __init__(
        self,
        title,
        file_path,
        tags,
        account_file,
        publish_date=0,
        desc: str | None = None,
        thumbnail_path: str | None = None,
        generate_article: bool = True,
        debug: bool = True,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.account_file = _resolve_account_file(account_file)
        self.publish_date = publish_date
        self.desc = desc or ""
        self.thumbnail_path = thumbnail_path
        self.generate_article = generate_article
        self.debug = debug
        self.headless = headless
        self.local_executable_path = LOCAL_CHROME_PATH
        self.max_title_length = 30

    async def validate_upload_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成头条号登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成头条号登录: {self.account_file}")
        if not self.title or not str(self.title).strip():
            raise ValueError("视频标题不能为空")
        if len(self.title) > self.max_title_length:
            raise ValueError(f"头条号标题最多 {self.max_title_length} 字，当前 {len(self.title)} 字")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def upload(self, playwright: Playwright) -> None:
        toutiao_logger.info(_msg("🧍", "先检查 cookie 和视频文件"))
        await self.validate_upload_args()
        toutiao_logger.info(_msg("🥳", "上传前检查通过"))

        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=self.headless))
        context = await browser.new_context(storage_state=self.account_file)
        try:
            page = await context.new_page()
            await page.goto(TOUTIAO_PUBLISH_URL, timeout=120000, wait_until="domcontentloaded")
            await page.wait_for_timeout(6000)

            await self.upload_video_file(page)
            await self._wait_form_ready(page)
            await self._dismiss_guide_popover(page)
            # 先把视频传完再动表单：上传中途操作其它控件容易把状态搞乱
            await self._wait_upload_complete(page)
            await self._fill_title(page)
            await self._fill_desc(page)
            await self._fill_topics(page)
            if self.generate_article:
                await self._enable_article_generation(page)
            await self._check_ai_declaration(page)
            if self.thumbnail_path:
                await self._upload_thumbnail(page)
            await self._submit_publish(page)

            await context.storage_state(path=self.account_file)
            toutiao_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await context.close()
            await browser.close()

    async def upload_video_file(self, page: Page) -> None:
        file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=30000)
        await file_input.set_input_files(self.file_path)
        toutiao_logger.info(_msg("🏃", f"已选择视频文件: {self.file_path}"))

    async def _wait_form_ready(self, page: Page, timeout: int = 180) -> None:
        """等发布表单渲染（「基本信息」标题出现）。"""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                if await page.locator(TITLE_SELECTOR).count():
                    toutiao_logger.info(_msg("🥳", "发布表单已就绪"))
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
        raise RuntimeError(f"发布表单在 {timeout}s 内未渲染")

    async def _dismiss_guide_popover(self, page: Page) -> None:
        """关掉「视频生成图文，同时发布得额外收益」引导弹窗（会遮挡表单）。"""
        for _ in range(2):
            try:
                ok = page.get_by_text("我知道了", exact=True).first
                if await ok.count() and await ok.is_visible():
                    await ok.click(timeout=5000)
                    await page.wait_for_timeout(1200)
                    return
            except Exception:
                await page.wait_for_timeout(800)

    async def _fill_title(self, page: Page) -> None:
        field = page.locator(TITLE_SELECTOR).first
        await field.wait_for(state="visible", timeout=30000)
        title = self.title[: self.max_title_length]
        await field.click()
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.press("Backspace")
        await field.fill(title)
        await page.wait_for_timeout(500)
        if (await field.input_value()).strip() != title:
            raise RuntimeError("标题未写入表单")
        toutiao_logger.info(_msg("🏷️", f"标题已填写: {title}"))

    async def _fill_desc(self, page: Page) -> None:
        if not self.desc:
            return
        field = page.locator(DESC_SELECTOR).first
        if not await field.count():
            toutiao_logger.warning(_msg("😵", "未找到视频简介输入框，跳过"))
            return
        await field.scroll_into_view_if_needed()
        await field.fill(self.desc[:400])
        await page.wait_for_timeout(500)
        toutiao_logger.info(_msg("🏷️", f"视频简介已填写: {self.desc[:30]}..."))

    async def _fill_topics(self, page: Page) -> None:
        """话题通过 arco-input-tag 输入，回车逐个添加。"""
        if not self.tags:
            return
        field = page.locator(TOPIC_SELECTOR).first
        if not await field.count():
            toutiao_logger.warning(_msg("😵", "未找到话题输入框，跳过话题"))
            return
        chips = page.locator(".arco-input-tag-tag")
        added = 0
        for tag in self.tags:
            try:
                before = await chips.count()
                for _ in range(2):
                    await field.scroll_into_view_if_needed()
                    await field.click()
                    await field.fill(tag)
                    await page.wait_for_timeout(800)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(600)
                    if await chips.count() > before:
                        added += 1
                        break
                else:
                    toutiao_logger.warning(_msg("⚠️", f"话题「{tag}」未写入表单，跳过"))
            except Exception as exc:
                toutiao_logger.warning(_msg("⚠️", f"话题「{tag}」添加失败: {exc}"))
        toutiao_logger.info(_msg("🏷️", f"已添加 {added} 个话题"))

    async def _enable_article_generation(self, page: Page) -> None:
        """勾选「生成图文」：与视频同时发布，额外获得图文创作收益。"""
        try:
            label = page.get_by_text("生成图文", exact=True).first
            if not await label.count():
                toutiao_logger.warning(_msg("😵", "未找到「生成图文」选项，跳过"))
                return
            await label.scroll_into_view_if_needed()
            await label.click(timeout=8000)
            await page.wait_for_timeout(800)
            toutiao_logger.success(_msg("🧾", "已勾选「生成图文」（额外图文收益）"))
        except Exception as exc:
            toutiao_logger.warning(_msg("⚠️", f"勾选「生成图文」失败，跳过: {exc}"))

    async def _check_ai_declaration(self, page: Page) -> None:
        """作品声明勾选「AI生成」。

        label 是 byte-checkbox，选中后 class 会加 byte-checkbox-checked，
        必须回读确认——点了没勾上，头条会按"未声明"处理。
        """
        label = page.locator(f'{DECLARATION_SELECTOR}:has-text("AI生成")').first
        try:
            if not await label.count():
                toutiao_logger.warning(_msg("😵", "未找到「AI生成」作品声明选项"))
                return
            await label.scroll_into_view_if_needed()
            for attempt in range(3):
                await label.click(timeout=8000)
                await page.wait_for_timeout(800)
                cls = (await label.get_attribute("class")) or ""
                if "byte-checkbox-checked" in cls:
                    toutiao_logger.success(_msg("🏷️", "已勾选作品声明「AI生成」"))
                    return
                toutiao_logger.info(_msg("🔁", f"作品声明未勾上，重试（{attempt + 1}/3）"))
            raise RuntimeError("点击后「AI生成」仍未勾选")
        except Exception as exc:
            toutiao_logger.warning(_msg("😵", f"勾选作品声明失败: {exc}"))

    async def _upload_thumbnail(self, page: Page) -> None:
        """自定义封面。

        实测流程（SOP E75）：点「上传封面」→ 弹窗默认停在「封面截取」tab，
        此时 DOM 里没有图片 input，必须先点「本地上传」tab 才会出现
        `input[type=file][accept*=image]` → 选图 → 点 `button.btn-sure`（进封面编辑器）
        → 再点 `button.btn-sure` → 弹「完成后无法继续编辑」确认框 → 点 `button.m-button.red`。

        失败不阻塞发布（头条会自动截帧），但必须把弹窗关掉，否则挡住「发布」。
        """
        try:
            entry = page.get_by_text("上传封面", exact=True).first
            if not await entry.count():
                toutiao_logger.warning(_msg("😵", "未找到「上传封面」入口，使用系统封面"))
                return
            await entry.scroll_into_view_if_needed()
            await entry.click(timeout=10000)
            await page.wait_for_timeout(2500)

            # 关键：先切到「本地上传」tab，图片 input 才会被挂载
            local_tab = page.get_by_text("本地上传", exact=True).first
            await local_tab.wait_for(state="visible", timeout=15000)
            await local_tab.click(timeout=8000)
            await page.wait_for_timeout(2000)

            img_input = page.locator('input[type="file"][accept*="image"]').first
            await img_input.wait_for(state="attached", timeout=15000)
            await img_input.set_input_files(self.thumbnail_path)
            await page.wait_for_timeout(3000)

            # 确认链路：编辑器的「确定」→「完成后无法继续编辑」确认框的「确定」
            for _ in range(4):
                if await self._click_if_visible(page, 'button.m-button.red'):
                    break
                if await self._click_if_visible(page, 'button.btn-sure'):
                    continue
                break

            await page.wait_for_timeout(1500)
            body = await page.inner_text("body")
            if "重选封面" in body or "替换" in body:
                toutiao_logger.success(_msg("🖼️", "封面已上传"))
            else:
                toutiao_logger.warning(_msg("⚠️", "封面确认后未检测到封面已设置，使用系统封面"))
        except Exception as exc:
            toutiao_logger.warning(_msg("⚠️", f"封面上传失败，使用系统封面: {exc}"))
            await self._close_blocking_overlay(page)

    async def _click_if_visible(self, page: Page, selector: str) -> bool:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=8000)
                await page.wait_for_timeout(2500)
                return True
        except Exception:
            return False
        return False

    async def _close_blocking_overlay(self, page: Page) -> None:
        """封面失败时关掉残留弹窗，避免挡住「发布」按钮。"""
        for _ in range(2):
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(600)
            except Exception:
                return

    async def _wait_upload_complete(self, page: Page, timeout: int = 900) -> None:
        """等视频上传结束：页面不再出现「上传中」/「已上传: x/y」。"""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                body = await page.inner_text("body")
            except Exception:
                body = ""
            if "上传失败" in body:
                raise RuntimeError("视频上传失败")
            if "上传中" not in body and "已上传:" not in body:
                toutiao_logger.success(_msg("🥳", "视频上传完毕"))
                return
            m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", body)
            if m:
                toutiao_logger.info(_msg("🏃", f"上传中 {m.group(1)}%"))
            await asyncio.sleep(3)
        raise RuntimeError(f"视频上传 {timeout}s 未完成")

    async def _submit_publish(self, page: Page) -> None:
        # 「定时发布」同样包含“发布”，文本模糊匹配会误打开定时弹窗。
        # 立即发布按钮在头条后台的实测 class 为 action-footer-btn submit。
        publish_btn = page.locator(".action-footer-btn.submit").first
        await publish_btn.wait_for(state="visible", timeout=15000)
        await publish_btn.click(force=True)
        toutiao_logger.info(_msg("🏃", "已点击发布按钮"))

        start = time.monotonic()
        while time.monotonic() - start < 180:
            url = page.url
            if "upload-video" not in url:
                toutiao_logger.success(_msg("🥳", "视频发布成功"))
                return
            body = ""
            try:
                body = await page.inner_text("body")
            except Exception:
                pass
            for kw in ("发布成功", "提交成功", "审核中"):
                if kw in body:
                    toutiao_logger.success(_msg("🥳", f"视频发布成功（{kw}）"))
                    return
            await page.wait_for_timeout(1500)

        if await page.locator('text="发布成功"').count():
            toutiao_logger.success(_msg("🥳", "视频发布成功"))
            return
        raise RuntimeError(f"发布后未检测到成功信号，当前 URL: {page.url}")

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
