"""Domain models for bili2am."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ExtractionSource(str, Enum):
    """Where a tracklist entry was extracted from."""

    PAGES = "pages"
    DESCRIPTION = "description"
    CHAPTERS = "chapters"
    COMMENTS = "comments"
    SUBTITLES = "subtitles"
    OCR = "ocr"
    SHAZAM = "shazam"
    MANUAL = "manual"


class MatchStatus(str, Enum):
    """Apple Music catalog verification status."""

    MATCHED = "matched"      # High-confidence match & streamable
    UNCERTAIN = "uncertain"  # Partial/fuzzy match, needs user confirmation
    MISSING = "missing"      # Not found in target storefront catalog


class CookieAttempt(BaseModel):
    """Result of attempting to read cookies from a single browser."""

    browser: str
    success: bool
    cookie_count: int = 0
    message: str = ""


class CookieResolution(BaseModel):
    """Transparent summary of browser cookie resolution (never stores raw secrets)."""

    enabled: bool = True
    selected_browser: str | None = None
    bilibili_cookie_count: int = 0
    notice: str = ""
    attempts: list[CookieAttempt] = Field(default_factory=list)


class VideoChapter(BaseModel):
    """A video chapter/viewpoint or part item."""

    title: str
    start_sec: int | None = None
    end_sec: int | None = None


class VideoComment(BaseModel):
    """Relevant comment from Bilibili."""

    rpid: int = 0
    mid: int = 0
    author: str = ""
    content: str
    likes: int = 0
    is_pinned: bool = False
    sub_replies: list[VideoComment] = Field(default_factory=list)


class VideoInfo(BaseModel):
    """Fetched Bilibili video metadata."""

    bvid: str
    aid: int | None = None
    cid: int | None = None
    title: str = ""
    owner: str = ""
    duration_sec: int = 0
    desc: str = ""
    url: str = ""
    pages: list[VideoChapter] = Field(default_factory=list)
    chapters: list[VideoChapter] = Field(default_factory=list)
    comments: list[VideoComment] = Field(default_factory=list)
    subtitles_text: list[str] = Field(default_factory=list)


class RawTrack(BaseModel):
    """Raw extracted track before normalization."""

    raw_text: str
    title: str = ""
    artist: str = ""
    timestamp: str | None = None
    start_sec: int | None = None
    source: ExtractionSource = ExtractionSource.DESCRIPTION
    shazam_apple_track_id: str | None = None


class AppleMusicCandidate(BaseModel):
    """A candidate track returned by Apple iTunes Search API."""

    track_id: int
    track_name: str
    artist_name: str
    collection_name: str = ""
    track_view_url: str = ""
    preview_url: str = ""
    artwork_url: str = ""
    is_streamable: bool = True
    confidence: float = 0.0
    country: str = "cn"


class TrackMatchItem(BaseModel):
    """Full lifecycle representation of a track in the confirmation UI."""

    id: str
    raw_text: str
    title: str
    artist: str
    timestamp: str | None = None
    source: ExtractionSource
    status: MatchStatus
    selected: bool = True
    selected_candidate_index: int = 0
    candidates: list[AppleMusicCandidate] = Field(default_factory=list)
    note: str = ""

    @property
    def active_candidate(self) -> AppleMusicCandidate | None:
        if 0 <= self.selected_candidate_index < len(self.candidates):
            return self.candidates[self.selected_candidate_index]
        return None


class PipelineResult(BaseModel):
    """Complete extraction and verification result for a video."""

    cache_version: int = 3
    video: VideoInfo
    cookie_info: CookieResolution
    extraction_method: str
    extraction_notes: list[str] = Field(default_factory=list)
    storefront: str = "cn"
    playlist_name: str = ""
    tracks: list[TrackMatchItem] = Field(default_factory=list)
    llm_used: str = "none"


class ImportRequest(BaseModel):
    """Payload sent from confirmation UI to perform export/import."""

    playlist_name: str
    method: Literal["web_apple_music", "csv", "txt"]
    tracks: list[TrackMatchItem]
    history_id: str | None = None
    retry_track_ids: list[str] | None = None
