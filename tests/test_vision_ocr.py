"""Tests for Apple Vision OCR frame cleaning and deduplication."""

from bili2am.extractors.vision_ocr import deduplicate_ocr_frames


def test_deduplicate_ocr_frames_merges_adjacent_and_filters_noise():
    frames = [
        {"file": "frame_0001.jpg", "lines": ["Now Playing", "00:12 / 04:15", "周杰伦 - 晴天"]},
        {"file": "frame_0002.jpg", "lines": ["00:27 / 04:15", "周杰伦 - 晴天"]},
        {"file": "frame_0003.jpg", "lines": ["正在播放", "01:02", "林俊杰 - 江南"]},
        {"file": "frame_0004.jpg", "lines": ["点赞", "投币", "01:17"]},
        {"file": "frame_0005.jpg", "lines": ["陈奕迅 - 富士山下"]},
    ]
    tracks = deduplicate_ocr_frames(frames, interval_sec=15)
    assert len(tracks) == 3
    assert tracks[0].raw_text == "周杰伦 - 晴天"
    assert tracks[0].timestamp == "00:00"
    assert tracks[1].raw_text == "林俊杰 - 江南"
    assert tracks[1].timestamp == "00:30"
    assert tracks[2].raw_text == "陈奕迅 - 富士山下"
    assert tracks[2].timestamp == "01:00"
