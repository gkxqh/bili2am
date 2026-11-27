"""macOS native Apple Vision OCR extractor for on-screen tracklists."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from bili2am.config import OCRConfig
from bili2am.models import ExtractionSource, RawTrack

SWIFT_OCR_SOURCE = r"""
import Foundation
import Vision
import AppKit

struct FrameResult: Codable {
    let file: String
    let lines: [String]
}

func recognizeImage(at path: String, languages: [String]) -> [String] {
    guard let image = NSImage(contentsOfFile: path),
          let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let cgImage = bitmap.cgImage else {
        return []
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    if !languages.isEmpty {
        request.recognitionLanguages = languages
    }
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return []
    }
    guard let observations = request.results else { return [] }
    var lines: [String] = []
    for obs in observations {
        if let top = obs.topCandidates(1).first {
            let trimmed = top.string.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                lines.append(trimmed)
            }
        }
    }
    return lines
}

let args = CommandLine.arguments
if args.count < 2 {
    fputs("Usage: vision_ocr <dir> [lang1,lang2,...]\n", stderr)
    exit(1)
}
let dirPath = args[1]
let langs: [String] = args.count >= 3
    ? args[2].split(separator: ",").map { String($0) }
    : ["zh-Hans", "zh-Hant", "en-US", "ja-JP"]

let fm = FileManager.default
let files = ((try? fm.contentsOfDirectory(atPath: dirPath)) ?? [])
    .filter { $0.hasSuffix(".png") || $0.hasSuffix(".jpg") }
    .sorted()

var output: [FrameResult] = []
for f in files {
    let full = (dirPath as NSString).appendingPathComponent(f)
    let lines = recognizeImage(at: full, languages: langs)
    output.append(FrameResult(file: f, lines: lines))
}

