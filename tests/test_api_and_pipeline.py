"""Tests for FastAPI confirmation UI server and end-to-end mocked pipeline."""

from pathlib import Path

from fastapi.testclient import TestClient

from bili2am.config import AppConfig
from bili2am.models import (
    AppleMusicCandidate,
    CookieResolution,
    ExtractionSource,
    MatchStatus,
    PipelineResult,
    RawTrack,
    TrackMatchItem,
    VideoChapter,
    VideoInfo,
)
from bili2am.pipeline import run_pipeline
from bili2am.ui.confirm_server import create_app


def _sample_pipeline_result() -> PipelineResult:
    return PipelineResult(
        video=VideoInfo(bvid="BV1xx411c7mD", title="测试歌单合集", owner="UP主"),
        cookie_info=CookieResolution(enabled=False, notice="匿名模式"),
        extraction_method="pages",
        storefront="cn",
        playlist_name="测试歌单合集",
        tracks=[
            TrackMatchItem(
                id="trk_001",
                raw_text="周杰伦 - 晴天",
                title="晴天",
                artist="周杰伦",
                source=ExtractionSource.PAGES,
                status=MatchStatus.MATCHED,
                selected=True,
                candidates=[
                    AppleMusicCandidate(
                        track_id=1001,
                        track_name="晴天",
                        artist_name="周杰伦",
                        collection_name="叶惠美",
                        confidence=0.98,
                    )
                ],
            ),
            TrackMatchItem(
                id="trk_002",
                raw_text="冷门曲 - 无版权",
                title="无版权",
                artist="冷门曲",
                source=ExtractionSource.PAGES,
                status=MatchStatus.MISSING,
                selected=False,
                candidates=[],
                note="Apple Music (CN) 曲库未检索到可播放版本",
            ),
        ],
    )


def test_web_ui_api_endpoints():
    cfg = AppConfig()
    app = create_app(cfg=cfg, initial_result=_sample_pipeline_result())
    client = TestClient(app)

    r_html = client.get("/")
    assert r_html.status_code == 200
    assert "bili2am" in r_html.text

    r_state = client.get("/api/state")
    assert r_state.status_code == 200
    body = r_state.json()
    assert body["result"]["video"]["bvid"] == "BV1xx411c7mD"
    assert len(body["recommended_models"]) >= 2

    # Ensure unapproved model name is rejected
    r_pull = client.post("/api/ollama/pull", json={"model": "unknown-huge-model"})
    assert r_pull.status_code == 400

    # Test web_apple_music async import endpoint & status/stop routes
    import bili2am.exporters.web_apple_music as wam

    orig_web_run = wam.run_web_apple_music_import
    wam.run_web_apple_music_import = lambda **kwargs: wam.WebAppleMusicResult(
        success=True,
        playlist_name=kwargs["playlist_name"],
        running=False,
        added_count=1,
        message="mocked web ok",
    )
    try:
        r_web = client.post(
            "/api/import",
            json={
                "playlist_name": "测试歌单",
                "method": "web_apple_music",
                "tracks": body["result"]["tracks"],
            },
        )
        assert r_web.status_code == 200
        assert r_web.json()["async"] is True
        r_stop = client.post("/api/web-am/stop")
        assert r_stop.status_code == 200
        r_status = client.get("/api/web-am/status")
        assert r_status.status_code == 200
    finally:
        wam.run_web_apple_music_import = orig_web_run


