"""Bilibili public API metadata, chapters, subtitles, and comment tracklist extractor."""

from __future__ import annotations

import html
import re
from http.cookiejar import CookieJar
from typing import Any

import httpx

from bili2am.models import ExtractionSource, RawTrack, VideoChapter, VideoComment, VideoInfo

BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
TIMESTAMP_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
INDEX_PREFIX_RE = re.compile(r"^\s*(?:P?\d{1,3}[\.、\)\]\s:：\-]+|[\(\[]\d{1,3}[\)\]]\s*)")
GENERIC_PART_RE = re.compile(r"^(?:P?\d+|Part\s*\d+|合集|片头|片尾|Intro|Outro)$", re.IGNORECASE)
TRACK_SEPARATOR_RE = re.compile(r"\s+[-—–/|]\s+|《.+?》")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def parse_timestamp_sec(ts: str) -> int | None:
    """Convert HH:MM:SS or MM:SS to total seconds."""
    parts = ts.strip().split(":")
    if not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def extract_bvid(url_or_bvid: str, client: httpx.Client | None = None) -> str:
    """Extract BV id from bare BV string, bilibili URL, or b23.tv short URL."""
    text = url_or_bvid.strip()
    m = BVID_RE.search(text)
    if m:
        return m.group(1)

    if "b23.tv" in text or text.startswith("http"):
        owns_client = client is None
        http = client or httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15.0)
        try:
            resp = http.get(text)
            m2 = BVID_RE.search(str(resp.url)) or BVID_RE.search(resp.text)
            if m2:
                return m2.group(1)
        finally:
            if owns_client:
                http.close()

    raise ValueError(f"无法从输入中识别 B 站 BV 号: {url_or_bvid}")


def _safe_get_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
            last_exc = exc
            if attempt == 2:
                # Fallback once bypassing system/env proxy in case proxy drops domestic TLS
                with httpx.Client(
                    headers=client.headers,
                    cookies=client.cookies,
                    follow_redirects=True,
                    timeout=20.0,
                    trust_env=False,
                ) as direct:
                    resp = direct.get(url, params=params)
                    resp.raise_for_status()
                    return resp.json()
    assert last_exc is not None
    raise last_exc


def fetch_video_info(
    url_or_bvid: str,
    cookies: CookieJar | None = None,
    client: httpx.Client | None = None,
) -> VideoInfo:
    """Fetch video metadata, multi-part pages, chapters, subtitles, and top/pinned comments."""
    owns_client = client is None
    http = client or httpx.Client(
        headers=DEFAULT_HEADERS,
        cookies=cookies,
        follow_redirects=True,
        timeout=20.0,
    )
    try:
        bvid = extract_bvid(url_or_bvid, client=http)
        view_data = _safe_get_json(
            http, "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}
        )
        if view_data.get("code") != 0 or not view_data.get("data"):
            msg = view_data.get("message") or f"code={view_data.get('code')}"
            raise RuntimeError(f"获取 B 站视频信息失败 ({bvid}): {msg}")

        data = view_data["data"]
        aid = data.get("aid")
        cid = data.get("cid")
        pages: list[VideoChapter] = []
        elapsed = 0
        for p in data.get("pages") or []:
            dur = int(p.get("duration") or 0)
            pages.append(
                VideoChapter(
                    title=str(p.get("part") or "").strip(),
                    start_sec=elapsed,
                    end_sec=elapsed + dur if dur else None,
                )
            )
            elapsed += dur

        chapters: list[VideoChapter] = []
        subtitles_text: list[str] = []
        if cid:
            try:
                player_data = _safe_get_json(
                    http,
                    "https://api.bilibili.com/x/player/v2",
                    {"bvid": bvid, "cid": cid},
                )
                pdata = player_data.get("data") or {}
                for vp in pdata.get("view_points") or []:
                    content = str(vp.get("content") or "").strip()
                    if content:
                        chapters.append(
                            VideoChapter(
                                title=content,
                                start_sec=vp.get("from"),
                                end_sec=vp.get("to"),
                            )
                        )
                subs = (pdata.get("subtitle") or {}).get("subtitles") or []
                for sub in subs[:1]:
                    sub_url = sub.get("subtitle_url")
                    if sub_url:
                        if sub_url.startswith("//"):
                            sub_url = f"https:{sub_url}"
                        sub_json = _safe_get_json(http, sub_url)
                        for item in sub_json.get("body") or []:
                            line = str(item.get("content") or "").strip()
                            if line:
                                subtitles_text.append(line)
            except Exception:  # noqa: BLE001
                pass

        owner_name = str((data.get("owner") or {}).get("name") or "").strip()
        comments: list[VideoComment] = []
        if aid:
            comments = _fetch_comments(http, int(aid), owner_name=owner_name)

        return VideoInfo(
            bvid=bvid,
            aid=aid,
            cid=cid,
            title=str(data.get("title") or "").strip(),
            owner=owner_name,
            duration_sec=int(data.get("duration") or 0),
            desc=str(data.get("desc") or "").strip(),
            url=f"https://www.bilibili.com/video/{bvid}",
            pages=pages,
            chapters=chapters,
            comments=comments,
            subtitles_text=subtitles_text,
        )
    finally:
        if owns_client:
            http.close()


