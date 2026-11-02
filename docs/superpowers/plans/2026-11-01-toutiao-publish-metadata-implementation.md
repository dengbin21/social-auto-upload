# 头条号发布元数据防错与提交确认修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让头条号视频发布在提交前验证正确的作品元数据与分类，并只在得到明确平台成功证据时报告发布成功。

**Architecture:** 保留现有逐参数 CLI 以兼容调用方，但将 `--category` 变为必填并新增 `--metadata` JSON 清单模式。元数据清单是全量字段的唯一来源，命令不允许清单与单字段混用。上传器将元数据填写拆成可回读的方法；最终提交只点击一次，并将成功、表单校验失败与未知提交结果分开处理。

**Tech Stack:** Python 3.12、argparse、dataclasses、Patchright async API、unittest、JSON。

## Global Constraints

- 修改实际被 `sau` 入口导入的 `/Users/robin/tools/social-auto-upload`，不修改临时副本。
- 不运行 `upload-video`，不重新发布、编辑或删除 `zhaosan` 的现有作品。
- 不改支付宝或其他平台上传器。
- 分类从头条实际发布表单读取和回读，不写死实时分类列表。
- `--metadata` 模式的 `video_file`、`title`、`description`、`tags`、`category` 必须完整；可选 `thumbnail_file`、`generate_article`。
- 直接参数模式保留 `--file`、`--title`、`--desc`、`--tags`、`--thumbnail`、`--no-article`，并新增必填 `--category`。
- 发生字段写入、分类选择、页面校验或发布结果未知时必须失败，且未知结果不得再次点击发布。
- 实际目录不是 Git 工作树，无法创建 Git commit；不得为满足提交要求而初始化、迁移或覆盖该目录的版本控制。

---

## File Structure

- Modify: `sau_commands/toutiao.py` — CLI 参数、元数据文件加载与请求构建。
- Modify: `uploader/toutiao_uploader/main.py` — 分类发现/选择、字段回读和提交结果状态判定。
- Modify: `tests/test_uploader_backend_drift.py` — CLI、表单回读和单次提交的回归测试。
- Create: `/Users/robin/Desktop/deepseek_harnes/zhaosan/toutiao-publish.json` — 已纠正、仅供未来复用的 `zhaosan` 发布清单；不触发发布。
- Create: `docs/superpowers/specs/2026-11-01-toutiao-publish-metadata-design.md` — 已完成的修复设计。

### Task 1: CLI 元数据清单与强制分类

**Files:**
- Modify: `sau_commands/toutiao.py:32-125`
- Test: `tests/test_uploader_backend_drift.py`

**Interfaces:**
- Produces: `ToutiaoVideoUploadRequest(..., category: str)`。
- Produces: `load_toutiao_metadata(path: Path) -> ToutiaoVideoUploadRequest`。
- Consumes: `existing_file_path`, `parse_tags`, `resolve_account_file`。

- [ ] **Step 1: Write failing CLI tests**

```python
def test_direct_upload_requires_category(self):
    parser = sau_cli.build_parser()
    with self.assertRaises(SystemExit):
        parser.parse_args([
            "toutiao", "upload-video", "--account", "erjia", "--file", __file__,
            "--title", "标题", "--desc", "简介", "--tags", "标签",
        ])


def test_metadata_loads_complete_publish_request(self):
    with tempfile.TemporaryDirectory() as directory:
        metadata = Path(directory) / "publish.json"
        video = Path(directory) / "video.mp4"
        video.touch()
        metadata.write_text(json.dumps({
            "video_file": str(video), "title": "标题", "description": "简介",
            "tags": ["典故", "框架效应"], "category": "文化",
            "generate_article": False,
        }), encoding="utf-8")
        request = toutiao_command.load_toutiao_metadata(metadata, "erjia")
    self.assertEqual(request.category, "文化")
    self.assertEqual(request.description, "简介")
    self.assertFalse(request.generate_article)
```

- [ ] **Step 2: Run focused tests to confirm failure**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoCliMetadataTests -v`

Expected: FAIL because `--category` is not required and `load_toutiao_metadata` does not exist.

- [ ] **Step 3: Add CLI parsing and metadata loader**

```python
upload_parser.add_argument("--metadata", type=existing_file_path)
upload_parser.add_argument("--category", help="Toutiao content category")
upload_parser.add_argument("--file", type=existing_file_path)
upload_parser.add_argument("--title")

