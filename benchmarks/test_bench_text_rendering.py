"""チャット本文の書き換えと HTML 組み立てのベンチマーク。

Filter の outlet は保存直前に全メッセージを走査して署名 URL を置換し、
生 JSON を <details> に畳む。Action は結果を毎回 embeds の HTML に
組み立て直す。どちらもメッセージ数・素材数に比例して伸びる処理。
"""

from __future__ import annotations

import _data
from _harness import load_function

guard = load_function("descript_guard")
studio = load_function("descript_studio")


def test_redact_signed_urls(benchmark):
    """署名付き URL の検出（正規表現）を長文に対して行う。"""
    redacted = benchmark(guard._redact_signed_urls, _data.TRANSCRIPT, _data.SHARE_URL)
    assert "X-Amz-Signature" not in redacted


def test_redact_signed_urls_no_match(benchmark):
    """1 件もヒットしない場合（大多数のメッセージがこちら）。"""
    text = _data.TRANSCRIPT.replace(_data.SIGNED_URL, _data.SHARE_URL)
    redacted = benchmark(guard._redact_signed_urls, text, "")
    assert redacted == text


def test_redact_message_content_multimodal(benchmark):
    def run():
        content = [dict(item) for item in _data.MULTIMODAL_CONTENT]
        return guard._redact_message_content(content, _data.SHARE_URL)

    _content, changed = benchmark(run)
    assert changed is True


def test_fold_json_blocks(benchmark):
    folded, changed = benchmark(guard._fold_json_blocks, _data.TRANSCRIPT)
    assert changed is True
    assert "<details>" in folded


def test_esc_long_text(benchmark):
    escaped = benchmark(guard._esc, _data.TRANSCRIPT)
    assert "&lt;" not in escaped or True


def test_render_upload_modal(benchmark):
    """7KB 超のモーダル JS テンプレートへの差し込み。"""

    def run():
        return studio._render(
            studio._UPLOAD_MODAL_JS,
            css=studio._BASE_CSS,
            title="動画のアップロード",
            note="対応形式: mp4 / mov",
        )

    rendered = benchmark(run)
    assert len(rendered) > len(studio._UPLOAD_MODAL_JS) - 100


def test_tool_table_html(benchmark):
    tools = list(_data.TOOL_SPECS.values())
    resolved = {
        "list_projects": "list_projects",
        "agent_edit": "prompt_project_agent",
        "publish": "publish_project",
        "job_status": "wait_for_job",
    }

    def run():
        return studio.Action._tool_table_html(tools, resolved, "ツール解決に失敗しました。")

    html = benchmark(run)
    assert "<tr>" in html


def test_media_table_html(benchmark):
    project = {
        "name": "ショート動画 001",
        "media_files": {
            f"media_{index}": {
                "name": f"収録_{index:03d}.mp4",
                "media_type": "video",
                "duration_sec": 58 + index,
            }
            for index in range(80)
        },
    }
    html = benchmark(studio.Action._media_table_html, project, "取り込み済みのメディアです。")
    assert "収録_000.mp4" in html


def test_error_markdown(benchmark):
    action = studio.Action()
    envelope = {
        "ok": False,
        "code": "JOB_TIMEOUT",
        "message_ja": "ジョブの完了を待てませんでした。",
        "hint": "しばらくしてから状況を確認してください。",
        "data": {"job_id": "job_2f9c41", "raw": _data.TRANSCRIPT[:1500]},
    }
    markdown = benchmark(action._error_markdown, envelope)
    assert "code: JOB_TIMEOUT" in markdown