def _parse_comment_item(raw: dict[str, Any], is_pinned: bool = False) -> VideoComment | None:
    msg = html.unescape(str((raw.get("content") or {}).get("message") or "")).strip()
    if not msg:
        return None
    member = raw.get("member") or {}
    sub_items: list[VideoComment] = []
    for sub in raw.get("replies") or []:
        if isinstance(sub, dict):
            parsed_sub = _parse_comment_item(sub, is_pinned=False)
            if parsed_sub is not None:
                sub_items.append(parsed_sub)
    return VideoComment(
        rpid=int(raw.get("rpid") or 0),
        mid=int(raw.get("mid") or 0),
        author=str(member.get("uname") or "").strip(),
        content=msg,
        likes=int(raw.get("like") or 0),
        is_pinned=is_pinned,
        sub_replies=sub_items,
    )


def _should_fetch_sub_replies(comment: VideoComment, owner_name: str, rcount: int) -> bool:
    if rcount <= len(comment.sub_replies) or comment.rpid <= 0:
        return False
    if comment.is_pinned or (owner_name and comment.author == owner_name):
        return True
    if len(parse_track_lines(comment.content, ExtractionSource.COMMENTS)) >= 2:
        return True
    hints = ("歌单", "曲目", "楼下", "楼中楼", "续", "时间轴", "bgm")
    low = comment.content.lower()
    return any(h in low for h in hints)


def _fetch_sub_replies(
    http: httpx.Client,
    aid: int,
    root_rpid: int,
    max_pages: int = 5,
) -> list[VideoComment]:
    collected: list[VideoComment] = []
    seen_ids: set[int] = set()
    for pn in range(1, max_pages + 1):
        try:
            payload = _safe_get_json(
                http,
                "https://api.bilibili.com/x/v2/reply/reply",
                {"type": 1, "oid": aid, "root": root_rpid, "ps": 20, "pn": pn},
            )
            if payload.get("code") != 0:
                break
            rdata = payload.get("data") or {}
            rows = rdata.get("replies") or []
            if not rows:
                break
            for raw in rows:
                parsed = _parse_comment_item(raw, is_pinned=False)
                if parsed is not None and (parsed.rpid == 0 or parsed.rpid not in seen_ids):
                    if parsed.rpid:
                        seen_ids.add(parsed.rpid)
                    collected.append(parsed)
            page_info = rdata.get("page") or {}
            total = int(page_info.get("count") or 0)
            if total and len(collected) >= total:
                break
        except Exception:  # noqa: BLE001
            break
    return collected