@dataclass(slots=True)
class ToutiaoVideoUploadRequest:
    # existing fields
    category: str

def load_toutiao_metadata(metadata_file: Path, account_name: str) -> ToutiaoVideoUploadRequest:
    raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    required = ("video_file", "title", "description", "tags", "category")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"头条发布清单缺少必填字段: {', '.join(missing)}")
    video_file = (metadata_file.parent / raw["video_file"]).resolve()
    thumbnail = raw.get("thumbnail_file")
    return ToutiaoVideoUploadRequest(
        account_name=account_name,
        video_file=existing_file_path(str(video_file)),
        title=raw["title"],
        description=raw["description"],
        tags=list(raw["tags"]),
        category=raw["category"],
        thumbnail_file=(metadata_file.parent / thumbnail).resolve() if thumbnail else None,
        generate_article=bool(raw.get("generate_article", True)),
    )
```

Update `run(args)` to require exactly one source: metadata mode rejects `--file`, `--title`, `--desc`, `--tags`, `--thumbnail`, `--category`, and `--no-article`; direct mode validates all required values before constructing the request.

- [ ] **Step 4: Run focused tests to confirm pass**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoCliMetadataTests -v`

Expected: PASS.

- [ ] **Step 5: Review scope and record non-commit limitation**

Run: `git -C /Users/robin/tools/social-auto-upload status --short`

Expected: it reports no Git repository. Do not initialize a repository; note the limitation in final delivery.

### Task 2: 分类、简介和话题的表单回读

**Files:**
- Modify: `uploader/toutiao_uploader/main.py:30-525`
- Test: `tests/test_uploader_backend_drift.py`

**Interfaces:**
- Consumes: `ToutiaoVideo.category: str`.
- Produces: `ToutiaoVideo._fill_desc(page) -> None`, which raises if description is absent or readback mismatches.
- Produces: `ToutiaoVideo._select_category(page) -> None`, which discovers visible options, selects `self.category`, then verifies displayed text.
- Produces: `ToutiaoVideo._fill_topics(page) -> None`, which raises if every requested topic is not present after input.

- [ ] **Step 1: Write failing form tests**

```python
async def test_desc_must_read_back_exact_value(self):
    page = DescriptionPage(readback="wrong text")
    video = MagicMock(desc="expected text")
    with self.assertRaisesRegex(RuntimeError, "视频简介未写入表单"):
        await toutiao_main.ToutiaoVideo._fill_desc(video, page)

async def test_category_must_be_an_actual_option_and_read_back(self):
    page = CategoryPage(options=["文化", "生活"], selected="文化")
    video = MagicMock(category="文化")
    await toutiao_main.ToutiaoVideo._select_category(video, page)

    video.category = "不存在"
    with self.assertRaisesRegex(ValueError, "可选分类"):
        await toutiao_main.ToutiaoVideo._select_category(video, page)

async def test_topics_raise_when_any_requested_tag_is_missing(self):
    page = TopicPage(accepted_tags=["典故"])
    video = MagicMock(tags=["典故", "框架效应"])
    with self.assertRaisesRegex(RuntimeError, "话题未写入表单"):
        await toutiao_main.ToutiaoVideo._fill_topics(video, page)
```

- [ ] **Step 2: Run focused tests to confirm failure**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoFormReadbackTests -v`

Expected: FAIL because description has no readback check, category support does not exist, and missing topic only logs a warning.

- [ ] **Step 3: Add explicit local selectors and methods**

```python
CATEGORY_LABEL_TEXT = "分类"
CATEGORY_CONTROL_SELECTOR = "input, [role=combobox], .arco-select-view, .xigua-select"
CATEGORY_OPTION_SELECTOR = '[role="option"], .arco-select-option, .xigua-select-option'

async def _fill_desc(self, page: Page) -> None:
    field = page.locator(DESC_SELECTOR).first
    await field.wait_for(state="visible", timeout=30000)
    expected = self.desc[:400]
    await field.fill(expected)
    if (await field.input_value()).strip() != expected:
        raise RuntimeError("视频简介未写入表单")

