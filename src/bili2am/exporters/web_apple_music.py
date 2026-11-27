"""Web Apple Music (music.apple.com) automation via Playwright using local Chrome."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bili2am.exporters.file_export import filter_confirmed_tracks
from bili2am.models import TrackMatchItem

logger = logging.getLogger("bili2am.web_apple_music")

CHROME_APP_PATH = Path("/Applications/Google Chrome.app")
CHROME_BIN_PATH = CHROME_APP_PATH / "Contents" / "MacOS" / "Google Chrome"
EDGE_APP_PATH = Path("/Applications/Microsoft Edge.app")
DEFAULT_CDP_PORT = 9229


@dataclass
class WebTrackAction:
    id: str
    title: str
    artist: str
    track_id: int
    web_url: str
    status: str  # "pending", "added", "failed", "skipped"
    detail: str = ""


@dataclass
class WebAppleMusicResult:
    success: bool
    playlist_name: str
    running: bool = False
    waiting_for_login: bool = False
    total_count: int = 0
    processed_count: int = 0
    added_count: int = 0
    failed_count: int = 0
    history_id: str | None = None
    message: str = ""
    log_path: str = ""
    logs: list[str] = field(default_factory=list)
    items: list[WebTrackAction] = field(default_factory=list)


def is_playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def detect_browser_channel() -> str | None:
    """Prefer locally installed Chrome/Edge to avoid downloading Chromium."""
    if CHROME_APP_PATH.exists():
        return "chrome"
    if EDGE_APP_PATH.exists():
        return "msedge"
    return None


def _write_web_log(cache_dir: Path, lines: list[str]) -> str:
    log_dir = cache_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "web_apple_music.log"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = f"\n=== [{stamp}] ===\n" + "\n".join(lines) + "\n"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(block)
    return str(log_file)


def _is_logged_in(page: Any) -> bool:
    """Check whether music.apple.com has an active signed-in session using sidebar library UI."""
    try:
        current_url = str(getattr(page, "url", "") or "")
        if current_url and "music.apple.com" not in current_url:
            return False

        sign_in_btn = page.locator('button[data-testid="sign-in-button"]')
        if sign_in_btn.count() > 0 and sign_in_btn.first.is_visible():
            return False

        alt_btn = page.locator("header button, .web-navigation button").filter(has_text="登录")
        if alt_btn.count() > 0 and alt_btn.first.is_visible():
            return False

        # Only /library sidebar links reliably prove CommerceKit + MusicKit are both signed in
        library_nav = page.locator('nav a[href*="/library"], [data-testid="sidebar"] a[href*="/library"]')
        return library_nav.count() > 0
    except Exception:  # noqa: BLE001
        return False


def _locate_track_row(page: Any, track_id: int, title: str) -> Any | None:
    """Locate exact song row inside [data-testid='tracklist'] on music.apple.com."""
    tracklist = page.locator('[data-testid="tracklist"]')
    if tracklist.count() == 0:
        return None

    # 1. Exact song ID href match (most reliable)
    by_id = tracklist.locator(f'.songs-list-row:has(a[href*="/{track_id}"])')
    if by_id.count() > 0:
        return by_id.first

    # 2. Highlighted/selected row from ?i=<trackId> URL parameter
    selected = tracklist.locator(".songs-list-row.songs-list-row--selected")
    if selected.count() > 0:
        return selected.first

    # 3. Fallback by song title
    if title:
        by_title = tracklist.locator(".songs-list-row").filter(has_text=title)
        if by_title.count() > 0:
            return by_title.first

    return None


def _find_visible_menu_item(page: Any, candidates: tuple[str, ...]) -> Any | None:
    selector = (
        'button.contextual-menu__item, [role="menuitem"], '
        '.contextual-menu button, .menu-item'
    )
    items = page.locator(selector)
    count = items.count()
    for i in range(count):
        el = items.nth(i)
        try:
            if not el.is_visible():
                continue
            txt = (el.inner_text() or "").strip()
            for target in candidates:
                if txt == target or target.lower() in txt.lower():
                    return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _list_visible_menu_texts(page: Any) -> list[str]:
    selector = (
        'button.contextual-menu__item, [role="menuitem"], '
        '.contextual-menu button, .menu-item'
    )
    texts: list[str] = []
    items = page.locator(selector)
    for i in range(min(items.count(), 30)):
        try:
            el = items.nth(i)
            if el.is_visible():
                t = (el.inner_text() or "").strip()
                if t:
                    texts.append(t)
        except Exception:  # noqa: BLE001
            continue
    return texts


def _open_row_menu(page: Any, row: Any) -> bool:
    """Hover row and open contextual menu, retrying once if menu didn't open or hydrate."""
    row.scroll_into_view_if_needed()
    trigger = row.locator("button.contextual-menu__trigger").first
    if trigger.count() == 0:
        return False

    for attempt in range(2):
        with contextlib.suppress(Exception):
            row.hover()
        page.wait_for_timeout(200)
        trigger.click(force=True)
        page.wait_for_timeout(800)
        seen = _list_visible_menu_texts(page)
        # If menu didn't open ([]) or only hydrated the 4 unauthenticated items, retry once
        if len(seen) > 4:
            return True
        if attempt == 0:
            page.keyboard.press("Escape")
            page.wait_for_timeout(1200)
    return bool(_list_visible_menu_texts(page))