let encoder = JSONEncoder()
if let data = try? encoder.encode(output),
   let str = String(data: data, encoding: .utf8) {
    print(str)
} else {
    print("[]")
}
"""

NOISE_LINE_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*/\s*\d{1,2}:\d{2}(?::\d{2})?)?|"
    r"now\s*playing|next:?|up\s*next|playing:?|bilibili|bv[0-9a-z]+|"
    r"正在播放|下一首|歌单|合集|点赞|投币|收藏|关注)$",
    re.IGNORECASE,
)
FRAME_INDEX_RE = re.compile(r"(\d+)")


def ensure_vision_ocr_binary(cache_dir: Path) -> Path:
    """Compile the Swift Apple Vision helper once and cache the binary."""
    bin_dir = cache_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    src_path = bin_dir / "vision_ocr.swift"
    bin_path = bin_dir / "vision_ocr"

    if bin_path.exists() and src_path.exists() and src_path.read_text("utf-8") == SWIFT_OCR_SOURCE:
        return bin_path

    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise RuntimeError("未找到 swiftc，无法使用 macOS 原生 Apple Vision OCR。")

    src_path.write_text(SWIFT_OCR_SOURCE, encoding="utf-8")
    proc = subprocess.run(
        [swiftc, "-O", str(src_path), "-o", str(bin_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"编译 Apple Vision OCR 工具失败: {proc.stderr.strip()}")
    return bin_path


def download_video_for_ocr(
    url: str,
    work_dir: Path,
    height_limit: int = 720,
    cookie_browser: str | None = None,
) -> Path:
    """Download video stream via yt-dlp for frame extraction."""
    work_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(work_dir.glob("video.*"))
    for p in existing:
        if p.suffix.lower() in {".mp4", ".mkv", ".webm"} and p.stat().st_size > 0:
            return p

    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("未找到 yt-dlp，请先安装 yt-dlp。")

    out_tpl = str(work_dir / "video.%(ext)s")
    fmt = f"bestvideo[height<={height_limit}][ext=mp4]/best[height<={height_limit}]/best"
    cmd = [ytdlp, "-f", fmt, "-o", out_tpl, "--no-playlist", url]
    if cookie_browser:
        cmd.extend(["--cookies-from-browser", cookie_browser])

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 下载视频失败: {proc.stderr.strip()[-400:]}")

    candidates = [
        p for p in work_dir.glob("video.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"}
    ]
    if not candidates:
        raise RuntimeError("yt-dlp 下载完成但未找到视频文件。")
    return candidates[0]


def extract_frames(
    video_path: Path,
    frames_dir: Path,
    interval_sec: int = 15,
    max_frames: int = 240,
    roi: str | None = None,
) -> list[Path]:
    """Extract periodic frames from video with ffmpeg."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(frames_dir.glob("frame_*.jpg"))
    if existing:
        return existing

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请先通过 brew install ffmpeg 安装。")

    vf_parts = [f"fps=1/{max(1, interval_sec)}"]
    if roi:
        vf_parts.append(f"crop={roi}")
    vf = ",".join(vf_parts)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        vf,
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        str(frames_dir / "frame_%04d.jpg"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败: {proc.stderr.strip()}")
    return sorted(frames_dir.glob("frame_*.jpg"))


def _clean_ocr_lines(lines: list[str]) -> str:
    kept: list[str] = []
    for raw in lines:
        s = re.sub(r"\s+", " ", raw).strip()
        if len(s) < 2 or len(s) > 90:
            continue
        if NOISE_LINE_RE.match(s):
            continue
        # Skip lines that are purely digits/punctuation
        if not re.search(r"[A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", s):
            continue
        kept.append(s)
    if not kept:
        return ""
    # Prefer lines that look like "Artist - Title" or combine top 2 short lines
    for line in kept:
        if any(sep in line for sep in (" - ", " — ", " / ", "《")):
            return line
    if len(kept) >= 2 and len(kept[0]) <= 35 and len(kept[1]) <= 40:
        return f"{kept[0]} - {kept[1]}"
    return kept[0]


def deduplicate_ocr_frames(
    frame_results: list[dict[str, object]],
    interval_sec: int = 15,
    similarity_threshold: float = 0.80,
) -> list[RawTrack]:
    """Merge adjacent OCR frame detections into deduplicated RawTrack entries."""
    tracks: list[RawTrack] = []
    prev_norm = ""

    for idx, item in enumerate(frame_results):
        fname = str(item.get("file") or "")
        lines = [str(x) for x in (item.get("lines") or [])]
        candidate = _clean_ocr_lines(lines)
        if not candidate:
            continue

        norm = re.sub(r"[\W_]+", "", candidate.lower())
        if len(norm) < 2:
            continue

        # Consecutive frame similarity check
        if prev_norm:
            sim = SequenceMatcher(None, prev_norm, norm).ratio()
            if sim >= similarity_threshold:
                continue

        # Also avoid duplicating recently seen identical text across brief transitions
        if any(
            SequenceMatcher(None, re.sub(r"[\W_]+", "", t.raw_text.lower()), norm).ratio() >= 0.90
            for t in tracks[-3:]
        ):
            prev_norm = norm
            continue

        frame_num_match = FRAME_INDEX_RE.search(fname)
        frame_idx = int(frame_num_match.group(1)) - 1 if frame_num_match else idx
        start_sec = max(0, frame_idx * interval_sec)
        m, s = divmod(start_sec, 60)
        h, m = divmod(m, 60)
        ts = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        tracks.append(
            RawTrack(
                raw_text=candidate,
                timestamp=ts,
                start_sec=start_sec,
                source=ExtractionSource.OCR,
            )
        )
        prev_norm = norm

    return tracks


def run_ocr_extraction(
    url: str,
    bvid: str,
    cache_dir: Path,
    ocr_cfg: OCRConfig,
    cookie_browser: str | None = None,
) -> tuple[list[RawTrack], str]:
    """Run full video OCR pipeline and return (tracks, raw_ocr_text_blob)."""
    work_dir = cache_dir / bvid
    frames_dir = work_dir / "frames"

    ocr_bin = ensure_vision_ocr_binary(cache_dir)
    video_path = download_video_for_ocr(
        url=url,
        work_dir=work_dir,
        height_limit=ocr_cfg.video_height_limit,
        cookie_browser=cookie_browser,
    )
    extract_frames(
        video_path=video_path,
        frames_dir=frames_dir,
        interval_sec=ocr_cfg.interval_sec,
        max_frames=ocr_cfg.max_frames,
        roi=ocr_cfg.roi,
    )

    langs_arg = ",".join(ocr_cfg.languages)
    proc = subprocess.run(
        [str(ocr_bin), str(frames_dir), langs_arg],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Apple Vision OCR 执行失败: {proc.stderr.strip()}")

    raw_frames = json.loads(proc.stdout or "[]")
    tracks = deduplicate_ocr_frames(raw_frames, interval_sec=ocr_cfg.interval_sec)
    raw_blob_lines = []
    for item in raw_frames:
        lines = [str(x) for x in (item.get("lines") or [])]
        if lines:
            raw_blob_lines.append(f"[{item.get('file')}]: " + " | ".join(lines))
    return tracks, "\n".join(raw_blob_lines)
