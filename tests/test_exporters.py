"""Tests for TXT/CSV file exporters and Web Apple Music importer."""

from pathlib import Path

from bili2am.exporters.file_export import (
    export_tracks_to_csv,
    export_tracks_to_txt,
    filter_confirmed_tracks,
)
from bili2am.exporters.web_apple_music import (
    _find_visible_menu_item,
    _is_logged_in,
    detect_browser_channel,
    run_web_apple_music_import,
)
from bili2am.models import AppleMusicCandidate, ExtractionSource, MatchStatus, TrackMatchItem


def _sample_items() -> list[TrackMatchItem]:
    cand = AppleMusicCandidate(
        track_id=1001,
        track_name="晴天",
        artist_name="周杰伦",
        collection_name="叶惠美",
        track_view_url="https://music.apple.com/cn/album/1?i=1001",
        confidence=0.99,
    )
    return [
        TrackMatchItem(
            id="trk_001",
            raw_text="周杰伦 - 晴天",
            title="晴天",
            artist="周杰伦",
            source=ExtractionSource.PAGES,
            status=MatchStatus.MATCHED,
            selected=True,
            candidates=[cand],
        ),
        TrackMatchItem(
            id="trk_002",
            raw_text="某未收录翻唱",
            title="某未收录翻唱",
            artist="",
            source=ExtractionSource.PAGES,
            status=MatchStatus.MISSING,
            selected=False,
            candidates=[],
        ),
    ]


def test_export_excludes_missing_tracks(tmp_path: Path):
    items = _sample_items()
    confirmed = filter_confirmed_tracks(items)
    assert len(confirmed) == 1

    txt_path = export_tracks_to_txt(items, tmp_path, "测试:歌单")
    csv_path = export_tracks_to_csv(items, tmp_path, "测试:歌单")

    txt_body = txt_path.read_text(encoding="utf-8")
    csv_body = csv_path.read_text(encoding="utf-8")
    assert "周杰伦 - 晴天" in txt_body
    assert "某未收录翻唱" not in txt_body
    assert "1001" in csv_body
    assert "某未收录翻唱" not in csv_body


def test_web_apple_music_missing_playwright_and_empty(monkeypatch, tmp_path: Path):
    items = _sample_items()
    monkeypatch.setattr("bili2am.exporters.web_apple_music.is_playwright_available", lambda: False)
    res = run_web_apple_music_import("测试歌单", items, cache_dir=tmp_path)
    assert not res.success
    assert "playwright" in res.message
    assert Path(res.log_path).exists()

    monkeypatch.setattr("bili2am.exporters.web_apple_music.is_playwright_available", lambda: True)
    res_empty = run_web_apple_music_import("测试歌单", [items[1]], cache_dir=tmp_path)
    assert not res_empty.success
    assert "没有可导入" in res_empty.message


def test_detect_browser_channel_returns_known_or_none():
    ch = detect_browser_channel()
    assert ch in (None, "chrome", "msedge")


def test_web_apple_music_menu_supports_chinese_and_english_labels():
    class _FakeEl:
        def __init__(self, txt: str, vis: bool = True) -> None:
            self._txt = txt
            self._vis = vis

        def is_visible(self) -> bool:
            return self._vis

        def inner_text(self) -> str:
            return self._txt

    class _FakeLocator:
        def __init__(self, items: list[_FakeEl]) -> None:
            self._items = items

        def count(self) -> int:
            return len(self._items)

        def nth(self, idx: int) -> _FakeEl:
            return self._items[idx]

    class _FakePage:
        def locator(self, selector: str) -> _FakeLocator:  # noqa: ARG002
            return _FakeLocator([_FakeEl("置顶歌曲"), _FakeEl("添加到歌单"), _FakeEl("新歌单")])

    found = _find_visible_menu_item(_FakePage(), ("添加到歌单", "添加到播放列表", "Add to Playlist"))
    assert found is not None
    assert found.inner_text() == "添加到歌单"


def test_is_logged_in_requires_positive_session_signal():
    class _Loc:
        def __init__(self, cnt: int, vis: bool = True) -> None:
            self._cnt = cnt
            self._vis = vis
            self.first = self

        def count(self) -> int:
            return self._cnt

        def is_visible(self) -> bool:
            return self._vis

        def filter(self, **kwargs):  # noqa: ARG002
            return self

    class _MockPage:
        def __init__(self, url: str, selectors: dict[str, tuple[int, bool]]) -> None:
            self.url = url
            self._selectors = selectors

        def locator(self, sel: str) -> _Loc:
            cnt, vis = self._selectors.get(sel, (0, False))
            return _Loc(cnt, vis)

    assert not _is_logged_in(_MockPage("https://music.apple.com/cn", {}))
    assert not _is_logged_in(
        _MockPage(
            "https://idmsa.apple.com/auth",
            {'nav a[href*="/library"], [data-testid="sidebar"] a[href*="/library"]': (1, True)},
        )
    )
    assert _is_logged_in(
        _MockPage(
            "https://music.apple.com/cn",
            {'nav a[href*="/library"], [data-testid="sidebar"] a[href*="/library"]': (1, True)},
        )
    )


def test_web_apple_music_retry_preserves_previous_items(monkeypatch, tmp_path: Path):
    items = _sample_items()
    prev = [
        {
            "id": "trk_001",
            "title": "晴天",
            "artist": "周杰伦",
            "track_id": 1001,
            "web_url": "https://music.apple.com/cn/album/1?i=1001",
            "status": "added",
            "detail": "已添加",
        }
    ]
    monkeypatch.setattr("bili2am.exporters.web_apple_music.is_playwright_available", lambda: True)
    res = run_web_apple_music_import(
        "测试歌单",
        items,
        cache_dir=tmp_path,
        retry_track_ids=["trk_002"],
        previous_items=prev,
    )
    assert res.success
    assert "没有需要重试" in res.message
    assert len(res.items) == 1
    assert res.items[0].id == "trk_001"
    assert res.items[0].status == "added"

