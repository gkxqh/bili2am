"""Tests for Bilibili metadata and tracklist extraction."""

import httpx

from bili2am.extractors.bilibili_api import (
    _fetch_comments,
    extract_bvid,
    extract_text_tracks,
    parse_timestamp_sec,
)
from bili2am.models import ExtractionSource, VideoChapter, VideoComment, VideoInfo


def test_extract_bvid_variants():
    assert extract_bvid("BV1xx411c7mD") == "BV1xx411c7mD"
    assert extract_bvid("https://www.bilibili.com/video/BV1xx411c7mD/?p=2") == "BV1xx411c7mD"


def test_extract_bvid_short_link():
    def handler(request: httpx.Request) -> httpx.Response:
        if "b23.tv" in str(request.url):
            return httpx.Response(
                302,
                headers={"Location": "https://www.bilibili.com/video/BV1ab411c7XY"},
            )
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert extract_bvid("https://b23.tv/abcdef", client=client) == "BV1ab411c7XY"


def test_parse_timestamp_sec():
    assert parse_timestamp_sec("03:15") == 195
    assert parse_timestamp_sec("1:02:03") == 3723
    assert parse_timestamp_sec("bad") is None


def test_extract_text_tracks_from_pages():
    video = VideoInfo(
        bvid="BV1xx411c7mD",
        title="华语精选",
        pages=[
            VideoChapter(title="周杰伦 - 晴天", start_sec=0),
            VideoChapter(title="林俊杰 - 江南", start_sec=260),
        ],
    )
    tracks, notes, _ = extract_text_tracks(video)
    assert len(tracks) == 2
    assert tracks[0].source == ExtractionSource.PAGES
    assert tracks[1].timestamp == "04:20"
    assert "分 P" in notes[0]


def test_extract_text_tracks_prefers_pinned_comment_over_empty_desc():
    video = VideoInfo(
        bvid="BV1xx411c7mD",
        title="深夜歌单",
        desc="喜欢的点赞三连支持一下！",
        comments=[
            VideoComment(author="路人", content="好好听！", likes=99),
            VideoComment(
                author="UP主",
                is_pinned=True,
                likes=500,
                content=(
                    "本期歌单：\n"
                    "00:00 宇多田ヒカル - First Love\n"
                    "04:18 Aimer - Ref:rain\n"
                    "08:05 米津玄師 - Lemon\n"
                ),
            ),
        ],
    )
    tracks, notes, _ = extract_text_tracks(video)
    assert len(tracks) == 3
    assert tracks[0].source == ExtractionSource.COMMENTS
    assert tracks[1].timestamp == "04:18"
    assert "置顶评论" in notes[0]


def test_extract_text_tracks_merges_sub_replies_chain():
    video = VideoInfo(
        bvid="BV1xx411c7mD",
        title="超长歌单接龙",
        comments=[
            VideoComment(
                rpid=10,
                mid=999,
                author="课代表",
                is_pinned=True,
                content="歌单太长分楼发：\n00:00 周杰伦 - 晴天\n04:20 林俊杰 - 江南",
                sub_replies=[
                    VideoComment(
                        rpid=11,
                        mid=999,
                        author="课代表",
                        content="续上楼：\n08:30 许嵩 - 有何不可\n12:15 S.H.E - Ring Ring Ring",
                    ),
                    VideoComment(
                        rpid=12,
                        mid=111,
                        author="路人",
                        content="感谢课代表！",
                    ),
                ],
            )
        ],
    )
    tracks, notes, merged = extract_text_tracks(video)
    assert len(tracks) == 4
    assert "楼中楼" in notes[0]
    assert "有何不可" in merged


def test_fetch_comments_paginates_main_and_sub_replies():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/x/v2/reply/main":
            nxt = params.get("next")
            if not nxt:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "cursor": {"is_end": False, "next": 2},
                            "replies": [
                                {
                                    "rpid": 1,
                                    "mid": 100,
                                    "like": 10,
                                    "rcount": 0,
                                    "member": {"uname": "路人A"},
                                    "content": {"message": "沙发！"},
                                    "replies": [],
                                }
                            ],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {"is_end": True, "next": 2},
                        "replies": [
                            {
                                "rpid": 2,
                                "mid": 200,
                                "like": 50,
                                "rcount": 4,
                                "member": {"uname": "课代表B"},
                                "content": {"message": "歌单放楼下：\n00:00 Aimer - Ref:rain\n04:00 米津玄師 - Lemon"},
                                "replies": [],
                            }
                        ],
                    },
                },
            )
        if path == "/x/v2/reply/reply":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "page": {"count": 1, "size": 20},
                        "replies": [
                            {
                                "rpid": 201,
                                "mid": 200,
                                "like": 20,
                                "member": {"uname": "课代表B"},
                                "content": {"message": "08:00 宇多田ヒカル - First Love\n12:00 I&#39;ll Do It"},
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    comments = _fetch_comments(client, aid=12345, owner_name="UP主")
    assert len(comments) == 2
    assert comments[1].rpid == 2
    assert len(comments[1].sub_replies) == 1
    assert "First Love" in comments[1].sub_replies[0].content
    assert "I'll Do It" in comments[1].sub_replies[0].content
