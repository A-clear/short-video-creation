"""ベンチマークで使う入力データ。

実運用に近い形と量にすることを優先している（MCP の応答、チャット履歴、
添付ファイル一覧、ストリームチャンク）。生成コストを計測に含めないよう、
すべてモジュールレベルで組み立てるか、明示的に呼ぶファクトリにしている。
"""

from __future__ import annotations

import json
from typing import Any

SIGNED_URL = (
    "https://descript-media.s3.us-east-1.amazonaws.com/exports/proj_8f21/final_cut.mp4"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=EXAMPLEKEYID%2F20260827%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260827T101500Z&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "9f" * 32
)
SHARE_URL = "https://share.example.com/view/proj_8f21"

_PARAGRAPH = (
    "書き出しが完了しました。ジェットカットとフィラー除去を適用し、"
    "テロップは日本語で自動生成しています。尺は 58 秒、アスペクト比は 9:16 です。"
)


def transcript(blocks: int = 12) -> str:
    """署名付き URL が数か所に混ざった、長めのアシスタント本文。"""
    parts = []
    for index in range(blocks):
        parts.append(f"### ステップ {index + 1}\n\n{_PARAGRAPH}\n")
        if index % 3 == 0:
            parts.append(f"ダウンロード: {SIGNED_URL}\n")
        if index % 4 == 0:
            parts.append(
                "```json\n"
                + json.dumps(
                    {
                        "job_id": f"job_{index:04d}",
                        "state": "succeeded",
                        "progress": 100,
                        "media": {"download_url": SIGNED_URL, "duration_sec": 58.4},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n```\n"
            )
    return "\n".join(parts)


TRANSCRIPT = transcript()

MULTIMODAL_CONTENT: list[dict[str, Any]] = [
    {"type": "text", "text": TRANSCRIPT[:1500]},
    {"type": "image_url", "image_url": {"url": "https://example.com/thumb.png"}},
    {"type": "text", "text": f"仕上がりはこちらです: {SIGNED_URL}"},
]


def chat_messages(count: int = 24) -> list[dict[str, Any]]:
    """outlet が受け取る messages 配列（ユーザとアシスタントの往復）。"""
    messages: list[dict[str, Any]] = [{"role": "system", "content": "あなたは動画編集アシスタントです。"}]
    for index in range(count):
        if index % 2 == 0:
            messages.append({"role": "user", "content": f"{index // 2 + 1} 本目の動画をアップロードして編集して"})
        else:
            messages.append({"role": "assistant", "content": transcript(3)})
    return messages


# --- MCP まわり -------------------------------------------------------------

_TOOL_SPECS: list[tuple[str, str]] = [
    ("list_projects", "List all projects in the current Descript drive"),
    ("get_project", "Get a project including its compositions and media"),
    ("import_media", "Import media into a project from a url or direct upload"),
    ("prompt_project_agent", "Run the Underlord agent on a project with a natural language prompt"),
    ("publish_project", "Publish a composition and return the share url"),
    ("wait_for_job", "Wait for an async job and return its state"),
    ("export_timeline", "Export the timeline as fcpxml or edl"),
    ("report_upload_status", "Report the result of a direct upload to the import job"),
    ("create_project", "Create a new empty project"),
    ("delete_project", "Delete a project permanently"),
    ("list_drives", "List the drives the user can access"),
    ("get_transcript", "Get the transcript of a composition"),
    ("update_composition", "Update composition metadata"),
    ("search_media", "Search stock media in the library"),
    ("get_job", "Get the raw job payload"),
    ("cancel_job", "Cancel a running job"),
]

TOOL_SPECS: dict[str, dict[str, Any]] = {
    name: {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "drive_id": {"type": "string"},
                "prompt": {"type": "string"},
                "media_type": {"type": "string"},
                "add_media": {"type": "object"},
                "composition_id": {"type": "string"},
                "format": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    }
    for name, description in _TOOL_SPECS
}

JOB_ENVELOPE = {
    "job_id": "job_2f9c41",
    "state": "succeeded",
    "progress": 100,
    "media": {
        "share_url": SHARE_URL,
        "download_url": SIGNED_URL,
        "composition_id": "comp_77a1",
        "project_url": "https://app.example.com/projects/proj_8f21",
    },
    "warnings": ["captions were generated automatically"],
}

MCP_TEXT_WHOLE = json.dumps(JOB_ENVELOPE, ensure_ascii=False)
MCP_TEXT_FENCED = "Here is the result of the publish job:\n\n```json\n" + MCP_TEXT_WHOLE + "\n```\n"
MCP_TEXT_EMBEDDED = (
    "The publish job finished. Details follow, and note that the download url expires in one hour. "
    + MCP_TEXT_WHOLE
    + " Let me know if you want another export."
)

MCP_CONTENT_BLOCKS: list[dict[str, Any]] = [
    {"type": "text", "text": "publish_project completed."},
    {"type": "resource", "resource": {"uri": "descript://job/2f9c41", "text": MCP_TEXT_WHOLE}},
    {"type": "text", "text": "No further action required."},
]

RAW_PROJECT_LIST = {
    "projects": [
        {
            "id": f"proj_{index:05d}",
            "name": f"ショート動画 {index:03d}",
            "updatedAt": "2026-08-27T10:15:00Z",
            "folderPath": f"/drive/team-{index % 5}/shorts",
            "extra": {"noise": "x" * 40},
        }
        for index in range(200)
    ]
}

MEDIA_ENTRIES = [
    {
        "name": f"収録_{index:03d}.mp4" if index % 3 else "収録.mp4",
        "url": SIGNED_URL if index % 2 else None,
        "path": f"/data/uploads/收録_{index:03d}.mp4",
        "content_type": "video/mp4",
        "file_size": 178_000_000 + index,
    }
    for index in range(120)
]

ARGS_TO_COERCE = {
    "project_id": "proj_8f21",
    "projectId": "proj_8f21",
    "prompt": "ジェットカットで 60 秒に圧縮して",
    "mediaType": "Video",
    "add_media": {"収録.mp4": {"url": SIGNED_URL}},
    "unknown_key": "dropped",
    "timeout seconds": 25,
    "empty": None,
}

MCP_ERRORS = [
    RuntimeError("HTTP 402 payment required: insufficient credits for this drive"),
    RuntimeError("429 Too Many Requests - rate limit reached, retry later"),
    RuntimeError("401 unauthorized: invalid_token, the oauth session expired"),
    RuntimeError("403 Forbidden: you cannot edit this drive"),
    RuntimeError("500 internal error while running the tool"),
]

# --- 添付ファイル / ストリーム ------------------------------------------------

ATTACHED_FILES: list[dict[str, Any]] = []
for _index in range(60):
    if _index % 3 == 0:
        ATTACHED_FILES.append(
            {
                "type": "file",
                "id": f"file_{_index:04d}",
                "name": f"収録_{_index:03d}.mp4",
                "url": f"/api/v1/files/file_{_index:04d}",
                "meta": {"content_type": "video/mp4", "size": 120_000_000},
            }
        )
    elif _index % 3 == 1:
        ATTACHED_FILES.append(
            {
                "file": {
                    "filename": f"資料_{_index:03d}.pdf",
                    "meta": {"content_type": "application/pdf", "size": 2_400_000},
                }
            }
        )
    else:
        ATTACHED_FILES.append(
            {
                "type": "file",
                "id": f"file_{_index:04d}",
                "name": f"素材_{_index:03d}.mov",
                "url": f"/api/v1/files/file_{_index:04d}",
                "meta": {"content_type": "video/quicktime"},
            }
        )


def stream_events(marker_prefix: str, count: int = 200) -> list[dict[str, Any]]:
    """SSE チャンク列。本文・マーカー行・署名 URL・終了チャンクを混ぜる。"""
    events: list[dict[str, Any]] = []
    for index in range(count):
        if index % 17 == 0:
            content = marker_prefix + json.dumps(
                {"label": "書き出し中", "percent": index // 2}, ensure_ascii=False
            )
        elif index % 11 == 0:
            content = f"完成品: {SIGNED_URL}\n"
        else:
            content = _PARAGRAPH[index % 40 : (index % 40) + 24]
        events.append({"choices": [{"index": 0, "delta": {"role": "assistant", "content": content}}]})
    events.append(
        {
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 812, "completion_tokens": 1204},
        }
    )
    return events


CHAT_INTENT_PHRASES = [
    "動画をアップロード",
    "動画を編集",
    "書き出して",
    "プロジェクト一覧",
    "状況を確認",
    "今日の天気を教えて",
    "この動画を 60 秒のショートにまとめて、テロップも付けてください",
    "ぷろじぇくと",
    "publish",
    "  エクスポート。 ",
]
