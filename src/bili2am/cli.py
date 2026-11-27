"""Command-line interface and native macOS GUI launcher for bili2am."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from bili2am import __version__
from bili2am.config import get_config_path, load_config, save_config
from bili2am.doctor import print_doctor_report
from bili2am.exporters.file_export import export_tracks_to_csv, export_tracks_to_txt
from bili2am.exporters.web_apple_music import run_web_apple_music_import
from bili2am.models import MatchStatus, PipelineResult
from bili2am.pipeline import run_pipeline
from bili2am.ui.confirm_server import launch_confirm_server


def find_app_bundle() -> Path | None:
    """Locate built bili2am.app bundle if available."""
    pkg_root = Path(__file__).resolve().parents[2]
    candidates = [
        pkg_root / "dist" / "bili2am.app",
        Path("/Applications/bili2am.app"),
        Path.home() / "Applications" / "bili2am.app",
        Path(load_config().cache_dir).expanduser() / "bili2am.app",
    ]
    for p in candidates:
        if (p / "Contents" / "MacOS" / "bili2am").exists():
            return p
    return None


def print_terminal_summary(res: PipelineResult) -> None:
    print()
    print(f"🎬 视频: {res.video.title} ({res.video.bvid})")
    print(f"🍪 Cookie: {res.cookie_info.notice}")
    print(f"🔍 提取方式: {res.extraction_method} | 清洗: {res.llm_used} | 曲库地区: {res.storefront.upper()}")
    for note in res.extraction_notes:
        print(f"   • {note}")
    print("-" * 68)

    matched = [t for t in res.tracks if t.status == MatchStatus.MATCHED]
    uncertain = [t for t in res.tracks if t.status == MatchStatus.UNCERTAIN]
    missing = [t for t in res.tracks if t.status == MatchStatus.MISSING]

    print(
        f"📊 共提取 {len(res.tracks)} 首 | ✅ 曲库已有 {len(matched)} | "
        f"⚠️ 待确认 {len(uncertain)} | ❌ 曲库未收录 {len(missing)}"
    )
    print()

    for t in res.tracks:
        icon = {"matched": "✅", "uncertain": "⚠️", "missing": "❌"}[t.status.value]
        ts = f"[{t.timestamp}] " if t.timestamp else ""
        base = f"{t.artist} - {t.title}" if t.artist else t.title
        cand = t.active_candidate
        if cand:
            print(
                f"  {icon} {ts}{base}  ==>  {cand.artist_name} - {cand.track_name} "
                f"({int(cand.confidence * 100)}%)"
            )
        else:
            print(f"  {icon} {ts}{base}  ==>  [Apple Music 无此曲目: {t.note}]")

    if missing:
        print()
        print("❌ 未收录曲目清单（不导入，不导出额外文件）：")
        for m in missing:
            ts = f"[{m.timestamp}] " if m.timestamp else ""
            name = f"{m.artist} - {m.title}" if m.artist else m.title
            print(f"   - {ts}{name}")
    print("-" * 68)


def _interactive_cli_confirm(res: PipelineResult) -> int:
    print_terminal_summary(res)
    confirmable = [t for t in res.tracks if t.status != MatchStatus.MISSING and t.candidates]
    if not confirmable:
        print("没有可导入 Apple Music 的已匹配曲目。")
        return 0

    choice = input(
        "\n请选择操作: [1] 网页版 Apple Music 自动导入(Chrome)  [2] 导出 CSV  [3] 导出 TXT  [q] 取消: "
    ).strip().lower()
    if choice == "1":
        r = run_web_apple_music_import(
            res.playlist_name,
            res.tracks,
            country=res.storefront,
        )
        print(("✅ " if r.success else "❌ ") + r.message)
        return 0 if r.success else 1
    if choice == "2":
        p = export_tracks_to_csv(res.tracks, Path.home() / "Downloads", res.playlist_name)
        print(f"✅ 已导出 {len(confirmable)} 首曲目到: {p}")
        return 0
    if choice == "3":
        p = export_tracks_to_txt(res.tracks, Path.home() / "Downloads", res.playlist_name)
        print(f"✅ 已导出 {len(confirmable)} 首曲目到: {p}")
        return 0
    print("已取消导入。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili2am",
        description="提取 B 站音乐合集歌单，校验 Apple Music 曲库并确认后导入。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="提取 B 站视频歌单并打开独立 macOS 应用或 CLI 确认")
    run_p.add_argument("target", nargs="?", help="B 站视频链接、b23.tv 短链或 BV 号")
    run_p.add_argument("--mode", choices=["auto", "text", "ocr", "shazam"], default="auto")
    run_p.add_argument("--storefront", default=None, help="Apple Music 地区，默认 cn（可设 us/jp/hk/tw）")
    run_p.add_argument("--no-cookies", action="store_true", help="本次禁用浏览器 Cookie 读取")
    run_p.add_argument(
        "--cookie-browsers",
        default=None,
        help="浏览器 Cookie 优先级，逗号分隔，如 safari,edge,chrome",
    )
    run_p.add_argument("--no-cache", action="store_true", help="忽略缓存重新提取")
    run_p.add_argument("--cli-only", action="store_true", help="仅在终端交互确认，不打开图形界面")
    run_p.add_argument("--web", action="store_true", help="使用本地浏览器网页界面而非原生 macOS App")
    run_p.add_argument("--api-only", action="store_true", help="仅启动无界面本地 JSON API（供原生 App 调用）")
    run_p.add_argument("--no-browser", action="store_true", help="配合 --web 时不自动打开浏览器")
    run_p.add_argument("--port", type=int, default=8765, help="本地 API 服务端口（默认 8765）")

    sub.add_parser("doctor", help="检查 ffmpeg、yt-dlp、swiftc、Chrome/Playwright 及可选组件状态")

    cfg_p = sub.add_parser("config", help="查看或初始化配置文件")
    cfg_p.add_argument("--init", action="store_true", help="生成默认配置文件")

    sub.add_parser("build-app", help="重新编译本地独立 macOS .app 程序")

    return parser


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    known_cmds = {"run", "doctor", "config", "build-app", "-h", "--help", "--version"}
    if not args_list:
        args_list = ["run"]
    elif args_list[0] not in known_cmds:
        args_list = ["run", *args_list]

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.command == "doctor":
        return print_doctor_report()

    if args.command == "build-app":
        build_script = Path(__file__).resolve().parents[2] / "scripts" / "build_app.py"
        if not build_script.exists():
            print(f"❌ 未找到构建脚本: {build_script}")
            return 1
        proc = subprocess.run([sys.executable, str(build_script)], check=False)
        return proc.returncode

    if args.command == "config":
        cfg = load_config()
        if args.init:
            path = save_config(cfg)
            print(f"已生成默认配置: {path}")
        else:
            print(f"配置文件路径: {get_config_path()}")
            print(cfg.model_dump_json(indent=2))
        return 0

    cfg = load_config()
    if getattr(args, "storefront", None):
        cfg.storefront = args.storefront.strip().lower()
    if getattr(args, "no_cookies", False):
        cfg.cookie.enabled = False
    if getattr(args, "cookie_browsers", None):
        cfg.cookie.priority = [
            b.strip().lower() for b in args.cookie_browsers.split(",") if b.strip()
        ]

    if args.api_only:
        initial_res = None
        if args.target:
            initial_res = run_pipeline(
                url_or_bvid=args.target,
                cfg=cfg,
                mode=args.mode,
                use_cache=not args.no_cache,
            )
        launch_confirm_server(
            cfg=cfg,
            initial_result=initial_res,
            port=args.port,
            open_browser=False,
        )
        return 0

    # Launch native macOS .app by default unless --cli-only or --web is specified
    app_bundle = find_app_bundle()
    if not args.cli_only and not args.web and app_bundle is not None:
        cmd = ["open", "-n", str(app_bundle)]
        if args.target:
            env = dict(os.environ)
            env["BILI2AM_TARGET"] = args.target
            # Pass target via --args so `open` forwards it cleanly to the SwiftUI app
            cmd.extend(["--args", "--target", args.target, "--mode", args.mode, "--storefront", cfg.storefront])
            subprocess.run(cmd, env=env, check=False)
        else:
            subprocess.run(cmd, check=False)
        print(f"已启动独立桌面程序：{app_bundle}")
        return 0

    if not args.target:
        print(f"启动本地确认服务：http://127.0.0.1:{args.port}")
        launch_confirm_server(cfg=cfg, initial_result=None, port=args.port, open_browser=not args.no_browser)
        return 0

    res = run_pipeline(
        url_or_bvid=args.target,
        cfg=cfg,
        mode=args.mode,
        use_cache=not args.no_cache,
    )

    if args.cli_only:
        return _interactive_cli_confirm(res)

    print_terminal_summary(res)
    print(f"\n🌐 已启动本地确认页面：http://127.0.0.1:{args.port} （按 Ctrl+C 结束）")
    launch_confirm_server(
        cfg=cfg,
        initial_result=res,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