def _fetch_comments(
    http: httpx.Client,
    aid: int,
    owner_name: str = "",
    max_main_pages: int = 4,
) -> list[VideoComment]:
    comments: list[VideoComment] = []
    seen_rpids: set[int] = set()

    def _append_comment(raw: dict[str, Any], is_pinned: bool) -> None:
        parsed = _parse_comment_item(raw, is_pinned=is_pinned)
        if parsed is None:
            return
        if parsed.rpid and parsed.rpid in seen_rpids:
            return
        if parsed.rpid:
            seen_rpids.add(parsed.rpid)
        rcount = int(raw.get("rcount") or raw.get("count") or 0)
        if _should_fetch_sub_replies(parsed, owner_name=owner_name, rcount=rcount):
            full_subs = _fetch_sub_replies(http, aid, parsed.rpid)
            if len(full_subs) > len(parsed.sub_replies):
                parsed = parsed.model_copy(update={"sub_replies": full_subs})
        comments.append(parsed)

    # 1. Try /x/v2/reply/main with cursor pagination
    next_cursor: int | None = None
    main_ok = False
    for page_idx in range(max_main_pages):
        params: dict[str, Any] = {"type": 1, "oid": aid, "mode": 3, "ps": 20}
        if next_cursor is not None:
            params["next"] = next_cursor
        try:
            payload = _safe_get_json(http, "https://api.bilibili.com/x/v2/reply/main", params)
            if payload.get("code") != 0:
                break
            main_ok = True
            rdata = payload.get("data") or {}
            if page_idx == 0:
                upper_top = (rdata.get("top") or {}).get("upper") or rdata.get("upper", {}).get("top")
                if isinstance(upper_top, dict):
                    _append_comment(upper_top, is_pinned=True)
            replies = rdata.get("replies") or []
            for item in replies:
                if isinstance(item, dict):
                    _append_comment(item, is_pinned=False)

            # Stop early if we already found a solid tracklist on earlier pages
            if any(_score_comment_tracklist(c)[0] >= 12 for c in comments):
                break
            cursor = rdata.get("cursor") or {}
            if cursor.get("is_end") or not replies:
                break
            next_val = cursor.get("next")
            if next_val is None or next_val == next_cursor:
                break
            next_cursor = int(next_val)
        except Exception:  # noqa: BLE001
            break

    if main_ok and comments:
        return comments

    # 2. Fallback to legacy /x/v2/reply?sort=2 with pn pagination
    for pn in range(1, max_main_pages + 1):
        try:
            payload = _safe_get_json(
                http,
                "https://api.bilibili.com/x/v2/reply",
                {"type": 1, "oid": aid, "sort": 2, "ps": 20, "pn": pn},
            )
            if payload.get("code") != 0:
                break
            rdata = payload.get("data") or {}
            if pn == 1:
                upper_top = (rdata.get("top") or {}).get("upper") or rdata.get("upper", {}).get("top")
                if isinstance(upper_top, dict):
                    _append_comment(upper_top, is_pinned=True)
            replies = rdata.get("replies") or []
            if not replies:
                break
            for item in replies:
                if isinstance(item, dict):
                    _append_comment(item, is_pinned=False)
            if any(_score_comment_tracklist(c)[0] >= 12 for c in comments):
                break
        except Exception:  # noqa: BLE001
            break

    return comments


