"""Tracklist normalization: deterministic rules + optional local/cloud LLM refinement."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from bili2am.config import LLMConfig
from bili2am.models import ExtractionSource, RawTrack

BRACKET_NOISE_RE = re.compile(
    r"【[^】]*(?:hi-?res|无损|4k|60fps|mv|cover|翻唱|live|现场|纯音乐|伴奏|歌词|完整版|推荐)[^】]*】",
    re.IGNORECASE,
)
PAREN_NOISE_RE = re.compile(
    r"[\(\[（](?:hi-?res|flac|320k|4k|60fps|官方|hd|hq|official\s*(?:mv|audio|video))[\)\]）]",
    re.IGNORECASE,
)
GUILLEMET_RE = re.compile(r"《([^》]+)》")
SPLIT_SEPS = (" - ", " — ", " – ", " / ", " | ")

LLM_SYSTEM_PROMPT = """你是一个音乐歌单结构化清洗助手。
请从提供的 B 站视频简介、评论或画面 OCR 文本中，提取出所有的歌曲清单。
严格要求：
1. 过滤掉时间戳、序号、UP主感言、弹幕、播放器UI字样（如 Now Playing）、音质标签（如 Hi-Res/4K）。
2. 每首歌拆分为 artist（歌手，不确定可为空字符串）、title（歌名）、timestamp（如有，如 "03:12"，无则为 null）。
3. 仅返回一个 JSON 数组，不要包含 Markdown 代码块或任何额外解释文字。
示例：[{"artist": "周杰伦", "title": "晴天", "timestamp": "00:00"}]"""


def split_artist_title(raw_text: str) -> tuple[str, str]:
    """Deterministically clean noisy track string and split into (artist, title)."""
    text = raw_text.strip()
    # Remove timestamps first so "03:22" is not misinterpreted as index prefix "03:"
    text = re.sub(r"[\[\(【]?\b\d{1,2}:\d{2}(?::\d{2})?\b[\]\)】]?", " ", text).strip()
    # Remove leading index like "01. " or "1、"
    text = re.sub(r"^\s*(?:P?\d{1,3}[\.、\)\]\s:：\-]+|[\(\[]\d{1,3}[\)\]]\s*)", "", text).strip()
    text = BRACKET_NOISE_RE.sub("", text)
    text = PAREN_NOISE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -—–:：|")

    # Case 1: Explicit separator e.g. "周杰伦 - 晴天"
    for sep in SPLIT_SEPS:
        if sep in text:
            left, right = text.split(sep, 1)
            left = left.strip("《》 ")
            right = right.strip("《》 ")
            if left and right:
                return left, right

    # Compact dash without spaces if both sides have content and not hyphenated English phrase
    if "-" in text and " " not in text.split("-", 1)[0]:
        left, right = text.split("-", 1)
        if len(left.strip()) >= 1 and len(right.strip()) >= 1:
            return left.strip("《》 "), right.strip("《》 ")

    # Case 2: "周杰伦《晴天》" or "《晴天》周杰伦"
    gm = GUILLEMET_RE.search(text)
    if gm:
        title = gm.group(1).strip()
        rest = GUILLEMET_RE.sub("", text).strip(" -—–:：/byBY")
        return rest, title

    return "", text.strip("《》 ")


def normalize_tracks_rule_based(raw_tracks: list[RawTrack]) -> list[RawTrack]:
    """Apply deterministic artist/title cleanup and deduplication."""
    cleaned: list[RawTrack] = []
    seen: set[str] = set()
    for item in raw_tracks:
        artist = item.artist.strip()
        title = item.title.strip()
        if not title:
            artist, title = split_artist_title(item.raw_text)
        if not title or len(title) < 1:
            continue
        key = f"{artist.lower()}::{title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            item.model_copy(
                update={
                    "artist": artist,
                    "title": title,
                }
            )
        )
    return cleaned


def _parse_llm_json(content: str, source: ExtractionSource) -> list[RawTrack]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    arr: list[dict[str, Any]] = json.loads(text[start : end + 1])
    tracks: list[RawTrack] = []
    for row in arr:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        artist = str(row.get("artist") or "").strip()
        ts = row.get("timestamp")
        ts_str = str(ts).strip() if ts else None
        if not title:
            continue
        raw = f"{artist} - {title}" if artist else title
        tracks.append(
            RawTrack(
                raw_text=raw,
                artist=artist,
                title=title,
                timestamp=ts_str,
                source=source,
            )
        )
    return tracks


def check_ollama_status(base_url: str = "http://localhost:11434") -> dict[str, Any]:
    """Check whether Ollama service is reachable and list installed models."""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=3.0)
        resp.raise_for_status()
        models = [m.get("name", "") for m in (resp.json().get("models") or []) if m.get("name")]
        return {"reachable": True, "models": models, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "models": [], "error": str(exc)}


def normalize_with_optional_llm(
    raw_blob: str,
    fallback_tracks: list[RawTrack],
    llm_cfg: LLMConfig,
    source: ExtractionSource,
) -> tuple[list[RawTrack], str]:
    """Use configured local Ollama or OpenAI-compatible LLM if enabled, else rule-based."""
    if llm_cfg.provider == "none" or not raw_blob.strip():
        return normalize_tracks_rule_based(fallback_tracks), "none"

    try:
        if llm_cfg.provider == "ollama":
            resp = httpx.post(
                f"{llm_cfg.ollama_base_url.rstrip('/')}/api/chat",
                json={
                    "model": llm_cfg.ollama_model,
                    "stream": False,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": LLM_SYSTEM_PROMPT},
                        {"role": "user", "content": raw_blob[:12000]},
                    ],
                },
                timeout=90.0,
            )
            resp.raise_for_status()
            content = str((resp.json().get("message") or {}).get("content") or "")
            parsed = _parse_llm_json(content, source=source)
            if parsed:
                return normalize_tracks_rule_based(parsed), f"ollama:{llm_cfg.ollama_model}"

        elif llm_cfg.provider == "openai":
            if not llm_cfg.openai_api_key:
                return normalize_tracks_rule_based(fallback_tracks), "none (missing API key)"
            resp = httpx.post(
                f"{llm_cfg.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {llm_cfg.openai_api_key}"},
                json={
                    "model": llm_cfg.openai_model,
                    "temperature": 0.1,
                    "messages": [
                        {"role": "system", "content": LLM_SYSTEM_PROMPT},
                        {"role": "user", "content": raw_blob[:12000]},
                    ],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            choices = resp.json().get("choices") or []
            content = str((choices[0].get("message") or {}).get("content") or "") if choices else ""
            parsed = _parse_llm_json(content, source=source)
            if parsed:
                return normalize_tracks_rule_based(parsed), f"openai:{llm_cfg.openai_model}"
    except Exception:  # noqa: BLE001
        pass

    return normalize_tracks_rule_based(fallback_tracks), "none (fallback_rules)"
