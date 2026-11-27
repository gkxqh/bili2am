"""Transparent browser cookie extraction for Safari, Edge, and Chrome."""

from __future__ import annotations

from collections.abc import Callable
from http.cookiejar import CookieJar

from bili2am.models import CookieAttempt, CookieResolution

SUPPORTED_BROWSERS = ("safari", "edge", "chrome", "firefox", "brave")


class _SilentLogger:
    """Suppress yt-dlp stderr chatter during cookie probing."""

    def debug(self, msg: str) -> None:  # noqa: ARG002
        return

    def info(self, msg: str) -> None:  # noqa: ARG002
        return

    def warning(self, msg: str) -> None:  # noqa: ARG002
        return

    def error(self, msg: str) -> None:  # noqa: ARG002
        return


def _default_cookie_loader(browser: str) -> CookieJar:
    from yt_dlp.cookies import extract_cookies_from_browser

    return extract_cookies_from_browser(browser, logger=_SilentLogger())


def _count_bilibili_cookies(jar: CookieJar) -> int:
    return sum(1 for c in jar if "bilibili.com" in (c.domain or "").lower())


def resolve_bilibili_cookies(
    enabled: bool = True,
    priority: list[str] | None = None,
    loader: Callable[[str], CookieJar] | None = None,
) -> tuple[CookieJar | None, CookieResolution]:
    """Attempt to read Bilibili cookies in configured browser priority order.

    Never stores or logs raw cookie values. Always returns a human-readable
    Chinese notice explaining what happened.
    """
    order = [b.strip().lower() for b in (priority or ["safari", "edge", "chrome"]) if b.strip()]
    if not enabled or not order:
        return None, CookieResolution(
            enabled=False,
            selected_browser=None,
            bilibili_cookie_count=0,
            notice="已关闭浏览器 Cookie 读取，当前以匿名模式访问 B 站。",
            attempts=[],
        )

    cookie_loader = loader or _default_cookie_loader
    attempts: list[CookieAttempt] = []
    failed_notes: list[str] = []

    for browser in order:
        if browser not in SUPPORTED_BROWSERS:
            msg = f"不支持的浏览器标识: {browser}"
            attempts.append(CookieAttempt(browser=browser, success=False, message=msg))
            failed_notes.append(f"{browser}（不支持）")
            continue

        try:
            jar = cookie_loader(browser)
            bili_count = _count_bilibili_cookies(jar)
            if bili_count == 0:
                msg = "未在该浏览器中找到 bilibili.com 登录 Cookie"
                attempts.append(
                    CookieAttempt(browser=browser, success=False, cookie_count=0, message=msg)
                )
                failed_notes.append(f"{browser.capitalize()}（无 B 站 Cookie）")
                continue

            msg = f"成功读取到 {bili_count} 条 B 站 Cookie"
            attempts.append(
                CookieAttempt(browser=browser, success=True, cookie_count=bili_count, message=msg)
            )
            prefix = ""
            if failed_notes:
                prefix = f"已跳过 {'、'.join(failed_notes)}；"
            notice = (
                f"{prefix}已从 {browser.capitalize()} 读取到 {bili_count} 条 B 站 Cookie"
                "（仅在本地内存用于本次请求，不会保存或上传）。"
            )
            return jar, CookieResolution(
                enabled=True,
                selected_browser=browser,
                bilibili_cookie_count=bili_count,
                notice=notice,
                attempts=attempts,
            )
        except PermissionError:
            msg = "系统权限受限（macOS Safari 需在「系统设置 > 隐私与安全性 > 完全磁盘访问权限」授权终端）"
            attempts.append(CookieAttempt(browser=browser, success=False, message=msg))
            failed_notes.append(f"{browser.capitalize()}（无磁盘权限）")
        except Exception as exc:  # noqa: BLE001
            err_text = str(exc).strip() or exc.__class__.__name__
            if "Operation not permitted" in err_text or "EPERM" in err_text:
                msg = "系统权限受限（macOS Safari 需授予终端「完全磁盘访问权限」）"
                failed_notes.append(f"{browser.capitalize()}（无磁盘权限）")
            else:
                msg = f"读取失败: {err_text[:120]}"
                failed_notes.append(f"{browser.capitalize()}（不可用）")
            attempts.append(CookieAttempt(browser=browser, success=False, message=msg))

    chain = " -> ".join(b.capitalize() for b in order)
    notice = (
        f"按优先级尝试读取本地 Cookie（{chain}）均未成功，已自动降级为匿名模式。"
        + (f" 原因：{'；'.join(failed_notes)}。" if failed_notes else "")
    )
    return None, CookieResolution(
        enabled=True,
        selected_browser=None,
        bilibili_cookie_count=0,
        notice=notice,
        attempts=attempts,
    )
