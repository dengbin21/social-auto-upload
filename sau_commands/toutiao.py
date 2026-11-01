"""toutiao（头条号）的命令参数与执行逻辑。"""
from __future__ import annotations
from dataclasses import dataclass
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
    upload_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    upload_parser.add_argument("--title", required=True, help="Video title (max 30 chars)")
    upload_parser.add_argument("--desc", default="", help="Optional video description")
    upload_parser.add_argument("--tags", default="", help="Comma-separated topics, such as tag1,tag2")
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
    thumbnail_file: Path | None = None
    generate_article: bool = True
    debug: bool = True
    headless: bool = True


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

    app = ToutiaoVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        account_file=str(account_file),
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
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
        request = ToutiaoVideoUploadRequest(
            account_name=args.account,
            video_file=args.file,
            title=args.title,
            description=args.desc,
            tags=parse_tags(args.tags),
            thumbnail_file=args.thumbnail,
            generate_article=not args.no_article,
            debug=args.debug,
            headless=args.headless,
        )
        await upload_toutiao_video(request)
        print(f"Toutiao video upload submitted: {request.video_file}")
        return 0

    raise RuntimeError(f"Unsupported Toutiao action: {args.action}")
