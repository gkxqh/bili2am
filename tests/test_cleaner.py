"""Tests for rule-based track cleaning and optional LLM fallback."""

import httpx

from bili2am.config import LLMConfig
from bili2am.matcher.cleaner import (
    normalize_tracks_rule_based,
    normalize_with_optional_llm,
    split_artist_title,
)
from bili2am.models import ExtractionSource, RawTrack


def test_split_artist_title_formats():
    assert split_artist_title("01. [00:00] 周杰伦 - 晴天 【Hi-Res】") == ("周杰伦", "晴天")
    assert split_artist_title("2、林俊杰《江南》(官方)") == ("林俊杰", "江南")
    assert split_artist_title("Taylor Swift — Cruel Summer") == ("Taylor Swift", "Cruel Summer")
    assert split_artist_title("03:22 Pure Song Title") == ("", "Pure Song Title")


def test_normalize_tracks_deduplicates():
    raw = [
        RawTrack(raw_text="1. 周杰伦 - 晴天", source=ExtractionSource.DESCRIPTION),
        RawTrack(raw_text="00:00 周杰伦 - 晴天", source=ExtractionSource.DESCRIPTION),
        RawTrack(raw_text="2. 陶喆 - 爱很简单", source=ExtractionSource.DESCRIPTION),
    ]
    res = normalize_tracks_rule_based(raw)
    assert len(res) == 2
    assert res[0].artist == "周杰伦"
    assert res[0].title == "晴天"


def test_optional_llm_ollama_and_fallback(monkeypatch):
    def fake_post(url: str, **kwargs):  # noqa: ARG001
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "message": {
                    "content": '[{"artist": "Aimer", "title": "Ref:rain", "timestamp": "04:18"}]'
                }
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    cfg = LLMConfig(provider="ollama", ollama_model="qwen2.5:3b")
    tracks, used = normalize_with_optional_llm(
        raw_blob="04:18 Aimer Ref:rain 超好听",
        fallback_tracks=[],
        llm_cfg=cfg,
        source=ExtractionSource.COMMENTS,
    )
    assert used == "ollama:qwen2.5:3b"
    assert len(tracks) == 1
    assert tracks[0].artist == "Aimer"
    assert tracks[0].title == "Ref:rain"
