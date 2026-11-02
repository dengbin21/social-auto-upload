"""toutiao（头条号）的命令参数与执行逻辑。

发布前置门禁（K67/K57）：在浏览器打开前，先运行封面醒目度
与信息量两项门禁，任一不通过直接拦截，不浪费浏览器资源。
"""

from __future__ import annotations
from dataclasses import dataclass
import functools
import json
import sys
from pathlib import Path

from uploader.toutiao_uploader.main import (
    ToutiaoVideo,
    toutiao_setup,
    cookie_auth as toutiao_cookie_auth,
)
from sau_commands.arguments import (
    add_runtime_flags,
    existing_file_path,
    parse_tags,
    resolve_account_file,
)
from utils.log import toutiao_logger

# 向工作区 scripts 目录追加 sys.path，以便复用门禁脚本（不复制逻辑，
# 判据单一出处仍走 scripts/thresholds.json，见 E87/E74）
try:
    sys.path.insert(0, "/Users/robin/Desktop/deepseek_harnes/scripts")
    import cover_check as _cover_check
    import info_check as _info_check
    import film_gate_check as _film_gate_check
    import opening_check as _opening_check
except ImportError as exc:
    _cover_check = None
    _info_check = None
    _film_gate_check = None
    _opening_check = None
    _import_error = exc


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def gate_check(checker, label: str, method_name: str = "run_one"):
    """门禁检查装饰器工厂。将门禁检查封装为可复用的装饰器。

    用法：
        @gate_check(_cover_check, "封面醒目度")
        def check_cover(project_dir):
            pass
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if checker is None:
                toutiao_logger.warning(_msg("⚠️", f"{label} 门禁脚本未加载，跳过检查"))
                return func(*args, **kwargs)
            method = getattr(checker, method_name, None)
            if method is None:
                toutiao_logger.warning(_msg("⚠️", f"{label} 门禁脚本缺少 {method_name} 方法，跳过检查"))
                return func(*args, **kwargs)
            # 从 kwargs 或 args 中获取 project_dir
            project_dir = kwargs.get("project_dir") or (args[0] if args else None)
            if project_dir is not None and method(str(project_dir)):
                raise RuntimeError(f"{label} 检查未通过，发布被拦截")
            return func(*args, **kwargs)
        return wrapper
    return decorator


def _run_gate_check(checker, project_dir: Path, label: str, method_name: str = "run_one") -> None:
    """运行单个门禁检查，不通过时抛出 RuntimeError。"""
    if checker is None:
        toutiao_logger.warning(_msg("⚠️", f"{label} 门禁脚本未加载，跳过检查"))
        return
    method = getattr(checker, method_name, None)
    if method is None:
        toutiao_logger.warning(_msg("⚠️", f"{label} 门禁脚本缺少 {method_name} 方法，跳过检查"))
        return
    if method(str(project_dir)):
        raise RuntimeError(f"{label} 检查未通过，发布被拦截")


def register(platform_parsers):
    """声明本平台命令、参数及合法选项。"""
    toutiao_parser = platform_parsers.add_parser("toutiao", help="Toutiao (今日头条号) operations")
    toutiao_actions = toutiao_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = toutiao_actions.add_parser(action_name, help=f"Toutiao {action_name}")
        action_parser.add_argument("--account", required=True, help="Toutiao user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)
            # 头条二维码会自动刷新；默认让用户扫窗口内的实时二维码。
            action_parser.set_defaults(headless=False)

    upload_parser = toutiao_actions.add_parser("upload-video", help="Upload one video to Toutiao")
    upload_parser.add_argument("--account", required=True, help="Toutiao user-defined account_name")
    upload_parser.add_argument("--metadata", type=existing_file_path,
                               help="JSON publish manifest; cannot be combined with metadata flags")
    upload_parser.add_argument("--file", type=existing_file_path, help="Video file path")
    upload_parser.add_argument("--title", help="Video title (max 30 chars)")
    upload_parser.add_argument("--desc", default="", help="Optional video description")
    upload_parser.add_argument("--tags", default="", help="Comma-separated topics, such as tag1,tag2")
    upload_parser.add_argument("--category", help="Toutiao category shown in the publish form")
    upload_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional cover image path")
    upload_parser.add_argument(
        "--no-article",
        action="store_true",
        help="Do not tick '生成图文' (by default the companion article is generated for extra revenue)",
    )
    add_runtime_flags(upload_parser)


@dataclass(slots=True)
class ToutiaoVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    category: str
    thumbnail_file: Path | None = None
    generate_article: bool = True
    debug: bool = True
    headless: bool = True


def load_toutiao_metadata(metadata_file: Path, account_name: str) -> ToutiaoVideoUploadRequest:
    """Load one video's publish metadata from its JSON manifest.

    The manifest is intentionally complete so that a publish cannot inherit
    title or description values from a previous video command.
    """
    try:
        raw = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取头条发布清单: {metadata_file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("头条发布清单必须是 JSON 对象")

    required = ("video_file", "title", "description", "tags", "category")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"头条发布清单缺少必填字段: {', '.join(missing)}")
    if not isinstance(raw["tags"], list) or not all(isinstance(tag, str) and tag.strip() for tag in raw["tags"]):
        raise ValueError("头条发布清单的 tags 必须是非空字符串数组")

    video_file = (metadata_file.parent / raw["video_file"]).resolve()
    if not video_file.is_file():
        raise ValueError(f"头条发布清单的视频文件不存在: {video_file}")
    thumbnail_file = None
    if raw.get("thumbnail_file"):
        thumbnail_file = (metadata_file.parent / raw["thumbnail_file"]).resolve()
        if not thumbnail_file.is_file():
            raise ValueError(f"头条发布清单的封面文件不存在: {thumbnail_file}")

    return ToutiaoVideoUploadRequest(
        account_name=account_name,
        video_file=video_file,
        title=raw["title"].strip(),
        description=raw["description"].strip(),
        tags=[tag.strip() for tag in raw["tags"]],
        category=raw["category"].strip(),
        thumbnail_file=thumbnail_file,
        generate_article=bool(raw.get("generate_article", True)),
    )


def build_toutiao_upload_request(args) -> ToutiaoVideoUploadRequest:
    """Build a request from exactly one metadata source before opening a browser."""
    if args.metadata:
        conflicting = [
            flag for flag, value in (
                ("--file", args.file), ("--title", args.title), ("--desc", args.desc),
                ("--tags", args.tags), ("--category", args.category),
                ("--thumbnail", args.thumbnail), ("--no-article", args.no_article),
            ) if value
        ]
        if conflicting:
            raise ValueError(f"--metadata 不能与 {', '.join(conflicting)} 同时使用")
        return load_toutiao_metadata(args.metadata, args.account)

    missing = [
        flag for flag, value in (
            ("--file", args.file), ("--title", args.title), ("--desc", args.desc),
            ("--tags", args.tags), ("--category", args.category),
        ) if not value
    ]
    if missing:
        raise ValueError(f"逐参数发布缺少必填参数: {', '.join(missing)}")
    return ToutiaoVideoUploadRequest(
        account_name=args.account,
        video_file=args.file,
        title=args.title,
        description=args.desc,
        tags=parse_tags(args.tags),
        category=args.category,
        thumbnail_file=args.thumbnail,
        generate_article=not args.no_article,
        debug=args.debug,
        headless=args.headless,
    )


async def login_toutiao_account(account_name: str, headless: bool = False) -> dict:
    account_file = resolve_account_file("toutiao", account_name)
    # 头条二维码刷新很快，静态图必过期；默认 headed 让用户扫窗口内自动刷新的码
    return await toutiao_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_toutiao_account(account_name: str) -> bool:
    account_file = resolve_account_file("toutiao", account_name)
    if not account_file.exists():
        return False
    return await toutiao_cookie_auth(str(account_file))


async def upload_toutiao_video(request: ToutiaoVideoUploadRequest) -> Path:
    account_file = resolve_account_file("toutiao", request.account_name)
    is_ready = await toutiao_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Toutiao cookie is missing or expired: {account_file}. "
            f"Run `sau toutiao login --account {request.account_name}` first."
        )
    
    project_dir = Path(request.video_file).resolve().parents[1]
    
    # 发布前置门禁（K67/K57）：封面醒目度与信息量检查（在打开浏览器前）
    # 这些检查在打开浏览器前执行，不通过则直接拦截，不打开浏览器
    # 去重检查（避免重复发布）
    try:
        sys.path.insert(0, "/Users/robin/Desktop/deepseek_harnes/scripts")
        import publish_dedup_check as _dedup
        is_dup, reason = _dedup.check_duplicate(project_dir, "toutiao", request.account_name, request.title, str(request.video_file))
        if is_dup:
            raise RuntimeError(f"重复发布检测：{reason}")
    except ImportError:
        pass  # 脚本不存在时跳过
    
    try:
        # 封面醒目度检查（K67）
        _run_gate_check(_cover_check, project_dir, "封面醒目度")
        
        # 信息量检查（K57）
        _run_gate_check(_info_check, project_dir, "信息量")

        # 首 3 秒打断检查（K67）：渲染后、发布前检查
        _run_gate_check(_opening_check, project_dir, "首 3 秒打断")
        
        # 成片量化检查（K71）：渲染后、发布前检查
        _run_gate_check(_film_gate_check, project_dir, "成片量化", "check_project")
    except Exception as e:
        raise RuntimeError(f"发布前门禁检查失败: {e}")
    
    app = ToutiaoVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        account_file=str(account_file),
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        category=request.category,
        generate_article=request.generate_article,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def run(args):
    """执行本平台命令；发布结果由平台实现核验。"""
    if args.action == "login":
        result = await login_toutiao_account(args.account, headless=args.headless)
        if not result["success"]:
            raise RuntimeError(result["message"])
        print(f"Toutiao login flow completed: {result['account_file']}")
        return 0

    if args.action == "check":
        is_valid = await check_toutiao_account(args.account)
        print("valid" if is_valid else "invalid")
        return 0 if is_valid else 1

    if args.action == "upload-video":
        request = build_toutiao_upload_request(args)
        await upload_toutiao_video(request)
        print(f"Toutiao video upload submitted: {request.video_file}")
        return 0

    raise RuntimeError(f"Unsupported Toutiao action: {args.action}")