def test_run_pipeline_uses_cache_and_mocked_extractors(monkeypatch, tmp_path: Path):
    cfg = AppConfig(cache_dir=str(tmp_path))

    monkeypatch.setattr(
        "bili2am.pipeline.resolve_bilibili_cookies",
        lambda enabled, priority: (None, CookieResolution(enabled=enabled, notice="mocked")),
    )
    monkeypatch.setattr(
        "bili2am.pipeline.fetch_video_info",
        lambda url, cookies=None: VideoInfo(
            bvid="BV1mocked001",
            title="Mock 合集",
            url="https://www.bilibili.com/video/BV1mocked001",
            pages=[
                VideoChapter(title="周杰伦 - 晴天", start_sec=0),
                VideoChapter(title="林俊杰 - 江南", start_sec=240),
            ],
        ),
    )

    def fake_verify(tracks: list[RawTrack], country: str = "cn"):
        return [
            TrackMatchItem(
                id=f"trk_{i:03d}",
                raw_text=t.raw_text,
                title=t.title,
                artist=t.artist,
                source=t.source,
                status=MatchStatus.MATCHED,
                selected=True,
                candidates=[
                    AppleMusicCandidate(
                        track_id=100 + i,
                        track_name=t.title,
                        artist_name=t.artist,
                        confidence=0.95,
                        country=country,
                    )
                ],
            )
            for i, t in enumerate(tracks, start=1)
        ]

    monkeypatch.setattr("bili2am.pipeline.verify_tracks_in_apple_music", fake_verify)

    res = run_pipeline("BV1mocked001", cfg=cfg, mode="auto", use_cache=True)
    assert res.video.bvid == "BV1mocked001"
    assert len(res.tracks) == 2
    assert (tmp_path / "BV1mocked001" / "pipeline_result.json").exists()

    # Second run should hit local cache immediately without calling fetch_video_info
    monkeypatch.setattr(
        "bili2am.pipeline.fetch_video_info",
        lambda url, cookies=None: (_ for _ in ()).throw(AssertionError("should not fetch network")),
    )
    res_cached = run_pipeline("BV1mocked001", cfg=cfg, mode="auto", use_cache=True)
    assert res_cached.video.bvid == "BV1mocked001"
    assert len(res_cached.tracks) == 2


def test_cli_parser_supports_api_only_and_build_app():
    from bili2am.cli import build_parser

    p = build_parser()
    args = p.parse_args(["run", "BV182nL6UEiG", "--api-only", "--port", "9999"])
    assert args.api_only is True
    assert args.port == 9999

    args2 = p.parse_args(["build-app"])
    assert args2.command == "build-app"


def test_history_store_crud_and_search(tmp_path: Path):
    from bili2am.history import HistoryStore

    store = HistoryStore(tmp_path)
    rec_ok = store.new_record(input_url="BV1ok0001", mode="auto", storefront="cn")
    rec_ok.summary.status = "success"
    rec_ok.summary.bvid = "BV1xx411c7mD"
    rec_ok.summary.title = "测试歌单合集"
    rec_ok.result = _sample_pipeline_result()
    store.save_record(rec_ok)

    rec_fail = store.new_record(input_url="BV1fail002", mode="ocr", storefront="us")
    rec_fail.summary.status = "failed"
    rec_fail.summary.error_message = "Bilibili HTTP 412"
    store.save_record(rec_fail)

    all_items = store.list_summaries()
    assert len(all_items) == 2
    assert {item.status for item in all_items} == {"success", "failed"}

    filtered = store.list_summaries(query="412")
    assert len(filtered) == 1
    assert filtered[0].id == rec_fail.summary.id

    loaded = store.get_record(rec_ok.summary.id)
    assert loaded is not None
    assert loaded.result is not None
    assert loaded.result.video.bvid == "BV1xx411c7mD"

    assert store.delete_record(rec_fail.summary.id) is True
    assert store.delete_record(rec_fail.summary.id) is False
    assert len(store.list_summaries()) == 1

    removed = store.clear_all()
    assert removed == 1
    assert store.list_summaries() == []


def test_async_analyze_failure_and_history_endpoints(monkeypatch, tmp_path: Path):
    cfg = AppConfig(cache_dir=str(tmp_path))
    app = create_app(cfg=cfg)
    client = TestClient(app)

    def boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("mock network failure")

    monkeypatch.setattr("bili2am.ui.confirm_server.run_pipeline", boom)

    r_start = client.post(
        "/api/analyze/start",
        json={"url": "BV1err001", "mode": "auto", "storefront": "cn", "use_cache": False},
    )
    assert r_start.status_code == 200
    assert r_start.json()["history_id"]

    import time

    status_body = None
    for _ in range(40):
        status_body = client.get("/api/analyze/status").json()
        if not status_body["running"]:
            break
        time.sleep(0.05)

    assert status_body is not None
    assert status_body["running"] is False
    assert "mock network failure" in (status_body["error"] or "")

    r_hist = client.get("/api/history")
    assert r_hist.status_code == 200
    items = r_hist.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["input_url"] == "BV1err001"

    rec_id = items[0]["id"]
    r_get = client.get(f"/api/history/{rec_id}")
    assert r_get.status_code == 200
    assert r_get.json()["summary"]["error_message"] == "mock network failure"

    r_del = client.delete(f"/api/history/{rec_id}")
    assert r_del.status_code == 200
    assert client.get("/api/history").json()["items"] == []

