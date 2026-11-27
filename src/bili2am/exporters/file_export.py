"""Export confirmed tracks to standard TXT/CSV for playlist migration tools."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from bili2am.models import MatchStatus, TrackMatchItem


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return cleaned[:80] or "bili2am_playlist"


def filter_confirmed_tracks(tracks: list[TrackMatchItem]) -> list[TrackMatchItem]:
    """Keep only user-selected tracks that exist in Apple Music catalog.

    Missing tracks are intentionally excluded from export files (only displayed in UI/summary).
    """
    confirmed: list[TrackMatchItem] = []
    for item in tracks:
        if not item.selected:
            continue
        if item.status == MatchStatus.MISSING or not item.candidates:
            continue
        if item.active_candidate is None:
            continue
        confirmed.append(item)
    return confirmed


def format_tracks_txt(tracks: list[TrackMatchItem]) -> str:
    """Format confirmed tracks as plain 'Artist - Title' lines."""
    lines: list[str] = []
    for item in filter_confirmed_tracks(tracks):
        cand = item.active_candidate
        assert cand is not None
        lines.append(f"{cand.artist_name} - {cand.track_name}")
    return "\n".join(lines) + ("\n" if lines else "")


def format_tracks_csv(tracks: list[TrackMatchItem]) -> str:
    """Format confirmed tracks as UTF-8 CSV compatible with TuneMyMusic / Soundiiz."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Track Name", "Artist Name", "Album Name", "Apple Music Track ID", "Apple Music URL"])
    for item in filter_confirmed_tracks(tracks):
        cand = item.active_candidate
        assert cand is not None
        writer.writerow(
            [
                cand.track_name,
                cand.artist_name,
                cand.collection_name,
                str(cand.track_id),
                cand.track_view_url,
            ]
        )
    return buf.getvalue()


def export_tracks_to_txt(tracks: list[TrackMatchItem], out_dir: Path, playlist_name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sanitize_filename(playlist_name)}.txt"
    path.write_text(format_tracks_txt(tracks), encoding="utf-8")
    return path


def export_tracks_to_csv(tracks: list[TrackMatchItem], out_dir: Path, playlist_name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sanitize_filename(playlist_name)}.csv"
    # UTF-8 with BOM helps Excel/Chinese apps open cleanly
    path.write_text("\ufeff" + format_tracks_csv(tracks), encoding="utf-8")
    return path