def _add_row_to_playlist(
    page: Any,
    row: Any,
    playlist_name: str,
    log_fn: Any,
) -> tuple[bool, str]:
    """Open row's contextual menu and add song to target playlist (creating if needed)."""
    if not _open_row_menu(page, row):
        return False, "未能展开歌曲行的「更多 (…)」菜单"

    add_pl_item = _find_visible_menu_item(
        page, ("添加到歌单", "添加到播放列表", "Add to Playlist")
    )
    if add_pl_item is None:
        seen = _list_visible_menu_texts(page)
        log_fn(f"  ⚠️ 右键菜单未出现「添加到歌单」，当前可见菜单项：{seen}")
        page.keyboard.press("Escape")
        return False, "未找到「添加到歌单」菜单项（请确认已登录且该曲目有播放权限）"

    # Hover/click to expand playlist submenu
    with contextlib.suppress(Exception):
        add_pl_item.hover()
    page.wait_for_timeout(700)

    # Check if target playlist already appears in submenu and click it
    target_pl_item = _find_visible_menu_item(page, (playlist_name,))
    if target_pl_item is not None:
        try:
            target_pl_item.click(timeout=3000)
            page.wait_for_timeout(1000)
            return True, f"已添加到歌单「{playlist_name}」"
        except Exception:  # noqa: BLE001
            pass

    # Submenu didn't open on hover alone — click parent menu item
    add_pl_item.click()
    page.wait_for_timeout(700)

    target_pl_item = _find_visible_menu_item(page, (playlist_name,))
    if target_pl_item is not None:
        target_pl_item.click(timeout=4000)
        page.wait_for_timeout(1000)
        return True, f"已添加到歌单「{playlist_name}」"

    # Otherwise create new playlist from submenu
    new_pl_item = _find_visible_menu_item(
        page, ("新歌单", "新建播放列表", "New Playlist")
    )
    if new_pl_item is None:
        seen = _list_visible_menu_texts(page)
        log_fn(f"  ⚠️ 歌单子菜单中未找到「{playlist_name}」或「新歌单」，可见项：{seen}")
        page.keyboard.press("Escape")
        return False, "未在子菜单找到目标歌单或新建选项"

    new_pl_item.click()
    page.wait_for_timeout(900)

    # Fill playlist name modal/input if Apple Music prompts for playlist title
    name_input = page.locator(
        '[role="dialog"] input[type="text"], '
        'input[placeholder*="歌单"], input[placeholder*="播放列表"], input[placeholder*="Playlist"], '
        '.modal input[type="text"]'
    )
    if name_input.count() > 0 and name_input.first.is_visible():
        name_input.first.fill(playlist_name)
        page.wait_for_timeout(250)
        confirm_btn = page.locator(
            '[role="dialog"] button[type="submit"], '
            '[role="dialog"] button:has-text("创建"), '
            '[role="dialog"] button:has-text("Create"), '
            '[role="dialog"] button:has-text("保存"), '
            '[role="dialog"] button:has-text("Save")'
        )
        if confirm_btn.count() > 0:
            confirm_btn.first.click()
        else:
            name_input.first.press("Enter")
        page.wait_for_timeout(1200)
        return True, f"已新建歌单「{playlist_name}」并添加此曲"

    return True, "已触发新建歌单（若歌单名为默认名称，可稍后在 Apple Music 重命名）"


