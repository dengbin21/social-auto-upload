"""西瓜视频 的命令参数与执行逻辑。

⚠️ 重要说明：
  西瓜视频（ixigua.com）没有独立的网页创作者平台。
  此命令实际调用的是头条号后台的上传接口：
  https://mp.toutiao.com/profile_v4/xigua/upload-video
  
  作品会发布到【头条号】，不是西瓜视频独立平台。
  西瓜视频 APP 可见（因为账号互通），但数据、粉丝、收益在西瓜视频是分离的。
  
  如果需要独立发布到西瓜视频，请使用西瓜视频 APP 手动上传。
"""
from __future__ import annotations
from sau_commands.toutiao import (
    register as toutiao_register,
    login_toutiao_account,
    check_toutiao_account,
    upload_toutiao_video,
)
from sau_commands.arguments import (
    SCHEDULE_FORMAT,
    add_runtime_flags,
    existing_file_path,
    parse_tags,
    resolve_account_file,
)


def register(platform_parsers):
    """声明本平台命令、参数及合法选项。"""
    schedule_help = SCHEDULE_FORMAT.replace("%", "%%")
    xigua_parser = platform_parsers.add_parser("xigua", help="Xigua (Douyin/Headline) video publishing")
    xigua_actions = xigua_parser.add_subparsers(dest="action", required=True)

    for action_name in ("login", "check"):
        action_parser = xigua_actions.add_parser(action_name, help=f"Xigua {action_name}")
        action_parser.add_argument("--account", required=True, help="Xigua user-defined account_name")
        if action_name == "login":
            add_runtime_flags(action_parser)

    xigua_upload_video_parser = xigua_actions.add_parser("upload-video", help="Upload one video to Xigua")
    xigua_upload_video_parser.add_argument("--account", required=True, help="Xigua user-defined account_name")
    xigua_upload_video_parser.add_argument("--file", required=True, type=existing_file_path, help="Video file path")
    xigua_upload_video_parser.add_argument("--title", required=True, help="Video title")
    xigua_upload_video_parser.add_argument("--desc", default="", help="Optional video description")
    xigua_upload_video_parser.add_argument("--tags", default="", help="Comma-separated tags, such as tag1,tag2")
    xigua_upload_video_parser.add_argument("--thumbnail", type=existing_file_path, help="Optional cover image path")
    xigua_upload_video_parser.add_argument("--category", default="生活", help="视频分类，默认为‘生活’，可在头条号后台查看可选项")
    xigua_upload_video_parser.add_argument("--collection", default=None, help="Optional collection name to add the work into (must already exist)")
    add_runtime_flags(xigua_upload_video_parser)


async def login_xigua_account(account_name: str, headless: bool = True):
    """扫码登录头条号（西瓜视频无独立网页版，共用账号体系）."""
    return await login_toutiao_account(account_name, headless=headless)


async def check_xigua_account(account_name: str) -> bool:
    """检查头条号登录状态（西瓜视频无独立网页版）."""
    return await check_toutiao_account(account_name)


async def upload_xigua_video(args):
    """发布视频到头条号（西瓜视频无独立网页创作者平台）."""
    # 构建 ToutiaoVideoUploadRequest，因为 xigua 的参数结构与头条不同
    from sau_commands.toutiao import ToutiaoVideoUploadRequest
    from pathlib import Path
    
    # 获取账号文件
    account_file = resolve_account_file("xigua", args.account)
    
    request = ToutiaoVideoUploadRequest(
        account_name=args.account,
        video_file=Path(args.file),
        title=args.title,
        description=args.desc,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else [],
        category=args.category,
        thumbnail_file=Path(args.thumbnail) if args.thumbnail else None,
        generate_article=False,  # xigua 视频不生成文章
        debug=True,
        headless=True,
    )
    return await upload_toutiao_video(request)


async def run(args):
    """执行本平台命令；发布结果由平台实现核验。"""
    if args.action == "login":
        result = await login_xigua_account(args.account, headless=args.headless)
        if not result["success"]:
            raise RuntimeError(result["message"])
        print(f"Xigua login flow completed: {result['account_file']}")
        return 0

    if args.action == "check":
        is_valid = await check_xigua_account(args.account)
        print("valid" if is_valid else "invalid")
        return 0 if is_valid else 1

    if args.action == "upload-video":
        await upload_xigua_video(args)
        print("Upload request completed; use `sau xigua verify` or check platform management page for status.")
        return 0

    raise RuntimeError(f"Unsupported action: {args.action}")