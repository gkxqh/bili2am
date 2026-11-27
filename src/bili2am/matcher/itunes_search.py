"""Apple Music catalog verification via iTunes Search API + Apple Music web search (for CN)."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Any

import httpx

from bili2am.models import AppleMusicCandidate, MatchStatus, RawTrack, TrackMatchItem

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
APPLE_MUSIC_SEARCH_URL_TPL = "https://music.apple.com/{country}/search"
SERIALIZED_DATA_RE = re.compile(
    r'<script[^>]+id="serialized-server-data"[^>]*>(.*?)</script>',
    re.DOTALL,
)
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

VARIANT_PENALTY_KEYWORDS = ("live", "remix", "karaoke", "instrumental", "cover", "伴奏", "翻唱", "现场")


def _norm_text(s: str) -> str:
    """Normalize unicode punctuation and whitespace for comparison."""
    cleaned = re.sub(r"[\(\[（【][^)\]）】]*[\)\]）】]", " ", s.lower())
    cleaned = re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def score_candidate(
    query_artist: str,
    query_title: str,
    cand_artist: str,
    cand_title: str,
    is_streamable: bool = True,
) -> float:
    """Compute [0, 1] confidence score between parsed track and catalog item."""
    if not is_streamable:
        return 0.0

    qt = _norm_text(query_title)
    ct = _norm_text(cand_title)
    qa = _norm_text(query_artist)
    ca = _norm_text(cand_artist)

    if not qt or not ct:
        return 0.0

    title_sim = SequenceMatcher(None, qt, ct).ratio()
    if qt == ct:
        title_sim = 1.0
    elif qt in ct and len(qt) / max(1, len(ct)) >= 0.45:
        title_sim = max(title_sim, 0.88)
    elif ct in qt and len(ct) / max(1, len(qt)) >= 0.65:
        title_sim = max(title_sim, 0.84)

    if qa:
        artist_sim = SequenceMatcher(None, qa, ca).ratio()
        if qa == ca or (qa in ca and len(qa) / max(1, len(ca)) >= 0.5) or (
            ca in qa and len(ca) / max(1, len(qa)) >= 0.5
        ):
            artist_sim = max(artist_sim, 0.92)
        score = 0.62 * title_sim + 0.38 * artist_sim
    else:
        score = 0.85 * title_sim

    raw_q = f"{query_artist} {query_title}".lower()
    raw_c = cand_title.lower()
    for kw in VARIANT_PENALTY_KEYWORDS:
        if kw in raw_c and kw not in raw_q:
            score -= 0.14
            break

    return max(0.0, min(1.0, round(score, 4)))


def _parse_itunes_item(
    item: dict[str, Any],
    query_artist: str,
    query_title: str,
    country: str,
) -> AppleMusicCandidate | None:
    if item.get("wrapperType") not in (None, "track"):
        return None
    track_id = item.get("trackId")
    track_name = str(item.get("trackName") or "").strip()
    artist_name = str(item.get("artistName") or "").strip()
    if not track_id or not track_name:
        return None

    is_streamable = item.get("isStreamable")
    streamable = True if is_streamable is None else bool(is_streamable)
    conf = score_candidate(
        query_artist=query_artist,
        query_title=query_title,
        cand_artist=artist_name,
        cand_title=track_name,
        is_streamable=streamable,
    )
    return AppleMusicCandidate(
        track_id=int(track_id),
        track_name=track_name,
        artist_name=artist_name,
        collection_name=str(item.get("collectionName") or "").strip(),
        track_view_url=str(item.get("trackViewUrl") or "").strip(),
        preview_url=str(item.get("previewUrl") or "").strip(),
        artwork_url=str(item.get("artworkUrl100") or "").strip(),
        is_streamable=streamable,
        confidence=conf,
        country=country,
    )


def _extract_artist_from_web_item(item: dict[str, Any]) -> str:
    links = item.get("subtitleLinks")
    if isinstance(links, list) and links:
        names = [str(x.get("title") or "").strip() for x in links if isinstance(x, dict)]
        joined = ", ".join(n for n in names if n)
        if joined:
            return joined
    subtitle = str(item.get("subtitle") or "").strip()
    if "·" in subtitle:
        return subtitle.split("·", 1)[-1].strip()
    return subtitle


def parse_apple_music_web_html(
    html: str,
    query_artist: str,
    query_title: str,
    country: str,
) -> list[AppleMusicCandidate]:
    """Parse song candidates from public music.apple.com/{country}/search HTML."""
    m = SERIALIZED_DATA_RE.search(html)
    if not m:
        return []
    try:
        payload = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    sections = (
        (payload.get("data") or [{}])[0]
        .get("data", {})
        .get("sections", [])
    )
    candidates_map: dict[int, AppleMusicCandidate] = {}

    for sec in sections:
        for item in sec.get("items") or []:
            if not isinstance(item, dict):
                continue
            cd = item.get("contentDescriptor") or {}
            if cd.get("kind") != "song" and item.get("itemKind") != "songs":
                continue
            adam_id = str((cd.get("identifiers") or {}).get("storeAdamID") or "").strip()
            if not adam_id.isdigit():
                continue
            track_id = int(adam_id)
            track_name = str(item.get("title") or "").strip()
            if not track_name:
                continue
            artist_name = _extract_artist_from_web_item(item)
            track_url = str(cd.get("url") or "").strip()

            art_tpl = str(((item.get("artwork") or {}).get("dictionary") or {}).get("url") or "")
            artwork_url = (
                art_tpl.replace("{w}", "100").replace("{h}", "100").replace("{f}", "jpg")
                if art_tpl
                else ""
            )
            conf = score_candidate(
                query_artist=query_artist,
                query_title=query_title,
                cand_artist=artist_name,
                cand_title=track_name,
                is_streamable=True,
            )
            cand = AppleMusicCandidate(
                track_id=track_id,
                track_name=track_name,
                artist_name=artist_name,
                collection_name="",
                track_view_url=track_url,
                preview_url="",
                artwork_url=artwork_url,
                is_streamable=True,
                confidence=conf,
                country=country,
            )
            prev = candidates_map.get(track_id)
            if prev is None or cand.confidence > prev.confidence:
                candidates_map[track_id] = cand

    return list(candidates_map.values())


def _query_itunes(
    client: httpx.Client,
    params: dict[str, Any],
    url: str = ITUNES_SEARCH_URL,
) -> list[dict[str, Any]]:
    for attempt in range(3):
        resp = client.get(url, params=params)
        if resp.status_code in (403, 429) and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("results") or [])
    return []


def _query_apple_music_web(
    client: httpx.Client,
    term: str,
    country: str,
    query_artist: str,
    query_title: str,
) -> list[AppleMusicCandidate]:
    url = APPLE_MUSIC_SEARCH_URL_TPL.format(country=country.lower())
    resp = client.get(url, params={"term": term}, headers=WEB_HEADERS, follow_redirects=True)
    resp.raise_for_status()
    return parse_apple_music_web_html(resp.text, query_artist, query_title, country)


def search_track_candidates(
    artist: str,
    title: str,
    country: str = "cn",
    client: httpx.Client | None = None,
    shazam_apple_track_id: str | None = None,
) -> list[AppleMusicCandidate]:
    """Search Apple Music catalog and return top 3 ranked candidates.

    Note: Apple's legacy iTunes Search API returns empty arrays for `country=cn`
    (Mainland China has Apple Music streaming without iTunes Store). For `cn`
    (or when iTunes returns empty), we query the public `music.apple.com/{country}/search`
    catalog page directly.
    """
    owns = client is None
    http = client or httpx.Client(timeout=15.0, follow_redirects=True)
    country_norm = country.strip().lower() or "cn"
    try:
        candidates_map: dict[int, AppleMusicCandidate] = {}

        def _merge(cands: list[AppleMusicCandidate]) -> None:
            for c in cands:
                if not c.is_streamable:
                    continue
                prev = candidates_map.get(c.track_id)
                if prev is None or c.confidence > prev.confidence:
                    candidates_map[c.track_id] = c

        if shazam_apple_track_id and shazam_apple_track_id.isdigit() and country_norm != "cn":
            with contextlib.suppress(Exception):
                rows = _query_itunes(
                    http,
                    {"id": shazam_apple_track_id, "country": country_norm, "entity": "song"},
                    url=ITUNES_LOOKUP_URL,
                )
                parsed = [
                    c for r in rows if (c := _parse_itunes_item(r, artist, title, country_norm)) is not None
                ]
                _merge(parsed)

        primary_term = f"{artist} {title}".strip() if artist else title.strip()
        if primary_term:
            if country_norm == "cn":
                _merge(_query_apple_music_web(http, primary_term, country_norm, artist, title))
            else:
                rows = _query_itunes(
                    http,
                    {
                        "term": primary_term,
                        "media": "music",
                        "entity": "song",
                        "country": country_norm,
                        "limit": 8,
                    },
                )
                parsed = [
                    c for r in rows if (c := _parse_itunes_item(r, artist, title, country_norm)) is not None
                ]
                _merge(parsed)
                if not candidates_map:
                    with contextlib.suppress(Exception):
                        _merge(_query_apple_music_web(http, primary_term, country_norm, artist, title))

        best_so_far = max((c.confidence for c in candidates_map.values()), default=0.0)
        if artist and title and best_so_far < 0.72:
            if country_norm == "cn":
                _merge(_query_apple_music_web(http, title.strip(), country_norm, artist, title))
            else:
                rows = _query_itunes(
                    http,
                    {
                        "term": title.strip(),
                        "media": "music",
                        "entity": "song",
                        "country": country_norm,
                        "limit": 8,
                    },
                )
                parsed = [
                    c for r in rows if (c := _parse_itunes_item(r, artist, title, country_norm)) is not None
                ]
                _merge(parsed)

        ranked = sorted(candidates_map.values(), key=lambda c: c.confidence, reverse=True)
        return ranked[:3]
    finally:
        if owns:
            http.close()


def classify_match(candidates: list[AppleMusicCandidate], country: str) -> tuple[MatchStatus, bool, str]:
    """Classify track match status and default selection state."""
    if not candidates:
        return MatchStatus.MISSING, False, f"Apple Music ({country.upper()}) 曲库未检索到可播放版本"
    top = candidates[0]
    if top.confidence >= 0.72:
        return MatchStatus.MATCHED, True, ""
    if top.confidence >= 0.42:
        return MatchStatus.UNCERTAIN, True, "匹配度中等，建议核对版本"
    return MatchStatus.MISSING, False, f"Apple Music ({country.upper()}) 无高匹配结果"


def verify_tracks_in_apple_music(
    tracks: list[RawTrack],
    country: str = "cn",
    client: httpx.Client | None = None,
    throttle_sec: float = 0.05,
    max_workers: int = 4,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[TrackMatchItem]:
    """Verify every normalized track against Apple Music storefront catalog."""
    def _verify_one(idx: int, tr: RawTrack, shared_client: httpx.Client | None) -> TrackMatchItem:
        artist = tr.artist.strip()
        title = tr.title.strip() or tr.raw_text.strip()
        try:
            cands = search_track_candidates(
                artist=artist,
                title=title,
                country=country,
                client=shared_client,
                shazam_apple_track_id=tr.shazam_apple_track_id,
            )
        except Exception as exc:  # noqa: BLE001
            cands = []
            note = f"查询失败: {exc}"
            status = MatchStatus.MISSING
            selected = False
        else:
            status, selected, note = classify_match(cands, country)

        if throttle_sec > 0:
            time.sleep(throttle_sec)

        return TrackMatchItem(
            id=f"trk_{idx:03d}",
            raw_text=tr.raw_text,
            title=title,
            artist=artist,
            timestamp=tr.timestamp,
            source=tr.source,
            status=status,
            selected=selected,
            selected_candidate_index=0,
            candidates=cands,
            note=note,
        )

    total = len(tracks)
    if total == 0:
        return []

    # When a single mock/test client is passed, execute sequentially on it
    if client is not None:
        out: list[TrackMatchItem] = []
        for idx, tr in enumerate(tracks, start=1):
            res = _verify_one(idx, tr, client)
            out.append(res)
            if progress_cb:
                progress_cb(idx, total, f"{res.artist} - {res.title}" if res.artist else res.title)
        return out

    workers = min(max_workers, total)
    results_by_idx: dict[int, TrackMatchItem] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_verify_one, idx, tr, None): idx
            for idx, tr in enumerate(tracks, start=1)
        }
        for completed, fut in enumerate(as_completed(future_map), start=1):
            idx = future_map[fut]
            item = fut.result()
            results_by_idx[idx] = item
            if progress_cb:
                label = f"{item.artist} - {item.title}" if item.artist else item.title
                progress_cb(completed, total, label)
    return [results_by_idx[i] for i in range(1, total + 1)]


def rematch_single_track(
    item: TrackMatchItem,
    artist: str,
    title: str,
    country: str = "cn",
    client: httpx.Client | None = None,
) -> TrackMatchItem:
    """Re-search a single track after user edits artist/title in the confirmation UI."""
    cands = search_track_candidates(artist=artist, title=title, country=country, client=client)
    status, selected, note = classify_match(cands, country)
    return item.model_copy(
        update={
            "artist": artist.strip(),
            "title": title.strip(),
            "candidates": cands,
            "selected_candidate_index": 0,
            "status": status,
            "selected": selected,
            "note": note,
        }
    )