def run_web_apple_music_import(
    playlist_name: str,
    tracks: list[TrackMatchItem],
    country: str = "cn",
    cache_dir: Path | None = None,
    live_state: WebAppleMusicResult | None = None,
    stop_event: threading.Event | None = None,
    login_timeout_sec: int = 180,
    retry_track_ids: list[str] | None = None,
    previous_items: list[dict[str, Any]] | None = None,
    on_finish: Any = None,
) -> WebAppleMusicResult:
    """Automate music.apple.com in a dedicated local Chrome profile via Playwright."""
    resolved_cache = (cache_dir or (Path.home() / ".cache" / "bili2am")).expanduser()
    confirmed = filter_confirmed_tracks(tracks)
    target_pl = playlist_name.strip() or "B站导入歌单"

    state = live_state or WebAppleMusicResult(success=True, playlist_name=target_pl, running=True)
    state.playlist_name = target_pl
    state.running = True

    def _log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        state.logs.append(line)
        print(f"[web_am] {line}", flush=True)
        logger.info(msg)

    def _finalize(ok: bool, message: str) -> WebAppleMusicResult:
        state.success = ok
        state.running = False
        state.message = message
        state.log_path = _write_web_log(resolved_cache, state.logs)
        if on_finish:
            with contextlib.suppress(Exception):
                on_finish(state)
        return state

    if not is_playwright_available():
        _log("❌ 未安装 playwright，请先运行 `pip install playwright`。")
        return _finalize(False, "未安装 playwright，请先运行 `pip install playwright`。")

    if not confirmed:
        _log("无已确认曲目，中止操作。")
        return _finalize(False, "没有可导入的已匹配曲目。")

    retry_set = set(retry_track_ids) if retry_track_ids else None
    prev_by_id = {str(p.get("id")): p for p in (previous_items or []) if p.get("id")}

    state.items = []
    targets_to_run: list[WebTrackAction] = []
    for item in confirmed:
        cand = item.active_candidate
        assert cand is not None
        web_url = cand.track_view_url or f"https://music.apple.com/{country}/song/{cand.track_id}"
        if retry_set is not None and item.id not in retry_set:
            prev = prev_by_id.get(item.id)
            act = WebTrackAction(
                id=item.id,
                title=cand.track_name,
                artist=cand.artist_name,
                track_id=cand.track_id,
                web_url=web_url,
                status=str((prev or {}).get("status") or "added"),
                detail=str((prev or {}).get("detail") or "此前已处理"),
            )
            state.items.append(act)
            continue

        act = WebTrackAction(
            id=item.id,
            title=cand.track_name,
            artist=cand.artist_name,
            track_id=cand.track_id,
            web_url=web_url,
            status="pending",
            detail="等待处理",
        )
        state.items.append(act)
        targets_to_run.append(act)

    if not targets_to_run:
        return _finalize(True, "没有需要重试的曲目。")

    state.total_count = len(targets_to_run)
    state.processed_count = 0
    state.added_count = sum(1 for x in state.items if x.status == "added")
    state.failed_count = sum(1 for x in state.items if x.status == "failed")

    channel = detect_browser_channel()
    # Use a dedicated profile directory free of Playwright's --use-mock-keychain cookies
    # so macOS Chrome Safe Storage Keychain persists login across browser restarts.
    profile_dir = resolved_cache / "chrome_user_data"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cdp_url = f"http://127.0.0.1:{DEFAULT_CDP_PORT}"
    _log(
        f"准备连接专属浏览器配置目录：{profile_dir}（浏览器通道：{channel or 'default'}），"
        f"目标歌单：「{target_pl}」，共 {len(state.items)} 首。"
    )

    from playwright.sync_api import sync_playwright

    def _is_cdp_ready() -> bool:
        try:
            with httpx.Client(trust_env=False, timeout=1.0) as local_http:
                r = local_http.get(f"{cdp_url}/json/version")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def _ensure_cdp_window() -> None:
        """If macOS Chrome is alive in Dock with 0 open windows, open a blank target first."""
        with contextlib.suppress(Exception), httpx.Client(trust_env=False, timeout=2.0) as local_http:
            targets = local_http.get(f"{cdp_url}/json/list").json()
            if isinstance(targets, list) and len(targets) == 0:
                local_http.put(f"{cdp_url}/json/new?about:blank")
                time.sleep(0.4)

    try:
        with sync_playwright() as pw:
            use_cdp = False
            persistent_ctx = None
            worker_page = None

            if CHROME_BIN_PATH.exists():
                if not _is_cdp_ready():
                    _log("启动独立 Google Chrome 实例（使用系统钥匙串保存登录态，可随时关闭窗口）...")
                    subprocess.Popen(
                        [
                            str(CHROME_BIN_PATH),
                            f"--user-data-dir={profile_dir}",
                            f"--remote-debugging-port={DEFAULT_CDP_PORT}",
                            "--no-first-run",
                            "--no-default-browser-check",
                        ],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    for _ in range(25):
                        if _is_cdp_ready():
                            break
                        time.sleep(0.3)
                else:
                    _log("检测到已运行的专属 Chrome 窗口，直接复用当前会话...")

                if _is_cdp_ready():
                    _ensure_cdp_window()
                    browser = pw.chromium.connect_over_cdp(cdp_url)
                    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                    use_cdp = True
                else:
                    _log("⚠️ 独立 Chrome 调试端口未响应，回退至 Playwright 托管启动模式。")

            if not use_cdp:
                launch_kwargs: dict[str, Any] = {
                    "user_data_dir": str(profile_dir),
                    "headless": False,
                    "viewport": {"width": 1280, "height": 840},
                    "ignore_default_args": ["--use-mock-keychain"],
                }
                if channel:
                    launch_kwargs["channel"] = channel
                persistent_ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
                ctx = persistent_ctx

            try:
                existing_am = [p for p in ctx.pages if "music.apple.com" in (p.url or "")]
                if existing_am:
                    page = existing_am[0]
                elif ctx.pages and use_cdp:
                    page = ctx.pages[0]
                else:
                    page = ctx.new_page()
                    worker_page = page

                home_url = f"https://music.apple.com/{country}"
                if "music.apple.com" not in (page.url or ""):
                    _log(f"正在打开网页版 Apple Music：{home_url}")
                    page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
                else:
                    _log(f"复用已打开的 Apple Music 页面：{page.url}")

                # Wait up to 8s for auth header/sidebar hydration before deciding login status
                for _ in range(8):
                    if _is_logged_in(page):
                        break
                    page.wait_for_timeout(1000)

                if not _is_logged_in(page):
                    state.waiting_for_login = True
                    _log("🔐 检测到尚未登录 Apple Music，请在弹出的专属 Chrome 窗口中登录（建议勾选保持登录/信任此浏览器）。")
                    sign_in_btn = page.locator('button[data-testid="sign-in-button"]')
                    if sign_in_btn.count() > 0 and sign_in_btn.first.is_visible():
                        with contextlib.suppress(Exception):
                            sign_in_btn.first.click()

                    deadline = time.time() + login_timeout_sec
                    while time.time() < deadline:
                        if stop_event and stop_event.is_set():
                            _log("⏹ 用户已取消操作。")
                            break
                        if _is_logged_in(page):
                            break
                        time.sleep(1.5)

                    state.waiting_for_login = False
                    if not _is_logged_in(page):
                        _log("❌ 等待登录超时或已取消，中止导入。")
                        return _finalize(False, "等待网页版 Apple Music 登录超时或已取消。")
                    _log("✅ 检测到已登录 Apple Music，开始逐首添加曲目！")
                else:
                    _log("✅ 登录态有效，开始逐首添加曲目！")

                for idx, act in enumerate(targets_to_run, start=1):
                    if stop_event and stop_event.is_set():
                        _log("⏹ 收到停止指令，已中止后续添加。")
                        break

                    _log(f"[{idx}/{len(targets_to_run)}] 打开歌曲页面：{act.artist} - {act.title}")
                    try:
                        page.goto(act.web_url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_selector('[data-testid="tracklist"] .songs-list-row', timeout=15000)
                        page.wait_for_timeout(1300)

                        row = _locate_track_row(page, act.track_id, act.title)
                        if row is None:
                            act.status = "failed"
                            act.detail = "页面曲目列表中未定位到该曲目行"
                            _log(f"  ❌ 未在曲目列表中找到：{act.title} (id={act.track_id})")
                        else:
                            ok, msg = _add_row_to_playlist(page, row, target_pl, _log)
                            if ok:
                                act.status = "added"
                                act.detail = msg
                                _log(f"  ✅ {msg}：{act.artist} - {act.title}")
                            else:
                                act.status = "failed"
                                act.detail = msg
                                _log(f"  ❌ 添加失败：{msg}")
                    except Exception as exc:  # noqa: BLE001
                        act.status = "failed"
                        err_msg = str(exc)
                        act.detail = err_msg[:140]
                        _log(f"  ❌ 处理异常：{err_msg}")
                        if "Target page, context or browser has been closed" in err_msg:
                            _log("⏹ 检测到浏览器窗口已关闭，中止后续操作。")
                            break

                    state.processed_count = idx
                    state.added_count = sum(1 for x in state.items if x.status == "added")
                    state.failed_count = sum(1 for x in state.items if x.status == "failed")
                    time.sleep(0.5)
            finally:
                if persistent_ctx is not None:
                    persistent_ctx.close()
                elif worker_page is not None:
                    with contextlib.suppress(Exception):
                        worker_page.close()
    except Exception as exc:  # noqa: BLE001
        _log(f"❌ 浏览器启动或运行失败：{exc}")
        return _finalize(False, f"网页版 Apple Music 启动失败：{exc}")

    state.added_count = sum(1 for x in state.items if x.status == "added")
    state.failed_count = sum(1 for x in state.items if x.status == "failed")
    ok_overall = state.failed_count == 0 and state.added_count > 0
    msg_final = (
        f"已添加 {state.added_count} 首到歌单「{target_pl}」"
        + (f"，失败 {state.failed_count} 首（可点击重试）" if state.failed_count else "")
    )
    return _finalize(ok_overall, msg_final)


class WebAppleMusicJobManager:
    """Manage background Playwright Web Apple Music import with live polling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: WebAppleMusicResult | None = None
        self._stop_event: threading.Event | None = None

    def start(
        self,
        playlist_name: str,
        tracks: list[TrackMatchItem],
        country: str,
        cache_dir: Path,
        history_id: str | None = None,
        retry_track_ids: list[str] | None = None,
        previous_items: list[dict[str, Any]] | None = None,
        on_finish: Any = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._job and self._job.running:
                return asdict(self._job)
            job = WebAppleMusicResult(
                success=True,
                playlist_name=playlist_name,
                running=True,
                total_count=len(retry_track_ids) if retry_track_ids else len(filter_confirmed_tracks(tracks)),
                history_id=history_id,
                message="正在启动专属 Chrome 窗口访问网页版 Apple Music...",
            )
            stop_ev = threading.Event()
            self._job = job
            self._stop_event = stop_ev

        def _worker() -> None:
            run_web_apple_music_import(
                playlist_name=playlist_name,
                tracks=tracks,
                country=country,
                cache_dir=cache_dir,
                live_state=job,
                stop_event=stop_ev,
                retry_track_ids=retry_track_ids,
                previous_items=previous_items,
                on_finish=on_finish,
            )

        threading.Thread(target=_worker, daemon=True).start()
        return asdict(job)

    def status(self) -> dict[str, Any] | None:
        with self._lock:
            return asdict(self._job) if self._job else None

    def stop(self) -> dict[str, Any] | None:
        with self._lock:
            if self._stop_event:
                self._stop_event.set()
            return asdict(self._job) if self._job else None