async def _select_category(self, page: Page) -> None:
    section = page.locator("label, div").filter(has_text=CATEGORY_LABEL_TEXT).first
    control = section.locator(CATEGORY_CONTROL_SELECTOR).first
    await control.click(timeout=10000)
    options = page.locator(CATEGORY_OPTION_SELECTOR)
    names = [text.strip() for text in await options.all_inner_texts() if text.strip()]
    if self.category not in names:
        raise ValueError(f"头条号分类「{self.category}」不可选；当前可选分类: {', '.join(names)}")
    await options.filter(has_text=re.compile(f"^{re.escape(self.category)}$")).first.click()
    if self.category not in (await control.inner_text()):
        raise RuntimeError("视频分类未写入表单")
```

Use a category-container helper that searches a label-bearing ancestor before falling back to a visible combobox only when exactly one exists. Do not select a global “分类” text match. Update `_fill_topics` to collect chip text and raise `RuntimeError` if any requested tag is absent.

- [ ] **Step 4: Insert methods into upload ordering**

```python
await self._fill_title(page)
await self._fill_desc(page)
await self._fill_topics(page)
await self._select_category(page)
```

The category call must occur before declaration, cover upload, and `_submit_publish`.

- [ ] **Step 5: Run focused tests to confirm pass**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoFormReadbackTests -v`

Expected: PASS.

### Task 3: 单次提交与明确结果状态

**Files:**
- Modify: `uploader/toutiao_uploader/main.py:496-524`
- Test: `tests/test_uploader_backend_drift.py`

**Interfaces:**
- Produces: `ToutiaoPublishUnknownError(RuntimeError)` for no explicit successful or failed result after one submit click.
- Produces: `ToutiaoVideo._submit_publish(page) -> None`, which clicks `.action-footer-btn.submit` exactly once.

- [ ] **Step 1: Write failing submit tests**

```python
async def test_submit_stops_on_page_validation_error_without_retry(self):
    page = PublishPage(body="请选择视频分类", url=TOUTIAO_PUBLISH_URL)
    with self.assertRaisesRegex(RuntimeError, "请选择视频分类"):
        await toutiao_main.ToutiaoVideo._submit_publish(MagicMock(), page)
    self.assertEqual(page.submit_clicks, 1)

async def test_submit_unknown_result_clicks_only_once(self):
    page = PublishPage(body="", url=TOUTIAO_PUBLISH_URL)
    with patch.object(toutiao_main.time, "monotonic", side_effect=range(200)):
        with self.assertRaises(toutiao_main.ToutiaoPublishUnknownError):
            await toutiao_main.ToutiaoVideo._submit_publish(MagicMock(), page)
    self.assertEqual(page.submit_clicks, 1)
```

- [ ] **Step 2: Run focused tests to confirm failure**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoImmediatePublishSelectorTests -v`

Expected: FAIL because neither explicit page validation nor unknown-result exception is represented.

- [ ] **Step 3: Implement result classifier with one click**

```python
class ToutiaoPublishUnknownError(RuntimeError):
    """点击发布后未收到可验证的成功或失败结果。"""

PUBLISH_SUCCESS_TEXTS = ("发布成功", "提交成功", "审核中")
PUBLISH_FAILURE_PATTERNS = (
    r"请选择.+", r"不能为空", r"发布失败", r"提交失败", r"请完善.+",
)

async def _submit_publish(self, page: Page) -> None:
    publish_btn = page.locator(".action-footer-btn.submit").first
    await publish_btn.wait_for(state="visible", timeout=15000)
    await publish_btn.click(force=True)
    for _ in range(120):
        body = await page.inner_text("body")
        if any(text in body for text in PUBLISH_SUCCESS_TEXTS) or "upload-video" not in page.url:
            return
        if match := next((re.search(pattern, body) for pattern in PUBLISH_FAILURE_PATTERNS if re.search(pattern, body)), None):
            raise RuntimeError(f"头条号发布被页面校验阻止: {match.group(0)}")
        await page.wait_for_timeout(1500)
    raise ToutiaoPublishUnknownError(
        f"头条号发布结果未知；未检测到成功或页面校验结果，当前 URL: {page.url}。"
        "请先在内容管理页确认，勿直接重复发布。"
    )
