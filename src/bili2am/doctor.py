"""Environment self-check (`bili2am doctor`)."""

from __future__ import annotations

import platform
import shutil
import sys

from bili2am.config import AppConfig, get_config_path, load_config
from bili2am.exporters.web_apple_music import detect_browser_channel, is_playwright_available
from bili2am.extractors.shazam_fallback import is_shazamio_available
from bili2am.matcher.cleaner import check_ollama_status


def run_doctor(cfg: AppConfig | None = None) -> list[tuple[str, bool, str]]:
    """Inspect local runtime dependencies and return check rows."""
    conf = cfg or load_config()
    rows: list[tuple[str, bool, str]] = []

    rows.append(("macOS 系统", platform.system() == "Darwin", platform.platform()))
    rows.append(("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))

    for tool, hint in (
        ("ffmpeg", "必需（用于抽帧 OCR 与音频切片），可用 `brew install ffmpeg` 安装"),
        ("yt-dlp", "必需（用于拉取视频/音频流），可用 `pip install yt-dlp` 安装"),
        ("swiftc", "必需（用于编译 macOS 原生 Apple Vision OCR），安装 Xcode Command Line Tools 即可"),
    ):
        found = shutil.which(tool)
        rows.append((tool, bool(found), found or hint))

    pw_ok = is_playwright_available()
    ch = detect_browser_channel()
    rows.append(
        (
            "Playwright + 本机浏览器",
            pw_ok and bool(ch),
            f"已就绪（channel={ch}，用于网页版 Apple Music 导入）"
            if (pw_ok and ch)
            else ("未安装 playwright（运行 `pip install playwright`）" if not pw_ok else "未检测到 Chrome 或 Edge"),
        )
    )

    shazam_ok = is_shazamio_available()
    rows.append(
        (
            "shazamio (听歌识曲保底)",
            shazam_ok,
            "已安装" if shazam_ok else "未安装（需要保底识曲时运行 `pip install 'bili2am[shazam]'`）",
        )
    )

    ol = check_ollama_status(conf.llm.ollama_base_url)
    if ol["reachable"]:
        models = ", ".join(ol["models"]) if ol["models"] else "服务已运行，暂无已下载模型"
        rows.append(("Ollama (可选本地模型)", True, models))
    else:
        rows.append(("Ollama (可选本地模型)", False, "未启动或未安装（默认纯规则清洗无需 Ollama）"))

    rows.append(("配置文件路径", True, str(get_config_path())))
    return rows


def print_doctor_report(cfg: AppConfig | None = None) -> int:
    rows = run_doctor(cfg)
    print("bili2am 环境自检报告")
    print("=" * 60)
    core_missing = False
    for name, ok, detail in rows:
        icon = "✅" if ok else "⚠️"
        if name in {"ffmpeg", "yt-dlp", "swiftc"} and not ok:
            icon = "❌"
            core_missing = True
        print(f"{icon} {name:<26} {detail}")
    print("=" * 60)
    return 1 if core_missing else 0
