"""MCP 応答の解釈まわりのベンチマーク（descript_pipe）。

Pipe は 1 操作あたり数回 MCP を呼び、そのたびに戻り値を
_unwrap_mcp -> _json_from_text -> 各 _extract_* で正規化する。
この経路はすべての操作が通る共通のホットパスなので重点的に測る。
"""

from __future__ import annotations

import _data
from _harness import load_function

pipe = load_function("descript_pipe")


def test_json_from_text_whole(benchmark):
    result = benchmark(pipe._json_from_text, _data.MCP_TEXT_WHOLE)
    assert result[1] == "whole"


def test_json_from_text_fenced(benchmark):
    result = benchmark(pipe._json_from_text, _data.MCP_TEXT_FENCED)
    assert result[1] == "fence"


def test_json_from_text_embedded(benchmark):
    """前後に説明文が付くと raw_decode の総当たりになる最悪ケース。"""
    result = benchmark(pipe._json_from_text, _data.MCP_TEXT_EMBEDDED)
    assert result[1] == "embedded"


def test_unwrap_mcp_content_blocks(benchmark):
    result = benchmark(pipe._unwrap_mcp, _data.MCP_CONTENT_BLOCKS)
    assert isinstance(result, (dict, str))


def test_coerce_args(benchmark):
    spec = _data.TOOL_SPECS["prompt_project_agent"]
    arg_map = {"projectId": "project_id", "mediaType": "media_type"}

    def run():
        return pipe._coerce_args(spec, _data.ARGS_TO_COERCE, arg_map)

    args, missing = benchmark(run)
    assert "project_id" in args
    assert missing == []


def test_classify_mcp_error(benchmark):
    def run():
        return [pipe._classify_mcp_error(exc, "publish_project").code for exc in _data.MCP_ERRORS]

    codes = benchmark(run)
    assert codes[0] == "QUOTA_EXCEEDED"
    assert codes[-1] == "JOB_FAILED"


def test_as_project_list(benchmark):
    """list_projects の応答（200 件）をキー揺れごと正規化する。"""
    projects = benchmark(pipe._as_project_list, _data.RAW_PROJECT_LIST)
    assert len(projects) == 200


def test_extract_publish(benchmark):
    published = benchmark(pipe._extract_publish, _data.JOB_ENVELOPE)
    assert published["share_url"] == _data.SHARE_URL


def test_build_add_media(benchmark):
    """import_media の add_media を 120 件分組み立てる（表示名の重複解決込み）。"""
    add_media, uploads = benchmark(pipe._build_add_media, _data.MEDIA_ENTRIES)
    assert len(add_media) == len(_data.MEDIA_ENTRIES)
    assert uploads


def test_unpack_pipe_response(benchmark):
    response = {
        "choices": [
            {
                "message": {
                    "content": '{"ok": true, "op": "publish", "data": {"share_url": "%s"}}' % _data.SHARE_URL
                }
            }
        ]
    }
    envelope = benchmark(pipe._unpack_pipe_response, response)
    assert envelope["ok"] is True
