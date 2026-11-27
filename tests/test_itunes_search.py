"""Tests for Apple iTunes Search API matching and scoring."""

import json

import httpx

from bili2am.matcher.itunes_search import (
    parse_apple_music_web_html,
    rematch_single_track,
    score_candidate,
    verify_tracks_in_apple_music,
)
from bili2am.models import ExtractionSource, MatchStatus, RawTrack


def test_score_candidate_penalizes_unrequested_live_and_unstreamable():
    studio = score_candidate("周杰伦", "晴天", "周杰伦", "晴天", is_streamable=True)
    live = score_candidate("周杰伦", "晴天", "周杰伦", "晴天 (Live)", is_streamable=True)
    blocked = score_candidate("周杰伦", "晴天", "周杰伦", "晴天", is_streamable=False)
    unrelated_short = score_candidate("", "不存在的测试歌曲xyz999", "柯宇", "存在", is_streamable=True)

    assert studio >= 0.95
    assert live < studio
    assert blocked == 0.0
    assert unrelated_short < 0.42


def test_parse_apple_music_web_html_cn():
    fake_payload = {
        "data": [
            {
                "data": {
                    "sections": [
                        {
                            "id": "top-search-section - top",
                            "items": [
                                {
                                    "title": "小酒窝",
                                    "subtitle": "歌曲\u202f·\u202f林俊杰",
                                    "itemKind": "songs",
                                    "contentDescriptor": {
                                        "kind": "song",
                                        "identifiers": {"storeAdamID": "1073050182"},
                                        "url": "https://music.apple.com/cn/album/x/1?i=1073050182",
                                    },
                                }
                            ],
                        }
                    ]
                }
            }
        ]
    }
    html = f'<html><script type="application/json" id="serialized-server-data">{json.dumps(fake_payload)}</script></html>'
    cands = parse_apple_music_web_html(html, "", "小酒窝", "cn")
    assert len(cands) == 1
    assert cands[0].track_id == 1073050182
    assert cands[0].artist_name == "林俊杰"
    assert cands[0].confidence >= 0.80


def test_verify_tracks_and_rematch():
    def handler(request: httpx.Request) -> httpx.Response:
        if "music.apple.com" in str(request.url):
            return httpx.Response(200, text="<html></html>")
        term = request.url.params.get("term", "")
        if "晴天" in term:
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "results": [
                            {
                                "wrapperType": "track",
                                "trackId": 1001,
                                "trackName": "晴天",
                                "artistName": "周杰伦",
                                "collectionName": "叶惠美",
                                "trackViewUrl": "https://music.apple.com/us/album/1?i=1001",
                                "previewUrl": "https://audio.example/1001.m4a",
                                "isStreamable": True,
                            }
                        ]
                    }
                ).encode(),
            )
        if "限定" in term:
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "results": [
                            {
                                "wrapperType": "track",
                                "trackId": 2002,
                                "trackName": "限定曲",
                                "artistName": "某歌手",
                                "isStreamable": False,
                            }
                        ]
                    }
                ).encode(),
            )
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    raw = [
        RawTrack(raw_text="周杰伦 - 晴天", artist="周杰伦", title="晴天", source=ExtractionSource.PAGES),
        RawTrack(raw_text="某歌手 - 限定曲", artist="某歌手", title="限定曲", source=ExtractionSource.PAGES),
        RawTrack(raw_text="无名氏 - 不存在的歌", artist="无名氏", title="不存在的歌", source=ExtractionSource.PAGES),
    ]
    res = verify_tracks_in_apple_music(raw, country="us", client=client, throttle_sec=0)
    assert res[0].status == MatchStatus.MATCHED
    assert res[0].selected is True
    assert res[1].status == MatchStatus.MISSING
    assert res[1].selected is False
    assert res[2].status == MatchStatus.MISSING

    # User fixes track 2 in UI and rematches
    fixed = rematch_single_track(res[2], artist="周杰伦", title="晴天", country="us", client=client)
    assert fixed.status == MatchStatus.MATCHED
    assert fixed.candidates[0].track_id == 1001
