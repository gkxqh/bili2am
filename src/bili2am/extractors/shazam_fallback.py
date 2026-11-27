"""Audio segmentation + shazamio fallback track recognition."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path

from bili2am.config import ShazamConfig
from bili2am.models import ExtractionSource, RawTrack

SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def is_shazamio_available() -> bool:
    try:
        import shazamio  # noqa: F401
        return True
    except ImportError:
        return False


def download_audio(
    url: str,
    work_dir: Path,
    cookie_browser: str | None = None,
) -> Path:
    """Download audio stream via yt-dlp for Shazam fingerprinting."""
    work_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(work_dir.glob("audio.*")):
        if p.suffix.lower() in {".m4a", ".mp3", ".webm", ".opus", ".aac"} and p.stat().st_size > 0:
            return p

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("未找到 yt-dlp，请先安装 yt-dlp。")

    out_tpl = str(work_dir / "audio.%(ext)s")
    cmd = [ytdlp, "-f", "bestaudio/best", "-o", out_tpl, "--no-playlist", url]
    if cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 下载音频失败: {proc.stderr.strip()[-400:]}")

    for p in sorted(work_dir.glob("audio.*")):
        if p.stat().st_size > 0:
            return p
    raise RuntimeError("yt-dlp 下载完成但未找到音频文件。")


def detect_silence_boundaries(audio_path: Path, total_duration: int) -> list[tuple[int, int]]:
    """Use ffmpeg silencedetect to find natural track boundaries."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(audio_path),
        "-af",
        "silencedetect=noise=-35dB:d=0.6",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    cuts = [0.0]
    for m in SILENCE_END_RE.finditer(proc.stderr):
        t = float(m.group(1))
        if t - cuts[-1] >= 45.0:
            cuts.append(t)
    if total_duration > 0 and total_duration - cuts[-1] >= 30.0:
        cuts.append(float(total_duration))
    if len(cuts) < 3:
        return []
    return [(int(cuts[i]), int(cuts[i + 1])) for i in range(len(cuts) - 1)]


def build_sample_windows(
    duration_sec: int,
    cfg: ShazamConfig,
    known_starts: list[int] | None = None,
    silence_ranges: list[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Compute (clip_start_sec, song_start_sec) windows for Shazam sampling."""
    windows: list[tuple[int, int]] = []
    half = max(4, cfg.sample_duration_sec // 2)

    if known_starts and len(known_starts) >= 2:
        sorted_starts = sorted(set(s for s in known_starts if s >= 0))
        for i, st in enumerate(sorted_starts[: cfg.max_segments]):
            nxt = sorted_starts[i + 1] if i + 1 < len(sorted_starts) else (duration_sec or st + 180)
            mid = st + max(15, (nxt - st) // 2)
            clip_start = max(st, mid - half)
            windows.append((clip_start, st))
        return windows

    if silence_ranges:
        for st, en in silence_ranges[: cfg.max_segments]:
            mid = st + max(15, (en - st) // 2)
            windows.append((max(st, mid - half), st))
        return windows

    total = max(duration_sec, cfg.fallback_interval_sec)
    t = 15
    while t < total and len(windows) < cfg.max_segments:
        windows.append((t, t))
        t += cfg.fallback_interval_sec
    return windows


def _extract_audio_clip(audio_path: Path, clip_path: Path, start_sec: int, duration_sec: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg。")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start_sec),
        "-t",
        str(duration_sec),
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "44100",
        "-f",
        "mp3",
        str(clip_path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


async def _recognize_clips(
    clip_specs: list[tuple[Path, int]],
    concurrency: int = 2,
) -> list[RawTrack]:
    from shazamio import Shazam

    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[tuple[int, dict]] = []

    async with Shazam() as shazam:
        async def _worker(clip_path: Path, song_start: int) -> None:
            async with sem:
                try:
                    out = await shazam.recognize(str(clip_path))
                    results.append((song_start, out))
                except Exception:  # noqa: BLE001
                    return

        await asyncio.gather(*[_worker(p, st) for p, st in clip_specs])

    results.sort(key=lambda x: x[0])
    tracks: list[RawTrack] = []
    seen_keys: list[str] = []

    for song_start, out in results:
        track = out.get("track")
        if not track:
            continue
        title = str(track.get("title") or "").strip()
        artist = str(track.get("subtitle") or "").strip()
        if not title:
            continue
        dedup_key = f"{artist.lower()}::{title.lower()}"
        if seen_keys and seen_keys[-1] == dedup_key:
            continue
        if dedup_key in seen_keys[-4:]:
            continue
        seen_keys.append(dedup_key)

        apple_id = None
        hub = track.get("hub") or {}
        for opt in hub.get("options") or []:
            for act in opt.get("actions") or []:
                if act.get("type") == "applemusicplay" and act.get("id"):
                    apple_id = str(act["id"])
                    break

        m, s = divmod(song_start, 60)
        h, m = divmod(m, 60)
        ts = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        tracks.append(
            RawTrack(
                raw_text=f"{artist} - {title}" if artist else title,
                title=title,
                artist=artist,
                timestamp=ts,
                start_sec=song_start,
                source=ExtractionSource.SHAZAM,
                shazam_apple_track_id=apple_id,
            )
        )
    return tracks


def run_shazam_fallback(
    url: str,
    bvid: str,
    duration_sec: int,
    cache_dir: Path,
    shazam_cfg: ShazamConfig,
    cookie_browser: str | None = None,
    known_starts: list[int] | None = None,
) -> list[RawTrack]:
    """Run audio download + segmentation + shazamio track identification."""
    if not is_shazamio_available():
        raise RuntimeError(
            "未安装 shazamio，无法启用听歌识曲保底。"
            "请运行 `pip install shazamio`（或 `pip install 'bili2am[shazam]'`）后重试。"
        )

    work_dir = cache_dir / bvid
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    audio_path = download_audio(url=url, work_dir=work_dir, cookie_browser=cookie_browser)
    silence_ranges = None if known_starts else detect_silence_boundaries(audio_path, duration_sec)
    windows = build_sample_windows(
        duration_sec=duration_sec,
        cfg=shazam_cfg,
        known_starts=known_starts,
        silence_ranges=silence_ranges,
    )

    clip_specs: list[tuple[Path, int]] = []
    for idx, (clip_start, song_start) in enumerate(windows):
        clip_path = clips_dir / f"clip_{idx:03d}.mp3"
        if not clip_path.exists():
            _extract_audio_clip(audio_path, clip_path, clip_start, shazam_cfg.sample_duration_sec)
        clip_specs.append((clip_path, song_start))

    return asyncio.run(_recognize_clips(clip_specs, concurrency=shazam_cfg.concurrency))