def _looks_like_track_line(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 2 or len(stripped) > 140:
        return False
    # Filter obvious non-track chatter
    lowered = stripped.lower()
    noise_prefixes = (
        "http://",
        "https://",
        "bv",
        "av",
        "简介",
        "歌单",
        "曲目",
        "合集",
        "收藏",
        "三连",
        "点赞",
        "投币",
        "关注",
        "商务",
        "合作",
        "版权",
        "侵删",
        "bgm",
        "封面",
    )
    if any(lowered.startswith(p) and ":" in lowered[:10] for p in noise_prefixes):
        return False
    if any(k in stripped for k in ("一键三连", "感谢收听", "欢迎订阅", "点赞投币", "持续更新")):
        return False
    has_ts = bool(TIMESTAMP_RE.search(stripped))
    has_idx = bool(INDEX_PREFIX_RE.match(stripped))
    has_sep = bool(TRACK_SEPARATOR_RE.search(stripped))
    return has_ts or has_idx or has_sep


def parse_track_lines(text: str, source: ExtractionSource) -> list[RawTrack]:
    """Parse candidate track rows from multi-line description or comment text."""
    tracks: list[RawTrack] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not _looks_like_track_line(line):
            continue
        ts_match = TIMESTAMP_RE.search(line)
        ts_str = ts_match.group(1) if ts_match else None
        start_sec = parse_timestamp_sec(ts_str) if ts_str else None

        cleaned = line
        if ts_match:
            # Remove surrounding brackets around timestamp like [03:12] or (03:12)
            cleaned = re.sub(
                rf"[\[\(【]?\s*{re.escape(ts_str)}\s*[\]\)】]?",
                " ",
                cleaned,
                count=1,
            ).strip()
        cleaned = INDEX_PREFIX_RE.sub("", cleaned).strip(" -—–:：|")
        if not cleaned or len(cleaned) < 2:
            continue
        # Skip header lines like "歌单：" or "Timecode:"
        if cleaned.rstrip(":：") in {"歌单", "曲目", "歌单如下", "目录", "Tracklist", "Playlist"}:
            continue
        tracks.append(
            RawTrack(
                raw_text=line,
                timestamp=ts_str,
                start_sec=start_sec,
                source=source,
            )
        )
    return tracks


def _collect_comment_with_sub_replies(comment: VideoComment) -> tuple[list[RawTrack], str]:
    """Combine tracks from a root comment and any tracklist-bearing sub-replies (楼中楼)."""
    tracks = parse_track_lines(comment.content, ExtractionSource.COMMENTS)
    text_parts = [comment.content]
    for sub in comment.sub_replies:
        sub_tracks = parse_track_lines(sub.content, ExtractionSource.COMMENTS)
        same_author = bool(comment.mid and sub.mid and comment.mid == sub.mid) or (
            bool(comment.author and sub.author and comment.author == sub.author)
        )
        # Include sub-reply if it comes from the same author or clearly contains tracklist lines
        if sub_tracks and (same_author or len(sub_tracks) >= 2 or any(t.timestamp for t in sub_tracks)):
            tracks.extend(sub_tracks)
            text_parts.append(sub.content)
    return tracks, "\n".join(text_parts)


def _score_comment_tracklist(comment: VideoComment) -> tuple[int, list[RawTrack], str]:
    parsed, merged_text = _collect_comment_with_sub_replies(comment)
    if len(parsed) < 2:
        return 0, [], ""
    ts_bonus = sum(1 for t in parsed if t.timestamp)
    pin_bonus = 20 if comment.is_pinned else 0
    return len(parsed) * 3 + ts_bonus * 2 + pin_bonus, parsed, merged_text


def extract_text_tracks(video: VideoInfo) -> tuple[list[RawTrack], list[str], str]:
    """Extract tracklist candidates from Bilibili pages, chapters, description, and comments.

    Returns (raw_tracks, notes, combined_raw_text_for_optional_llm).
    """
    notes: list[str] = []

    # 1. Multi-part pages (分P合集)
    meaningful_pages = [
        p for p in video.pages if p.title and not GENERIC_PART_RE.match(p.title.strip())
    ]
    if len(meaningful_pages) >= 2:
        tracks = [
            RawTrack(
                raw_text=p.title,
                start_sec=p.start_sec,
                timestamp=_fmt_sec(p.start_sec) if p.start_sec is not None else None,
                source=ExtractionSource.PAGES,
            )
            for p in meaningful_pages
        ]
        notes.append(f"从分 P 选集列表中提取到 {len(tracks)} 条曲目。")
        raw_blob = "\n".join(t.raw_text for t in tracks)
        return tracks, notes, raw_blob

    # 2. Chapters / Viewpoints
    if len(video.chapters) >= 2:
        tracks = [
            RawTrack(
                raw_text=c.title,
                start_sec=c.start_sec,
                timestamp=_fmt_sec(c.start_sec) if c.start_sec is not None else None,
                source=ExtractionSource.CHAPTERS,
            )
            for c in video.chapters
            if not GENERIC_PART_RE.match(c.title.strip())
        ]
        if len(tracks) >= 2:
            notes.append(f"从视频分段章节提取到 {len(tracks)} 条曲目。")
            raw_blob = "\n".join(t.raw_text for t in tracks)
            return tracks, notes, raw_blob

    # 3. Video description & Comments (choose the richer source)
    desc_tracks = parse_track_lines(video.desc, ExtractionSource.DESCRIPTION) if video.desc else []
    best_comment: VideoComment | None = None
    best_comment_score = 0
    best_comment_tracks: list[RawTrack] = []
    best_comment_text = ""
    for c in video.comments:
        score, parsed, merged_text = _score_comment_tracklist(c)
        if score > best_comment_score:
            best_comment_score = score
            best_comment = c
            best_comment_tracks = parsed
            best_comment_text = merged_text

    if len(desc_tracks) >= 2 and len(desc_tracks) >= len(best_comment_tracks):
        notes.append(f"从视频简介提取到 {len(desc_tracks)} 条候选曲目。")
        return desc_tracks, notes, video.desc

    if len(best_comment_tracks) >= 2 and best_comment is not None:
        tag = "置顶评论" if best_comment.is_pinned else "评论区"
        sub_note = "（含楼中楼接龙）" if best_comment.sub_replies else ""
        notes.append(f"从{tag}{sub_note}提取到 {len(best_comment_tracks)} 条候选曲目。")
        return best_comment_tracks, notes, best_comment_text

    if desc_tracks:
        notes.append(f"简介仅提取到 {len(desc_tracks)} 条候选，数量较少。")
        return desc_tracks, notes, video.desc

    if best_comment_tracks and best_comment is not None:
        notes.append(f"评论区仅提取到 {len(best_comment_tracks)} 条候选，数量较少。")
        return best_comment_tracks, notes, best_comment_text

    raw_blob_parts = [p for p in [video.desc] + [c.content for c in video.comments[:3]] if p]
    notes.append("未能从分P/章节/简介/热评中直接提取到歌单文本。")
    return [], notes, "\n---\n".join(raw_blob_parts)


def _fmt_sec(sec: int) -> str:
    h, rem = divmod(max(0, sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
