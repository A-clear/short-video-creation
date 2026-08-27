"""Filter（descript_guard）の inlet / stream / outlet のベンチマーク。

Filter はチャット 1 往復のうち
  inlet   … 添付の判別（拡張子・MIME の総当たり）
  stream  … SSE チャンクごとの本文書き換え
  outlet  … 保存直前に全メッセージを走査
という 3 か所で必ず走る。stream は 1 応答あたり数百回呼ばれるため、
1 チャンクあたりのコストがそのまま体感に効く。
"""

from __future__ import annotations

import asyncio
from typing import Any

import _data
from _harness import Chats, load_function

guard = load_function("descript_guard")

FILTER = guard.Filter()
MARKER_PREFIX = FILTER.valves.progress_marker_prefix
VIDEO_EXTENSIONS = guard._split_csv(FILTER.valves.video_extensions)

# 本文だけを先に作っておき、計測中は dict の組み立てだけを行う
# （毎回まっさらなチャンクを渡すため。書き換え済みを再入力すると経路が変わる）
STREAM_CONTENTS = [
    event["choices"][0]["delta"]["content"] for event in _data.stream_events(MARKER_PREFIX, 200)
]
MESSAGE_CONTENTS = [
    (message["role"], message["content"]) for message in _data.chat_messages(24)
]

CHAT_ID = "chat_bench_0001"
USER = {"id": "user_bench_0001", "name": "bench", "role": "user"}
Chats.seed(
    CHAT_ID,
    USER["id"],
    {
        "title": "動画編集",
        "descript": {
            "v": 1,
            "project_id": "proj_8f21",
            "share_url": _data.SHARE_URL,
            "revision": 3,
            "history": [{"ts": 0, "op": "publish", "result": "success"} for _ in range(40)],
        },
    },
)


async def _drive_stream(events: list[dict[str, Any]]) -> int:
    kept = 0
    for event in events:
        if await FILTER._stream(event, None) is not None:
            kept += 1
    return kept


def test_stream_chunks(benchmark):
    """1 応答分（201 チャンク）をストリームフィルタに通す。"""

    def run():
        events = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": content}}]}
            for content in STREAM_CONTENTS
        ]
        return asyncio.run(_drive_stream(events))

    kept = benchmark(run)
    assert kept > 100


def test_outlet_full_pipeline(benchmark):
    """署名 URL の恒久化 + 生 JSON の折り畳み + history 追記。"""

    def run():
        body = {
            "chat_id": CHAT_ID,
            "messages": [{"role": role, "content": content} for role, content in MESSAGE_CONTENTS],
        }
        return asyncio.run(FILTER._outlet(body, USER))

    result = benchmark(run)
    assert "X-Amz-Signature" not in result["messages"][2]["content"]


def test_stash_media(benchmark):
    """添付 60 件から動画だけを退避する（inlet の入口）。"""

    def run():
        body = {
            "files": _data.ATTACHED_FILES[:30],
            "metadata": {"files": _data.ATTACHED_FILES[15:]},
        }
        FILTER._stash_media(body)
        return body.get("descript_media") or []

    media = benchmark(run)
    assert len(media) == 40


def test_is_video_file(benchmark):
    def run():
        return sum(1 for entry in _data.ATTACHED_FILES if guard._is_video_file(entry, VIDEO_EXTENSIONS))

    assert benchmark(run) == 40


def test_strip_markers(benchmark):
    text = "\n".join(STREAM_CONTENTS[:40])
    kept, payloads = benchmark(guard._strip_markers, text, MARKER_PREFIX)
    assert payloads
    assert MARKER_PREFIX not in kept


def test_marker_label(benchmark):
    payloads = [content[len(MARKER_PREFIX) :] for content in STREAM_CONTENTS if content.startswith(MARKER_PREFIX)]

    def run():
        return [guard._marker_label(payload) for payload in payloads]

    labels = benchmark(run)
    assert labels and labels[0].startswith("書き出し中")
