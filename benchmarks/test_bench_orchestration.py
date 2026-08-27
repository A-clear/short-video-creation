"""ツール解決と編集プロンプト合成のベンチマーク（descript_pipe）。

MCP サーバのツール名は実装差があるため、Pipe は起動のたびに
論理操作 -> 実ツール名の対応を正規表現で解決する（_resolve_by_regex）。
編集プロンプトの合成は Underlord のツールカタログ全体を走査する。
どちらも 1 リクエストにつき必ず 1 回以上通る。
"""

from __future__ import annotations

import _data
from _harness import load_function

pipe = load_function("descript_pipe")

USER_VALVES = pipe.Pipe.UserValves(
    editing_style="full",
    enable_retake_removal=True,
    enable_pan_zoom=True,
    enable_broll=True,
    caption_language="ja",
)


def test_resolve_tools_by_regex(benchmark):
    """8 種の論理操作を 16 個のツール仕様に突き合わせる。"""

    def run():
        used: set = set()
        resolved = {}
        for op in pipe._LOGICAL_OPS_EXT:
            name = pipe.Pipe._resolve_by_regex(op, _data.TOOL_SPECS, used)
            if name:
                used.add(name)
            resolved[op] = name
        return resolved

    resolved = benchmark(run)
    assert resolved["list_projects"] == "list_projects"
    assert resolved["agent_edit"] == "prompt_project_agent"


def test_select_underlord_tools(benchmark):
    names = benchmark(pipe.Pipe._selected_tool_names, USER_VALVES, "full")
    assert names


def test_build_feature_hints(benchmark):
    hints = benchmark(pipe.Pipe._build_feature_hints, USER_VALVES, "full")
    assert len(hints) > 5


def test_fallback_prompt(benchmark):
    hints = pipe.Pipe._build_feature_hints(USER_VALVES, "full")

    def run():
        return pipe.Pipe._fallback_prompt("テンポよく 60 秒にまとめて", hints, "9:16", 60)

    prompt = benchmark(run)
    assert prompt.startswith("Edit this project")


def test_underlord_catalog_text(benchmark):
    """システムプロンプトに載せる AI Tools カタログの組み立て。"""
    catalog = benchmark(pipe._underlord_catalog_text)
    assert "##" in catalog


def test_match_chat_intents(benchmark):
    def run():
        return [pipe._match_chat_intent(phrase) for phrase in _data.CHAT_INTENT_PHRASES]

    intents = benchmark(run)
    assert intents[0] == "import_media"
    assert intents[5] is None


def test_last_user_text(benchmark):
    body = {"messages": _data.chat_messages(40)}
    text = benchmark(pipe.Pipe._last_user_text, body)
    assert text


def test_as_media_list(benchmark):
    raw = [
        {"name": entry["name"], "url": entry["url"], "content_type": entry["content_type"]}
        for entry in _data.MEDIA_ENTRIES
    ]
    media = benchmark(pipe.Pipe._as_media_list, raw)
    assert media


def test_aspect_from_resolution(benchmark):
    def run():
        return [
            pipe._aspect_from_resolution(value)
            for value in ("1080x1920", "1920x1080", "1080×1080", "720 x 1280", "", "ふつう")
        ]

    aspects = benchmark(run)
    assert aspects[0] == "9:16"
