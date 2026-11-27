"""End-to-end extraction, normalization, and Apple Music verification pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from bili2am.config import AppConfig
from bili2am.cookies import resolve_bilibili_cookies
from bili2am.extractors.bilibili_api import BVID_RE, extract_text_tracks, fetch_video_info
from bili2am.extractors.shazam_fallback import run_shazam_fallback
from bili2am.extractors.vision_ocr import run_ocr_extraction
from bili2am.matcher.cleaner import normalize_with_optional_llm
from bili2am.matcher.itunes_search import verify_tracks_in_apple_music
from bili2am.models import ExtractionSource, PipelineResult, RawTrack

ProgressCallback = Callable[[str, float, int, int, str], None]


def _suggest_playlist_name(title: str, bvid: str) -> str:
    cleaned = title.strip()
    if len(cleaned) > 50:
        cleaned = cleaned[:50].rstrip()
    return cleaned or f"Bilibili_{bvid}"


def run_pipeline(
    url_or_bvid: str,
    cfg: AppConfig,
    mode: Literal["auto", "text", "ocr", "shazam"] = "auto",
    use_cache: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> PipelineResult:
    """Run Bilibili tracklist extraction and Apple Music catalog verification."""
    def _report(stage: str, pct: float, cur: int, tot: int, msg: str) -> None:
        if progress_cb:
            progress_cb(stage, pct, cur, tot, msg)

    cache_dir = Path(cfg.cache_dir).expanduser()
    _report("extract", 0.05, 0, 1, "正在准备浏览器 Cookie 与环境…")
    cookie_jar, cookie_res = resolve_bilibili_cookies(
        enabled=cfg.cookie.enabled,
        priority=cfg.cookie.priority,
    )

    # Check local cache early when BV id is directly visible in input
    early_bvid_match = BVID_RE.search(url_or_bvid.strip())
    if use_cache and mode == "auto" and early_bvid_match:
        early_cache = cache_dir / early_bvid_match.group(1) / "pipeline_result.json"
        if early_cache.exists():
            try:
                raw_cached = json.loads(early_cache.read_text(encoding="utf-8"))
                if raw_cached.get("cache_version", 1) >= 3 and raw_cached.get("storefront") == cfg.storefront:
                    cached = PipelineResult.model_validate(raw_cached)
                    _report("done", 1.0, len(cached.tracks), len(cached.tracks), "已加载本地缓存")
                    return cached.model_copy(update={"cookie_info": cookie_res})
            except Exception:  # noqa: BLE001
                pass

    _report("extract", 0.12, 0, 1, "正在获取 B 站视频简介、评论与楼中楼…")
    video = fetch_video_info(url_or_bvid, cookies=cookie_jar)
    cache_file = cache_dir / video.bvid / "pipeline_result.json"

    if use_cache and mode == "auto" and cache_file.exists():
        try:
            raw_cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if raw_cached.get("cache_version", 1) >= 3 and raw_cached.get("storefront") == cfg.storefront:
                cached = PipelineResult.model_validate(raw_cached)
                _report("done", 1.0, len(cached.tracks), len(cached.tracks), "已加载本地缓存")
                return cached.model_copy(update={"cookie_info": cookie_res})
        except Exception:  # noqa: BLE001
            pass

    raw_tracks: list[RawTrack] = []
    notes: list[str] = []
    raw_blob = ""
    primary_source = ExtractionSource.DESCRIPTION
    method_label = "text"

    if mode in ("auto", "text"):
        raw_tracks, text_notes, raw_blob = extract_text_tracks(video)
        notes.extend(text_notes)
        if raw_tracks:
            primary_source = raw_tracks[0].source
            method_label = primary_source.value

    if mode == "ocr" or (mode == "auto" and len(raw_tracks) < 2):
        _report("extract", 0.22, 0, 1, "正在下载视频并执行 Apple Vision OCR…")
        try:
            ocr_tracks, ocr_blob = run_ocr_extraction(
                url=video.url,
                bvid=video.bvid,
                cache_dir=cache_dir,
                ocr_cfg=cfg.ocr,
                cookie_browser=cookie_res.selected_browser,
            )
            if len(ocr_tracks) >= len(raw_tracks) and ocr_tracks:
                raw_tracks = ocr_tracks
                raw_blob = ocr_blob
                primary_source = ExtractionSource.OCR
                method_label = "ocr"
                notes.append(f"通过 macOS Apple Vision OCR 从视频画面识别出 {len(ocr_tracks)} 条候选。")
            else:
                notes.append("视频画面 OCR 未识别到更多有效曲目。")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"画面 OCR 阶段跳过/失败: {exc}")

    if mode == "shazam" or (mode == "auto" and len(raw_tracks) < 2):
        _report("extract", 0.30, 0, 1, "正在提取音频并调用 shazamio 听歌识曲…")
        known_starts = [t.start_sec for t in raw_tracks if t.start_sec is not None] or None
        try:
            shazam_tracks = run_shazam_fallback(
                url=video.url,
                bvid=video.bvid,
                duration_sec=video.duration_sec,
                cache_dir=cache_dir,
                shazam_cfg=cfg.shazam,
                cookie_browser=cookie_res.selected_browser,
                known_starts=known_starts,
            )
            if shazam_tracks:
                raw_tracks = shazam_tracks
                raw_blob = ""
                primary_source = ExtractionSource.SHAZAM
                method_label = "shazam"
                notes.append(f"通过 shazamio 听歌识曲保底识别出 {len(shazam_tracks)} 首曲目。")
            else:
                notes.append("shazamio 听歌识曲未识别出有效曲目。")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"shazamio 保底阶段跳过/失败: {exc}")

    _report("extract", 0.38, 1, 1, f"正在清洗规范化歌单（{len(raw_tracks)} 条）…")
    if primary_source == ExtractionSource.SHAZAM:
        normalized = raw_tracks
        llm_used = "none (shazam structured)"
    else:
        normalized, llm_used = normalize_with_optional_llm(
            raw_blob=raw_blob,
            fallback_tracks=raw_tracks,
            llm_cfg=cfg.llm,
            source=primary_source,
        )

    total_norm = len(normalized)
    _report("match", 0.40, 0, total_norm, f"正在核对 Apple Music ({cfg.storefront.upper()}) 曲库…")

    def _on_match_progress(done: int, tot: int, label: str) -> None:
        ratio = done / max(1, tot)
        pct = 0.40 + 0.58 * ratio
        _report("match", pct, done, tot, f"正在匹配 Apple Music ({done}/{tot}): {label}")

    try:
        matched_tracks = verify_tracks_in_apple_music(
            tracks=normalized,
            country=cfg.storefront,
            progress_cb=_on_match_progress,
        )
    except TypeError:
        # Backward compatibility if tests monkeypatch verify_tracks_in_apple_music without progress_cb
        matched_tracks = verify_tracks_in_apple_music(
            tracks=normalized,
            country=cfg.storefront,
        )

    result = PipelineResult(
        video=video,
        cookie_info=cookie_res,
        extraction_method=method_label,
        extraction_notes=notes,
        storefront=cfg.storefront,
        playlist_name=_suggest_playlist_name(video.title, video.bvid),
        tracks=matched_tracks,
        llm_used=llm_used,
    )

    if matched_tracks:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    _report("done", 1.0, len(matched_tracks), len(matched_tracks), "解析与曲库校验完成")
    return result