```

Keep `.action-footer-btn.submit` as the immediate-publish selector. Do not add retry-click logic.

- [ ] **Step 4: Run focused tests to confirm pass**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoImmediatePublishSelectorTests -v`

Expected: PASS.

### Task 4: 正确 `zhaosan` 清单与无写入分类探测

**Files:**
- Create: `/Users/robin/Desktop/deepseek_harnes/zhaosan/toutiao-publish.json`
- Modify: `sau_commands/toutiao.py`
- Test: `tests/test_uploader_backend_drift.py`

**Interfaces:**
- Produces: `sau toutiao inspect-categories --account <account>` command that only opens the publish page and prints discovered category labels; it never attaches a file, fills a field, or clicks submit.

- [ ] **Step 1: Write failing inspect parser test**

```python
def test_inspect_categories_requires_account_only(self):
    parser = sau_cli.build_parser()
    args = parser.parse_args(["toutiao", "inspect-categories", "--account", "erjia"])
    self.assertEqual(args.action, "inspect-categories")
```

- [ ] **Step 2: Run the test to confirm failure**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoCliMetadataTests.test_inspect_categories_requires_account_only -v`

Expected: FAIL because the action is unavailable.

- [ ] **Step 3: Add the read-only inspect command and project metadata**

```json
{
  "video_file": "renders/zhaosan_2026-11-01_11-32-08.mp4",
  "title": "朝三暮四：3+4与4+3为何不同",
  "description": "《庄子·齐物论》里，养猴人把朝三暮四换成朝四暮三，橡子总数仍是七颗，猴子却从愤怒变成高兴。这正是框架效应：信息总量不变，表达方式会改变选择。下次看到一组漂亮数字，先问一句：它换过说法没有？",
  "tags": ["朝三暮四", "庄子", "框架效应", "传统文化"],
  "category": "文化",
  "thumbnail_file": "covers/zhaosan_cover.jpg",
  "generate_article": true
}
```

Implement `inspect_toutiao_categories(account_name: str) -> list[str]` using authenticated storage state, `TOUTIAO_PUBLISH_URL`, and the same category discovery helper as `_select_category`. The method must not query file input, call `.fill()`, or locate/click `.action-footer-btn.submit`.

- [ ] **Step 4: Run the parser test to confirm pass**

Run: `python -m unittest tests.test_uploader_backend_drift.ToutiaoCliMetadataTests.test_inspect_categories_requires_account_only -v`

Expected: PASS.

- [ ] **Step 5: Run the read-only live category inspection once**

Run: `sau toutiao inspect-categories --account erjia`

Expected: a non-empty list of visible category labels. If `文化` is not an actual value, update only the JSON fixture’s `category` to the exact closest available culture/history category. Do not upload or submit.

### Task 5: Full verification and review

**Files:**
- Verify: `sau_commands/toutiao.py`
- Verify: `uploader/toutiao_uploader/main.py`
- Verify: `tests/test_uploader_backend_drift.py`
- Verify: `/Users/robin/Desktop/deepseek_harnes/zhaosan/toutiao-publish.json`

- [ ] **Step 1: Run focused headless test suite**

Run: `python -m unittest tests.test_uploader_backend_drift -v`

Expected: all tests pass.

- [ ] **Step 2: Run complete project test suite**

Run: `python -m pytest -q`

Expected: no new failures. Any unrelated pre-existing failures must be listed with names and verified unchanged against the pre-change baseline.

- [ ] **Step 3: Compile edited modules and inspect command help**

Run: `python -m py_compile sau_commands/toutiao.py uploader/toutiao_uploader/main.py && sau toutiao upload-video --help && sau toutiao inspect-categories --help`

Expected: exit 0 and help documents `--metadata`, `--category`, and read-only category inspection.

- [ ] **Step 4: Architecture review**

Confirm manually:

- Metadata mode has one source of truth and rejects mixed sources.
- Direct mode requires category.
- Description/category/topic failures occur before submit.
- `_submit_publish` contains one `click()` call only.
- A “results unknown” outcome is never logged or returned as success.
- No `upload-video` command was run during this repair.

- [ ] **Step 5: Report the exact state**

Report changed paths, all command outputs, the live discovered category selected for `zhaosan`’s future-use metadata, and that no commit was possible because the actual source directory has no Git metadata.
