"""
title: Descript Orchestrator
author: A-clear
author_url: https://github.com/A-clear/short-video-creation
version: 0.1.0
license: MIT
description: Descript MCP を介した取込 / Underlord 編集 / 書き出しのオーケストレーション
required_open_webui_version: 0.11.0
requirements:
"""

# ---------------------------------------------------------------------------
# functions/descript_pipe.py — オーケストレーション層（Pipe）
#
# 契約の正本: docs/DetailedDesign/functions_contract.md
# 一次仕様:   docs/DetailedDesign/movie_flow.mmd
#
# 1 Function = 自己完結した 1 ファイル。相対 import は解決されないため、
# 契約書 §5 の共通ヘルパは本ファイルにインライン展開している（改変禁止）。
# ---------------------------------------------------------------------------

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

import httpx
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.models.prompts import Prompts
from open_webui.models.users import UserModel
from open_webui.storage.provider import Storage
from open_webui.utils.chat import generate_chat_completion
from open_webui.utils.middleware import connect_mcp_server
from pydantic import BaseModel, Field

# ===========================================================================
# 契約書 §5.1 定数・例外
# ===========================================================================

_STATE_KEY = "descript"
_STATE_VERSION = 1
_HISTORY_MAX = 50
_LOGICAL_OPS = ("list_projects", "get_project", "import_media",
                "agent_edit", "publish", "job_status")

# 契約書 §2.1 の 6 つに加え、実測で存在が確定した 2 ツールを Pipe だけが解決する。
#   export_timeline      — 7 番目の論理操作（契約書 §2.0.1 ④）
#   report_upload_status — 直接アップロード失敗時の通知（契約書 §2.0.1 ①）
# _LOGICAL_OPS 自体は 3 ファイル共通の定義なので変更しない。
_LOGICAL_OPS_EXT = _LOGICAL_OPS + ("export_timeline", "report_upload_status")


class DescriptError(Exception):
    """Pipe 内部で送出し、封筒に変換して返すためのエラー。"""

    def __init__(self, code: str, message_ja: str, hint: str = "", data: Any = None):
        super().__init__(f"{code}: {message_ja}")
        self.code = code
        self.message_ja = message_ja
        self.hint = hint
        self.data = data

    def envelope(self, op: str = "") -> dict:
        return {
            "ok": False,
            "op": op,
            "code": self.code,
            "message_ja": self.message_ja,
            "hint": self.hint,
            "data": self.data,
        }


# ===========================================================================
# 契約書 §5.10 ロギング
# ===========================================================================

_LOG_VALUE_MAX = 300

# Function は function_<function_id> という名前のモジュールとして exec される
# （backend/open_webui/utils/plugin.py:276）ので、__name__ がそのまま識別子になる。
_LOGGER = logging.getLogger(__name__)


def _log(level: int, event: str, **fields: Any) -> None:
    """1 行 1 イベントの構造化ログを出す。

    Open WebUI は stdlib logging を GLOBAL_LOG_LEVEL で一括設定する
    （backend/open_webui/env.py:107-118）。v0.11.0 の SRC_LOG_LEVELS は
    空の互換用変数なので参照してはいけない（KeyError になる）。

    Uvicorn Workers で複数プロセスに分かれるため、呼び出し側は chat_id と
    op を必ず渡すこと。値は署名 URL を伏せ、長すぎるものは切り詰める。
    ⚠️ Valve の値・アクセストークン・ファイル本体は載せない（§11）。
    """
    if not _LOGGER.isEnabledFor(level):
        return
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = _redact_signed_urls(str(value)).replace("\n", " ")
        if len(text) > _LOG_VALUE_MAX:
            text = text[:_LOG_VALUE_MAX] + "…"
        parts.append(f"{key}={text}")
    _LOGGER.log(level, "descript %s%s", event,
                (" " + " ".join(parts)) if parts else "")


def _log_debug(event: str, **fields: Any) -> None:
    _log(logging.DEBUG, event, **fields)


def _log_info(event: str, **fields: Any) -> None:
    _log(logging.INFO, event, **fields)


def _log_warn(event: str, **fields: Any) -> None:
    _log(logging.WARNING, event, **fields)


def _log_error(event: str, **fields: Any) -> None:
    _log(logging.ERROR, event, **fields)


def _apply_log_level(level: Any) -> None:
    """Valve のレベルをこの Function のロガーにだけ適用する。

    GLOBAL_LOG_LEVEL を上げると Open WebUI 全体が饒舌になるため、
    Function 単位で切り替えられるようにする。未知の値は無視する。
    getLevelNamesMapping は Python 3.11+（本プロジェクトの下限）。
    """
    name = str(level or "").strip().upper()
    if name in logging.getLevelNamesMapping():
        _LOGGER.setLevel(name)


def _ms(started: float) -> int:
    """time.monotonic() の起点からの経過ミリ秒。"""
    return int((time.monotonic() - started) * 1000)


# ===========================================================================
# 契約書 §5.2 小物
# ===========================================================================


def _pick(obj: Any, *keys: str, default: Any = None) -> Any:
    """dict から最初に見つかった非 None の値を返す。MCP の戻り値のキー揺れ吸収用。"""
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return default


def _norm_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _esc(text: Any) -> str:
    """HTML テンプレートに差し込む前のエスケープ。"""
    return (
        str("" if text is None else text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render(tpl: str, **kwargs: Any) -> str:
    """{{key}} を置換するだけの軽量テンプレート。

    str.format() を使わないのは、テンプレート中の CSS の { } を
    すべてエスケープする必要が生じて保守不能になるため。
    """
    out = tpl
    for key, value in kwargs.items():
        out = out.replace("{{" + key + "}}",
                          "" if value is None else str(value))
    return out


def _as_user_model(user: Any) -> UserModel:
    """Action/Pipe の __user__（dict）を UserModel に変換する。

    connect_mcp_server / generate_chat_completion は UserModel を要求する。
    'valves' は UserValves の pydantic インスタンスなので必ず除去する。
    """
    if isinstance(user, UserModel):
        return user
    return UserModel(**{k: v for k, v in dict(user or {}).items() if k != "valves"})


# ===========================================================================
# 契約書 §5.3 状態の読み書き
# ===========================================================================


async def _load_state(chat_id: Optional[str]) -> dict:
    if not chat_id:
        return {}
    try:
        chat = await Chats.get_chat_by_id(chat_id)
    except Exception:
        return {}
    if chat is None:
        return {}
    state = (chat.chat or {}).get(_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


async def _save_state(chat_id: Optional[str], patch: dict) -> dict:
    """chat.chat['descript'] にパッチをマージして保存し、マージ後の state を返す。

    update_chat_by_id は blob に 'title' が無いとチャットタイトルを
    'New Chat' にリセットする（models/chats.py:608）ため、必ず補う。
    """
    if not chat_id:
        return dict(patch or {})
    try:
        chat = await Chats.get_chat_by_id(chat_id)
    except Exception:
        return dict(patch or {})
    if chat is None:
        return dict(patch or {})

    blob = dict(chat.chat or {})
    state = dict(blob.get(_STATE_KEY) or {})
    state.update(patch or {})
    state["v"] = _STATE_VERSION
    if isinstance(state.get("history"), list) and len(state["history"]) > _HISTORY_MAX:
        state["history"] = state["history"][-_HISTORY_MAX:]

    blob[_STATE_KEY] = state
    if "title" not in blob:
        blob["title"] = chat.title or "New Chat"

    try:
        await Chats.update_chat_by_id(chat_id, blob, touch=False)
    except Exception:
        pass
    return state


async def _append_history(chat_id: Optional[str], entry: dict) -> None:
    state = await _load_state(chat_id)
    history = list(state.get("history") or [])
    history.append({"ts": int(time.time()), **entry})
    await _save_state(chat_id, {"history": history})


# ===========================================================================
# 契約書 §5.4 引数整形
# ===========================================================================


def _coerce_args(spec: dict, args: dict, arg_map: Optional[dict] = None) -> tuple[dict, list]:
    """論理引数名を実引数名に写像し、スキーマに無いキーを落とす。

    戻り値は (整形済み引数, 不足している required キー)。
    spec['parameters'] が空（スキーマ不明）の場合は素通しする。
    """
    arg_map = arg_map or {}
    schema = (spec or {}).get("parameters") or {}
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])

    normalized = {_norm_key(k): k for k in props}
    out = {}
    for key, value in (args or {}).items():
        if value is None:
            continue
        name = arg_map.get(key, key)
        if props:
            if name not in props:
                match = normalized.get(_norm_key(name))
                if match is None:
                    continue  # スキーマに無い引数は送らない
                name = match
        out[name] = value

    missing = [r for r in required if r not in out]
    return out, missing


# ===========================================================================
# 契約書 §5.5 MCP 呼び出し
# ===========================================================================


def _json_from_text(text: str) -> tuple:
    """テキストから JSON 値を取り出す。戻り値は (値, 取り出し方)。

    取り出せなければ (None, "")。取り出し方は "whole" / "fence" / "embedded"。
    MCP サーバは JSON の前後に人間向けの説明文を付けることがあるため、
    全体 parse だけに頼らない（契約書 §5.5 冒頭の実測）。
    """
    body = (text or "").strip()
    if not body:
        return None, ""
    try:
        return json.loads(body), "whole"
    except Exception:
        pass

    # ```json … ``` で囲んで返す実装
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fence:
        try:
            return json.loads(fence.group(1).strip()), "fence"
        except Exception:
            pass

    # 前後に説明文が付いている実装。raw_decode は「値の直後で終わらない」ことを
    # 許すので、最初の { または [ から均衡する位置までを 1 値として取り出せる。
    decoder = json.JSONDecoder()
    for index, char in enumerate(body):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(body, index)
        except Exception:
            continue
        return value, "embedded"
    return None, ""


def _unwrap_mcp(raw: Any) -> Any:
    """MCPClient.call_tool の戻り値を正規化する。

    call_tool は CallToolResult.content（コンテンツブロックの配列）を返す
    （utils/mcp/client.py:117-123）。structuredContent は捨てられるため、
    text ブロックから JSON を取り出すのが唯一の経路になる。
    """
    if raw is None or isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        # 文字列で返す実装もありうる。JSON なら開いておく。
        value, _mode = _json_from_text(raw)
        return raw if value is None else value
    if not isinstance(raw, list):
        return raw

    texts = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource.get("text"), str):
                texts.append(resource["text"])

    if not texts:
        return raw
    joined = "\n".join(texts).strip()
    value, mode = _json_from_text(joined)
    if value is None:
        # JSON がまったく無い＝本当に人間向けの文章だけ。文字列として返す。
        _log_warn("mcp.unwrap_text_only", length=len(
            joined), head=joined[:200])
        return joined
    if mode != "whole":
        # サーバが JSON に説明文を混ぜている。動作はするが取りこぼしの温床なので残す。
        _log_warn("mcp.unwrap_mixed", mode=mode,
                  length=len(joined), head=joined[:200])
    return value


def _classify_mcp_error(exc: Exception, tool_name: str) -> DescriptError:
    text = str(exc).lower()
    if any(t in text for t in ("402", "credit", "quota", "insufficient", "payment")):
        return DescriptError(
            "QUOTA_EXCEEDED",
            "Descript のクレジットまたはメディア時間が不足しています。",
            "Descript の残高をご確認ください。",
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    if any(t in text for t in ("429", "rate limit", "too many")):
        return DescriptError(
            "RATE_LIMITED",
            "Descript のレート制限に達しました。",
            "しばらく待ってから再実行してください。",
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    if any(t in text for t in ("401", "unauthorized", "invalid_token", "expired")):
        return DescriptError(
            "MCP_OAUTH_REQUIRED",
            "Descript との連携が未認可です。",
            _OAUTH_HINT,
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    if any(t in text for t in ("403", "forbidden")):
        return DescriptError(
            "MCP_FORBIDDEN",
            "Descript の操作権限がありません。",
            "対象 Drive での編集権限をご確認ください。",
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    return DescriptError(
        "JOB_FAILED",
        f"Descript ツール '{tool_name}' の実行に失敗しました。",
        "",
        {"tool": tool_name, "raw": str(exc)[:800]},
    )


async def _mcp_call(client, specs_by_name: dict, tool_map: dict, op: str,
                    args: dict, arg_maps: Optional[dict] = None) -> Any:
    """論理操作名で MCP ツールを呼ぶ。呼び出し側は実ツール名を意識しない。"""
    name = (tool_map or {}).get(op)
    if not name:
        raise DescriptError(
            "TOOL_UNRESOLVED",
            f"操作 '{op}' に対応する Descript MCP ツールを特定できませんでした。",
            "「Descript MCP 診断」アクションでツール名を確認し、対応する Valve に設定してください。",
            {"op": op, "tools": list(specs_by_name.values())},
        )

    spec = specs_by_name.get(name) or {}
    payload, missing = _coerce_args(spec, args, (arg_maps or {}).get(op))
    if missing:
        _log_warn("mcp.args_missing", op=op, tool=name, missing=missing)
        raise DescriptError(
            "TOOL_ARGS_MISSING",
            f"ツール '{name}' の必須引数が不足しています: {', '.join(missing)}",
            "tool_arg_map Valve で引数名の対応を設定してください。",
            {"tool": name, "missing": missing, "spec": spec},
        )

    # 引数は「キー名だけ」を出す。値には署名 URL やファイル名が入りうる。
    _log_info("mcp.call", op=op, tool=name, args=sorted(payload.keys()))
    started = time.monotonic()
    try:
        raw = await client.call_tool(name, payload)
    except DescriptError:
        raise
    except Exception as exc:
        _log_error("mcp.error", op=op, tool=name,
                   ms=_ms(started), error=str(exc)[:400])
        raise _classify_mcp_error(exc, name) from exc

    result = _unwrap_mcp(raw)
    # 応答は型とキーだけ。本文を流すとログが肥大し、署名 URL の漏洩経路になる。
    _log_info(
        "mcp.result",
        op=op,
        tool=name,
        ms=_ms(started),
        type=type(result).__name__,
        keys=sorted(str(k) for k in result.keys()) if isinstance(
            result, dict) else None,
        size=len(result) if isinstance(result, (list, str)) else None,
    )
    return result


# ===========================================================================
# 契約書 §5.6 封筒の詰め／開け
# ===========================================================================


def _ok(op: str, data: Any = None, state: Optional[dict] = None) -> str:
    return json.dumps(
        {"ok": True, "op": op, "data": data if data is not None else {},
            "state": state or {}},
        ensure_ascii=False,
    )


def _fail(op: str, code: str, message_ja: str, hint: str = "", data: Any = None) -> str:
    return json.dumps(
        {"ok": False, "op": op, "code": code,
            "message_ja": message_ja, "hint": hint, "data": data},
        ensure_ascii=False,
    )


def _unpack_pipe_response(res: Any) -> dict:
    """generate_chat_completion(stream=False) の戻り値から封筒を取り出す。"""
    if isinstance(res, str):
        content = res
    elif isinstance(res, dict):
        try:
            content = res["choices"][0]["message"]["content"]
        except Exception:
            content = json.dumps(res, ensure_ascii=False)
    else:
        content = str(res)

    try:
        envelope = json.loads(content)
    except Exception:
        return {
            "ok": False,
            "op": "",
            "code": "INTERNAL",
            "message_ja": "オーケストレータの応答を解釈できませんでした。",
            "hint": "",
            "data": {"raw": str(content)[:2000]},
        }

    if not isinstance(envelope, dict) or "ok" not in envelope:
        return {
            "ok": False,
            "op": "",
            "code": "INTERNAL",
            "message_ja": "オーケストレータの応答形式が不正です。",
            "hint": "",
            "data": {"raw": envelope},
        }
    return envelope


# ===========================================================================
# 契約書 §5.7 __event_call__ の戻り値判定（Action 専用。3 ファイル共通展開）
# ===========================================================================

_OAUTH_HINT = (
    "チャット入力欄の ＋ ボタン → Integrations → Tools から Descript を一度有効化し、"
    "ブラウザに表示される同意画面を完了してください。"
    "（一度完了すれば、以降のトークン更新は自動で行われます）"
)


def _form_result(value: Any) -> tuple[bool, Any, Optional[dict]]:
    """__event_call__ の戻り値を (成功か, 値, エラー封筒) に分解する。

    ユーザキャンセル -> False（Chat.svelte:3758-3769）
    タイムアウト/切断 -> {'error': ...}（socket/main.py:1119-1127）
    """
    if value is False or value is None:
        return False, None, {
            "ok": False, "code": "USER_CANCELLED",
            "message_ja": "中止しました。", "hint": "", "data": None,
        }
    if isinstance(value, dict) and "error" in value:
        return False, None, {
            "ok": False, "code": "UI_DISCONNECTED",
            "message_ja": "画面との接続が切れたため入力を受け取れませんでした。",
            "hint": "もう一度お試しください。", "data": {"raw": value},
        }
    return True, value, None


# ===========================================================================
# 契約書 §5.8 ジョブポーリング（Pipe 専用）
# ===========================================================================


def _accepts_arg(spec: dict, arg_name: str, arg_map: Optional[dict] = None) -> bool:
    """ツールのスキーマがその引数を受け付けるかを _coerce_args と同じ規則で判定する。

    スキーマ不明（properties が空）なら _coerce_args は素通しするので True を返す。
    """
    props = ((spec or {}).get("parameters") or {}).get("properties") or {}
    if not props:
        return True
    name = (arg_map or {}).get(arg_name, arg_name)
    if name in props:
        return True
    return _norm_key(name) in {_norm_key(k) for k in props}


async def _poll_job(client, specs_by_name: dict, tool_map: dict, arg_maps: dict,
                    job_id: str, emit_status, valves) -> dict:
    """Descript の非同期ジョブを完了まで追跡する。

    job_state は queued -> running -> stopped (+ cancelled)。
    stopped は「終わった」だけで成功ではない。必ず result.status を見る。

    ⚠️ 実測ツール `wait_for_job` はポーリングではなく**ブロッキング待機**で、
    wait_seconds を省略すると既定 300 秒ブロックする（契約書 §2.0.1 ③）。
    1 回の呼び出しで 300 秒待つと Action→Pipe の HTTP がリバースプロキシに
    切られるため、poll_wait_seconds（既定 25）を必ず明示して渡す。
    ブロッキング待機が効いている間は asyncio.sleep での二重待機を行わない。
    """
    started = time.monotonic()
    interval = float(valves.poll_interval_sec)
    ticks = 0

    # wait_seconds を実際に受け付けるツールかどうかで待ち方を切り替える。
    # 受け付けないツール（＝従来型のポーリング API）なら _coerce_args に
    # 落とされるだけなので、その場合は今までどおり sleep で間隔を空ける。
    wait_seconds = max(0, int(getattr(valves, "poll_wait_seconds", 0) or 0))
    job_spec = specs_by_name.get(
        (tool_map or {}).get("job_status") or "") or {}
    blocking = wait_seconds > 0 and _accepts_arg(
        job_spec, "wait_seconds", (arg_maps or {}).get("job_status")
    )
    # ブロッキング時は 1 tick がそのまま数十秒なので、毎回 status を更新する。
    status_every_n = 1 if blocking else max(1, int(valves.poll_status_every_n))

    while True:
        poll_args = {"job_id": job_id}
        if blocking:
            poll_args["wait_seconds"] = wait_seconds
        call_started = time.monotonic()
        raw = await _mcp_call(client, specs_by_name, tool_map, "job_status", poll_args, arg_maps)
        # ブロッキング待機のはずが即座に返る実装だった場合、ここで sleep を
        # 飛ばすと API を叩き続ける熱いループになる。実測の待ち時間で判定する。
        waited = time.monotonic() - call_started
        state = str(_pick(raw, "job_state", "state",
                    "status", default="") or "").lower()
        result = _pick(raw, "result", default={}) or {}
        _log_info(
            "job.tick",
            job_id=job_id,
            state=state or "(none)",
            tick=ticks + 1,
            waited_ms=int(waited * 1000),
            blocking=blocking,
        )

        if state in ("stopped", "completed", "succeeded", "success", "finished"):
            status = str(_pick(result, "status", "result_status",
                         default="") or "").lower()
            if status == "success":
                _log_info("job.done", job_id=job_id,
                          ms=_ms(started), partial=False)
                return {"ok": True, "partial": False, "result": result}
            if status == "partial":
                _log_warn("job.done", job_id=job_id,
                          ms=_ms(started), partial=True)
                return {"ok": True, "partial": True, "result": result}
            if status == "":
                # result を返さない実装向けのフォールバック
                _log_warn("job.done_no_status", job_id=job_id, ms=_ms(started))
                return {"ok": True, "partial": False, "result": result}
            _log_error("job.failed", job_id=job_id,
                       ms=_ms(started), status=status)
            raise DescriptError(
                "JOB_FAILED",
                str(_pick(result, "error_message", "message", default="処理が失敗しました。")),
                "",
                {"job_id": job_id, "result": result},
            )

        if state in ("cancelled", "canceled"):
            _log_warn("job.cancelled", job_id=job_id, ms=_ms(started))
            raise DescriptError(
                "JOB_CANCELLED", "処理がキャンセルされました。", "", {"job_id": job_id})

        ticks += 1
        elapsed = int(time.monotonic() - started)
        if ticks % status_every_n == 0:
            progress = _pick(raw, "progress", default={}) or {}
            label = _pick(progress, "label", default="処理中")
            percent = _pick(progress, "percent")
            suffix = f" {percent}%" if percent is not None else ""
            await emit_status(f"{label}{suffix}（{elapsed} 秒経過 / job {str(job_id)[:8]}）")

        if time.monotonic() - started > float(valves.poll_timeout_sec):
            _log_error("job.timeout", job_id=job_id,
                       ms=_ms(started), ticks=ticks)
            raise DescriptError(
                "JOB_TIMEOUT",
                f"処理が {int(valves.poll_timeout_sec)} 秒以内に完了しませんでした。",
                "Descript 側では処理が継続している可能性があります。",
                {"job_id": job_id},
            )

        if not blocking or waited < 1.0:
            # ブロッキング待機が効いていない実装向けの従来ポーリング。
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, float(valves.poll_backoff_max_sec))


# ===========================================================================
# 契約書 §5.9 署名付き URL の秘匿
# ===========================================================================

# 署名付き URL の検出。
# ⚠️ 意図的に「AWS SigV4 / GCS 署名の固有パラメータ」だけに絞っている。
#    汎用的な token= / Expires= を入れると、Descript と無関係な URL まで
#    outlet で書き換えてしまい、チャット履歴が不可逆に壊れる。
_SIGNED_URL_RE = re.compile(
    r"https?://[^\s\"'<>)\]]+?[?&]"
    r"(?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|X-Goog-Signature|GoogleAccessId)"
    r"=[^\s\"'<>)\]]*",
    re.IGNORECASE,
)

_SIGNED_URL_PLACEHOLDER = "（ダウンロードリンクは期限切れのため非表示）"


def _redact_signed_urls(text: str, replacement: str = "") -> str:
    """期限付き署名 URL を除去/置換する。

    Descript の download_url は署名付き・期限付きなので、チャット履歴に
    残すと後日 403 になる。share_url など恒久リンクに差し替える。

    replacement に恒久 URL（share_url）を渡せばリンクとして生き続ける。
    空なら注記文言に置き換える。
    """
    if not text or not isinstance(text, str):
        return text
    return _SIGNED_URL_RE.sub(replacement or _SIGNED_URL_PLACEHOLDER, text)


# ===========================================================================
# 契約書 §2.3 ツール名の正規表現候補
# ===========================================================================

# 先頭ほど高得点。2026-08-02 の probe で確定した実測ツール名を各行の先頭に置き、
# 自動解決が確実に当たるようにしている（契約書 §2.0）。
_TOOL_PATTERNS = {
    "list_projects": [r"^list_?projects?$", r"list.*project", r"projects?_?list", r"^get_?projects?$", r"search.*project"],
    "get_project":   [r"^get_?project$", r"project.*(detail|info|metadata)", r"^describe_?project$", r"open.*project"],
    "import_media":  [r"^import_?media$", r"import.*(media|file|video|url)", r"^upload", r"(create|add).*(composition|media)", r"add.*file"],
    "agent_edit":    [r"^prompt_?project_?agent$", r"agent.*edit", r"prompt.*agent", r"^agent$", r"underlord", r"^edit$", r"apply.*edit", r"edit.*project"],
    "publish":       [r"^publish_?project$", r"^publish", r"export.*(video|media|link)", r"^share", r"render"],
    "job_status":    [r"^wait_?for_?job$", r"job.*(status|state)", r"^get_?job$", r"^poll", r"task.*status"],
    "export_timeline": [r"^export_?timeline$", r"export.*(timeline|fcpxml|edl|aaf|sesx)", r"timeline.*export"],
    "report_upload_status": [r"^report_?upload_?status$", r"report.*upload", r"upload.*status"],
}

# description に含まれていれば加点するキーワード（§2.3 の「日本語/英語キーワード」）
_TOOL_KEYWORDS = {
    "list_projects": ("list", "projects", "all project", "プロジェクト", "一覧"),
    "get_project":   ("detail", "metadata", "composition", "media files", "詳細", "プロジェクト"),
    "import_media":  ("import", "upload", "media", "video", "audio", "取り込", "アップロード"),
    "agent_edit":    ("underlord", "agent", "edit", "instruction", "prompt", "編集"),
    "publish":       ("publish", "export", "share", "render", "download", "書き出", "公開"),
    "job_status":    ("job", "status", "progress", "poll", "wait", "ジョブ", "進捗"),
    "export_timeline": ("timeline", "fcpxml", "final cut", "premiere", "edl", "タイムライン"),
    "report_upload_status": ("upload", "status", "report", "failed", "アップロード"),
}

# 本 Function 自身の id。編集プロンプト合成で自分自身を呼んで無限再帰するのを防ぐ。
_FUNCTION_ID = "descript_pipe"

# 解像度 → アスペクト比
_ASPECT_BY_RESOLUTION = {
    "1080x1920": "9:16",
    "720x1280": "9:16",
    "1080x1080": "1:1",
    "1920x1080": "16:9",
}

_DEFAULT_SYSTEM_PROMPT = """あなたは Descript Underlord に渡す編集プロンプトを組み立てる動画編集ディレクターです。

ユーザの日本語の編集指示と、有効化されている編集機能フラグ、目標尺・アスペクト比を読み取り、
Descript Underlord がそのまま実行できる**英語の編集プロンプトを 1 つだけ**生成してください。

制約:
- 出力は編集プロンプトの本文のみ。前置き・解説・引用符・Markdown の装飾を付けない。
- 命令形の英語で 200 語以内。1〜3 文の連続した指示にまとめる。
- 「有効な編集機能」に挙がっていない編集操作を勝手に足さない。ユーザ指示に無い演出を創作しない。
- ターゲット尺とアスペクト比が与えられている場合は必ず言及する。
- 素材に存在しない映像・音声を新規生成させる指示は書かない。
"""

# ===========================================================================
# 契約書 §6 HTML テンプレート正本
# ===========================================================================

_HEIGHT_JS = """
<script>
(function () {
  function report() {
    parent.postMessage(
      { type: 'iframe:height', height: document.documentElement.scrollHeight + 24 }, '*');
  }
  try { new ResizeObserver(report).observe(document.documentElement); } catch (e) {}
  addEventListener('load', report);
  addEventListener('resize', report);
  setTimeout(report, 100);
  setTimeout(report, 600);
})();
</script>
"""

_BASE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font: 14px/1.65 system-ui, -apple-system, "Segoe UI", "Noto Sans JP", sans-serif;
  background: transparent; color: #1a1a1e;
}
.wrap { padding: 14px; }
.card { background: #fff; border: 1px solid #e3e3e8; border-radius: 14px; padding: 14px; }
.h { font-weight: 650; font-size: 15px; margin: 0 0 10px; }
.muted { opacity: .62; font-size: 12px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.btn { padding: 9px 16px; border-radius: 10px; border: 1px solid #d9d9e0;
       background: #f4f4f6; color: #1a1a1e; font: inherit; font-size: 13px;
       cursor: pointer; text-decoration: none; display: inline-block; }
.btn:hover { background: #ebebef; }
.btn.primary { background: #3b6fe0; border-color: #3b6fe0; color: #fff; }
.btn.primary:hover { background: #3260cc; }
textarea { width: 100%; min-height: 84px; padding: 10px; border-radius: 10px;
           border: 1px solid #d9d9e0; font: inherit; resize: vertical;
           background: #fff; color: inherit; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid #e8e8ee;
         vertical-align: top; }
th { font-weight: 600; opacity: .7; font-size: 12px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
       background: #f0f0f4; padding: 1px 5px; border-radius: 5px; }
.scroll { overflow-x: auto; }
@media (prefers-color-scheme: dark) {
  body { color: #e8e8ec; }
  .card { background: #1c1c20; border-color: #303038; }
  .btn { background: #2a2a31; border-color: #3a3a44; color: #e8e8ec; }
  .btn:hover { background: #33333c; }
  textarea { background: #16161a; border-color: #3a3a44; }
  th, td { border-bottom-color: #2c2c34; }
  code { background: #26262e; }
}
"""

_PLAYER = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">{{title}}</p>
  <video src="{{src}}" controls playsinline preload="metadata"
         style="width:100%;max-height:70vh;border-radius:10px;background:#000;display:block"></video>
  <div class="row">
    <a class="btn primary" href="{{app_url}}" target="_blank" rel="noopener">Descript で開く</a>
    <a class="btn" href="{{share_url}}" target="_blank" rel="noopener">共有リンク</a>
  </div>
  <p class="muted" style="margin:10px 0 0">rev {{revision}} ・ {{note}}</p>
</div></div>{{height_js}}</body></html>"""

_TOOL_TABLE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">Descript MCP ツール一覧（{{count}} 件）</p>
  <p class="muted">{{lead}}</p>
  <div class="scroll"><table>
    <thead><tr><th style="width:30%">ツール名</th><th style="width:16%">推定</th><th>説明 / 必須引数</th></tr></thead>
    <tbody>{{rows}}</tbody>
  </table></div>
  <p class="muted" style="margin-top:12px">
    ここで確認した名前を Admin → Functions → Descript Orchestrator の Valves
    <code>tool_list_projects</code> / <code>tool_get_project</code> / <code>tool_import_media</code> /
    <code>tool_agent_edit</code> / <code>tool_publish</code> / <code>tool_job_status</code> に設定してください。
  </p>
</div></div>{{height_js}}</body></html>"""

_EDIT_LOOP_FORM = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">{{title}}</p>
  <video src="{{src}}" controls playsinline preload="metadata"
         style="width:100%;max-height:60vh;border-radius:10px;background:#000;display:block"></video>
  <p class="muted" style="margin:10px 0 6px">{{agent_response}}</p>
  <textarea id="d-input" placeholder="追加の編集指示（例: 冒頭 3 秒をカット、字幕をもう少し大きく）"></textarea>
  <div class="row">
    <button class="btn primary" type="button" data-act="revise">この指示で再編集</button>
    <button class="btn" type="button" data-act="confirm">これで確定する</button>
    <a class="btn" href="{{app_url}}" target="_blank" rel="noopener">Descript で開く</a>
  </div>
  <p class="muted" style="margin:10px 0 0">rev {{revision}} / 残り {{remaining}} 回</p>
</div></div>
<script>
(function () {
  document.querySelectorAll('button[data-act]').forEach(function (b) {
    b.addEventListener('click', function () {
      var payload = JSON.stringify({
        op: b.getAttribute('data-act'),
        text: (document.getElementById('d-input') || {}).value || ''
      });
      parent.postMessage({ type: 'input:prompt:submit', text: '@descript ' + payload }, '*');
      document.querySelectorAll('button[data-act]').forEach(function (x) { x.disabled = true; });
    });
  });
})();
</script>{{height_js}}</body></html>"""


# ===========================================================================
# Pipe 固有のユーティリティ
# ===========================================================================


def _make_emit_status(event_emitter):
    """status イベントの送出関数を作る。emitter が無い経路でも落ちないようにする。"""

    async def emit(description: Any, done: bool = False) -> None:
        if event_emitter is None:
            return
        try:
            await event_emitter(
                {"type": "status", "data": {
                    "description": str(description), "done": bool(done)}}
            )
        except Exception:
            pass

    return emit


async def _emit_embeds(event_emitter, html: str) -> None:
    """embeds を全置換で送る（backend は追記・frontend は全置換のため replace 必須）。"""
    if event_emitter is None or not html:
        return
    try:
        await event_emitter({"type": "embeds", "data": {"embeds": [html], "replace": True}})
    except Exception:
        pass


def _parse_json_valve(raw: Any) -> dict:
    """JSON 文字列の Valve を dict にする。壊れていても例外にしない。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_llm_text(res: Any) -> str:
    """generate_chat_completion(stream=False) の戻り値から本文テキストを取り出す。"""
    if isinstance(res, str):
        return res
    if hasattr(res, "model_dump"):
        try:
            res = res.model_dump()
        except Exception:
            return ""
    if isinstance(res, dict):
        try:
            content = res["choices"][0]["message"]["content"]
        except Exception:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(
                p, dict) and p.get("type") == "text"]
            return "".join(parts)
    return ""


def _strip_code_fence(text: str) -> str:
    """LLM が付けた ```json ... ``` を剥がす。"""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _aspect_from_resolution(resolution: str) -> str:
    key = str(resolution or "").strip().lower().replace("×", "x")
    if key in _ASPECT_BY_RESOLUTION:
        return _ASPECT_BY_RESOLUTION[key]
    match = re.match(r"^(\d+)\s*x\s*(\d+)$", key)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if height and width:
            if width * 16 == height * 9:
                return "9:16"
            if width == height:
                return "1:1"
            if width * 9 == height * 16:
                return "16:9"
            return f"{width}:{height}"
    return "9:16"


def _as_project_list(raw: Any) -> list:
    """list_projects の戻り値を [{'id','name',...}] に正規化する。"""
    items = raw
    if isinstance(raw, dict):
        items = _pick(raw, "projects", "items", "data", "results", default=[])
    if not isinstance(items, list):
        return []

    projects = []
    for item in items:
        if not isinstance(item, dict):
            continue
        project_id = _pick(item, "id", "project_id", "projectId", "uuid")
        if project_id is None:
            continue
        projects.append(
            {
                "id": str(project_id),
                "name": str(_pick(item, "name", "title", "project_name", "displayName", default="") or ""),
                "updated_at": _pick(item, "updated_at", "updatedAt", "modified_at", "last_modified"),
                "folder_path": _pick(item, "folder_path", "folderPath", "path", "drive_name"),
            }
        )
    return projects


def _normalize_media_type(value: Any) -> str:
    """publish_project の media_type を実測値 'Video' / 'Audio' に正規化する。

    実測（契約書 §2.0.1 ②）では先頭大文字の 'Video' / 'Audio' のみが通り、
    'mp4' / 'mp3' / 'wav' のような拡張子表記は 422 で弾かれる。
    Valve に旧既定値（mp4 等）が残っている環境でも動くようここで吸収する。
    """
    key = str(value or "").strip().lower()
    if key in ("audio", "mp3", "wav", "m4a", "aac", "flac", "ogg", "opus", "wma"):
        return "Audio"
    return "Video"


def _looks_like_no_video(exc: "DescriptError") -> bool:
    """publish の失敗が「映像トラックが無いのに Video を指定した」ものか推定する。

    実測（契約書 §2.0.1 ②）: media_type='Video' を明示しかつ映像が無いと 422。
    """
    parts = [str(exc.message_ja or "")]
    try:
        parts.append(json.dumps(exc.data, ensure_ascii=False, default=str))
    except Exception:
        parts.append(str(exc.data))
    text = " ".join(parts).lower()
    if "422" in text or "unprocessable" in text:
        return True
    return any(t in text for t in ("no video", "video track", "without video", "audio only"))


# export_timeline の format（契約書 §2.0.1 ④）
_TIMELINE_FORMATS = ("fcp", "premiere", "davinci_resolve",
                     "aaf", "edl", "sesx")


# ===========================================================================
# 契約書 §1.6 通常チャットのインテント振り分け
# ===========================================================================

# これを超える長さの入力は一切振り分けない。自然文の編集指示を守るための上限。
_CHAT_INTENT_MAX_LEN = 24

# ⚠️ すべて ^…$ で全体一致にすること。部分一致にすると
#    「冒頭をカットして書き出して」のような本物の編集指示が export に奪われる。
#    照合対象は _normalize_command() を通した「空白を除いた小文字」。
_CHAT_INTENTS = (
    (
        "probe",
        re.compile(
            r"^(?:descript)?(?:mcp)?(?:の)?(?:診断|接続確認|疎通確認|接続テスト)(?:する|して|します)?$"
            r"|^probe$"
        ),
    ),
    (
        "list_projects",
        re.compile(
            r"^(?:descript)?(?:の)?プロジェクト(?:の)?(?:一覧|リスト)(?:を)?"
            r"(?:表示|表示する|見る|見せて|ちょうだい)?$"
            r"|^(?:list)?projects?$"
        ),
    ),
    (
        "export_timeline",
        re.compile(
            r"^(?:タイムライン|動画|映像|プロジェクト)?(?:を|の)?"
            r"(?:エクスポート|書き出し|書出し|書き出す|書出す|書き出して|出力|fcpxml)"
            r"(?:する|して|したい|します|お願い)?$"
            r"|^export(?:timeline)?$"
        ),
    ),
    (
        "import_media",
        re.compile(
            r"^(?:動画|映像|ファイル|素材)?(?:を|の)?"
            r"(?:アップロード|取り込み|取込み|取り込む|取込む|取込|インポート|追加)"
            r"(?:する|して|したい|します|お願い)?$"
            r"|^(?:upload|import)$"
        ),
    ),
    (
        "start_edit_loop",
        re.compile(
            r"^(?:動画|映像|プロジェクト)?(?:を|の)?(?:編集|編集開始|カット編集)"
            r"(?:する|して|したい|します|開始|を開始)?$"
            r"|^edit$"
        ),
    ),
)


def _normalize_command(text: Any) -> str:
    """コマンド句の照合用に正規化する（契約書 §1.6）。

    小文字化 → 空白（半角/全角）除去 → 末尾の句読点・記号除去。
    """
    body = str(text or "").strip().lower()
    body = re.sub(r"[\s　]+", "", body)
    return body.strip("。．.!！?？、,･・")


def _match_chat_intent(text: Any) -> Optional[str]:
    """短いコマンド句を descript_op に振り分ける。該当しなければ None。

    Suggestion Prompts（新規チャット画面）のクリックは、この関数が拾える
    短い句をそのまま送信してくる（契約書 §1.6）。
    """
    body = _normalize_command(text)
    if not body or len(body) > _CHAT_INTENT_MAX_LEN:
        return None
    for op, pattern in _CHAT_INTENTS:
        if pattern.match(body):
            return op
    return None


def _media_key(name: Any, url: Any, used: set) -> str:
    """add_media のキー（Descript 上の表示名）を作る。

    元のファイル名 → URL の末尾 の順に採る。重複時は連番を付ける。
    """
    base = str(name or "").strip()
    if not base and url:
        path = str(url).split("?", 1)[0].split("#", 1)[0].rstrip("/")
        base = path.rsplit("/", 1)[-1]
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip() or "media"

    key = base
    index = 2
    while key in used:
        stem, dot, ext = base.rpartition(".")
        key = f"{stem}_{index}{dot}{ext}" if dot else f"{base}_{index}"
        index += 1
    used.add(key)
    return key


def _build_add_media(entries: list) -> tuple[dict, dict]:
    """import_media の add_media（表示名 → メディア定義）を組み立てる（契約書 §2.0.1 ①）。

    tool_arg_map はキー名の付け替えしかできないため、この構造変換はコード側で行う。

    entries の要素: {"name", "url", "path", "content_type", "file_size"}
    戻り値: (add_media, uploads)
      uploads は upload_urls への PUT が必要なもの {表示名: entry}
    """
    add_media: dict = {}
    uploads: dict = {}
    used: set = set()

    for entry in entries or []:
        key = _media_key(entry.get("name"), entry.get("url"), used)
        url = entry.get("url")
        if url:
            # URL 取込: {"表示名": {"url": "https://..."}}
            add_media[key] = {"url": str(url)}
            continue
        # 直接アップロード: {"表示名": {"content_type": ..., "file_size": ...}}
        # file_size は宣言値と実サイズが一致しないと失敗するため実測値を入れる。
        definition = {"file_size": int(entry.get("file_size") or 0)}
        if entry.get("content_type"):
            definition["content_type"] = str(entry["content_type"])
        add_media[key] = definition
        uploads[key] = entry

    return add_media, uploads


def _extract_job_id(raw: Any) -> Optional[str]:
    """ツールの戻り値から非同期ジョブ ID を取り出す。無ければ None。"""
    if not isinstance(raw, dict):
        return None
    job_id = _pick(raw, "job_id", "jobId")
    if job_id is None:
        state_hint = str(_pick(raw, "job_state", "state",
                         "status", default="") or "").lower()
        if state_hint in ("queued", "running", "pending", "in_progress", "processing"):
            job_id = _pick(raw, "id")
    return str(job_id) if job_id is not None else None


def _file_chunks(path: str, chunk_size: int = 1024 * 1024):
    """ファイルをチャンクで読む同期ジェネレータ。

    httpx は Content-Length を明示していれば Transfer-Encoding: chunked を
    付けない（_prepare が既存の Content-Length を尊重する）ため、
    署名付き PUT でも壊れない。
    """
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _diag_response(raw: Any, sent_keys: Any, limit: int = 900) -> str:
    """MCP 応答が期待した形でないときに、どの層で壊れたかを示す診断文を作る。

    Function からはログを出せないため、封筒の data['raw'] に載せるのが唯一の
    手掛かりになる（Action の _error_markdown が <details> に描画し、
    _redact_signed_urls で署名 URL を伏せる）。

    型だけで層が切り分けられる:
      list  … MCPClient.call_tool が返した content が空/非 text。
              サーバが structuredContent のみを返している可能性
              （utils/mcp/client.py は content しか返さない）
      str   … text ブロックが JSON として parse できなかった。
              人間向けサマリを返すツールだと _unwrap_mcp が素通しする
      dict  … 応答は取れている。キー名の揺れか、Descript 側が直接
              アップロードとして受け取っていない
    """
    lines = [
        f"送信した add_media キー: {sorted(sent_keys)}",
        f"応答の型: {type(raw).__name__}",
    ]
    if isinstance(raw, dict):
        lines.append(f"応答のキー: {sorted(str(k) for k in raw.keys())}")
    elif isinstance(raw, list):
        lines.append(f"応答の要素数: {len(raw)}")
    lines.append(f"応答（先頭 {limit} 文字）: {repr(raw)[:limit]}")
    return "\n".join(lines)


def _extract_publish(result: Any) -> dict:
    """publish 系ジョブの結果から URL 群を取り出す。"""
    payload = result if isinstance(result, dict) else {}
    nested = _pick(payload, "media", "output", "asset",
                   "published", default={}) or {}
    if not isinstance(nested, dict):
        nested = {}

    def grab(*keys):
        return _pick(payload, *keys) or _pick(nested, *keys)

    return {
        "share_url": grab("share_url", "shareUrl", "published_url", "publishedUrl", "url", "link"),
        "download_url": grab("download_url", "downloadUrl", "media_url", "mediaUrl", "asset_url", "file_url"),
        "composition_id": grab("composition_id", "compositionId", "sequence_id"),
        "project_url": grab("project_url", "projectUrl", "app_url", "appUrl", "edit_url", "editor_url"),
    }


# ===========================================================================
# Pipe 本体
# ===========================================================================


class Pipe:
    """Descript オーケストレータ。

    ★ pipes() は定義しない（manifold にしない）。モデル選択 UI を汚さないため、
      操作の分岐は body['descript_op'] で行う。
    """

    class Valves(BaseModel):
        # --- MCP 接続 ---
        mcp_server_id: str = Field(
            default="",
            description="Admin Settings > External Tools で登録した Descript MCP の info.id",
        )
        mcp_connect_retries: int = Field(default=2, description="MCP 接続の再試行回数")
        # --- ツール名オーバーライド（空なら自動推定） ---
        # description の「実測値」は 2026-08-02 に probe で確認した実際のツール名
        # （契約書 §2.0）。本番は tool_resolution_mode=valve_only にしてここへ明示する。
        tool_list_projects: str = Field(
            default="", description="空なら自動推定。実測値は list_projects"
        )
        tool_get_project: str = Field(
            default="", description="空なら自動推定。実測値は get_project"
        )
        tool_import_media: str = Field(
            default="", description="空なら自動推定。実測値は import_media"
        )
        tool_agent_edit: str = Field(
            default="", description="空なら自動推定。実測値は prompt_project_agent"
        )
        tool_publish: str = Field(
            default="", description="空なら自動推定。実測値は publish_project"
        )
        tool_job_status: str = Field(
            default="", description="空なら自動推定。実測値は wait_for_job"
        )
        tool_export_timeline: str = Field(
            default="export_timeline",
            description="タイムライン書き出し（FCPXML 等）。実測値は export_timeline",
        )
        tool_report_upload_status: str = Field(
            default="report_upload_status",
            description=(
                "直接アップロード失敗を import ジョブに通知するツール。"
                "実測値は report_upload_status"
            ),
        )
        tool_arg_map: str = Field(
            default="{}",
            description='引数名の差異を吸収する JSON。例 {"agent_edit": {"project_id": "projectId"}}',
        )
        tool_resolution_mode: str = Field(
            default="regex_then_llm",
            json_schema_extra={
                "input": {"type": "select", "options": ["valve_only", "regex_only", "regex_then_llm"]}
            },
            description="ツール名の解決方法。本番では valve_only 推奨",
        )
        # --- ジョブポーリング ---
        poll_wait_seconds: int = Field(
            default=25,
            description=(
                "wait_for_job に渡す 1 回あたりのブロッキング待機秒数。"
                "省略すると MCP 側の既定 300 秒ブロックし、Action→Pipe の HTTP が"
                "リバースプロキシに切られる。0 で即時リターン（従来のポーリング相当）"
            ),
        )
        poll_interval_sec: int = Field(
            default=5, description="ブロッキング待機が使えないときの再問い合わせ間隔（秒）"
        )
        poll_backoff_max_sec: int = Field(default=30)
        poll_timeout_sec: int = Field(
            default=900, description="1 ジョブの上限待ち時間（秒）")
        poll_status_every_n: int = Field(
            default=3,
            description="N 回に 1 回 status イベントを出す（ブロッキング待機時は毎回）",
        )
        upload_timeout_sec: int = Field(
            default=600, description="upload_urls への PUT 1 本あたりのタイムアウト（秒）"
        )
        # --- 編集ループ ---
        max_edit_iterations: int = Field(default=5)
        confirm_timeout_sec: int = Field(
            default=240,
            description="確認待ちの上限。WEBSOCKET_EVENT_CALLER_TIMEOUT（既定 300）未満にすること",
        )
        # --- 出力既定 ---
        default_media_type: str = Field(
            default="Video",
            json_schema_extra={
                "input": {"type": "select", "options": ["Video", "Audio"]}},
            description=(
                "publish_project の media_type。実測値は Video / Audio のみ。"
                "Video を明示しかつ映像が無いと 422 で失敗するため、その場合は"
                "自動的に Audio で 1 回だけ再試行する"
            ),
        )
        default_resolution: str = Field(
            default="1080x1920",
            json_schema_extra={
                "input": {"type": "select", "options": ["1080x1920", "1080x1080", "1920x1080", "720x1280"]}
            },
            description=(
                "編集プロンプトのアスペクト比算出にのみ使う。"
                "MCP 側スキーマに存在しない可能性あり・未確認のため publish には渡さない"
            ),
        )
        default_timeline_format: str = Field(
            default="fcp",
            json_schema_extra={"input": {"type": "select",
                                         "options": list(_TIMELINE_FORMATS)}},
            description=(
                "export_timeline の format。fcp = Final Cut Pro X（.fcpxml）。"
                "書き出されるのはタイムライン/XML のみでメディアは同梱されない"
            ),
        )
        # --- LLM ---
        editing_model_id: str = Field(
            default="",
            description="編集プロンプト生成に使うモデル ID。空なら body['model'] を使う。1 行で入力",
        )
        system_prompt_command: str = Field(
            default="",
            description="Prompts から取得するコマンド（完全一致）。空なら system_prompt_fallback を使う",
        )
        system_prompt_fallback: str = Field(default=_DEFAULT_SYSTEM_PROMPT)
        # --- 表示 ---
        embed_player: bool = Field(
            default=True, description="publish 結果を embeds のプレイヤーで表示する")
        redact_download_url: bool = Field(
            default=True, description="署名付き download_url をチャット履歴に残さない"
        )
        enable_chat_intents: bool = Field(
            default=True,
            description=(
                "通常チャットの短いコマンド句（「動画をアップロード」等）を "
                "descript_op に振り分ける（契約書 §1.6）。誤爆時は false で従来動作に戻る"
            ),
        )
        log_level: str = Field(
            default="INFO",
            description="この Function だけのログレベル。GLOBAL_LOG_LEVEL を上げずに詳細を見たいとき DEBUG にする",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
                }
            },
        )

    class UserValves(BaseModel):
        editing_style: str = Field(
            default="jet_cut",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": ["jet_cut", "caption_focus", "dynamic_effects", "full"],
                }
            },
        )
        enable_filler_removal: bool = Field(
            default=True, description="言い淀み（フィラー）の自動削除")
        enable_silence_cut: bool = Field(default=True, description="無音区間の自動削除")
        enable_studio_sound: bool = Field(
            default=True, description="ノイズ除去・音圧の自動マスタリング")
        enable_auto_captions: bool = Field(
            default=True, description="自動テロップ生成")
        enable_pan_zoom: bool = Field(default=False, description="オートパン&ズーム")
        enable_broll: bool = Field(default=False, description="インサート素材の自動配置")
        caption_language: str = Field(
            default="ja",
            json_schema_extra={
                "input": {"type": "select", "options": ["ja", "en", "auto"]}},
        )

    def __init__(self):
        self.valves = self.Valves()

    # -- エントリポイント ---------------------------------------------------

    async def pipe(
        self,
        body: dict,
        __user__=None,
        __request__=None,
        __event_emitter__=None,
        __event_call__=None,
        __chat_id__=None,
        __message_id__=None,
        __session_id__=None,
        __task__=None,
        __files__=None,
        __metadata__=None,
    ) -> str:
        # ★ ログレベルの適用は __init__ ではなくここで行う。Open WebUI は呼び出しの
        #    たびに function_module.valves をインスタンスごと差し替える
        #    （functions.py:61-66）ため、__init__ では常に既定値しか読めない。
        _apply_log_level(self.valves.log_level)

        # タイトル生成・タグ生成などの内部タスクでは MCP を叩かない（functions.py:262）。
        if __task__ is not None:
            return ""

        # Filter の inlet による事前診断（契約書 §1.4）。ok が false のときは
        # MCP に接続せず、この封筒をそのまま返す。
        preflight = (body or {}).get("descript_preflight") or {}
        if preflight and preflight.get("ok") is False:
            _log_warn(
                "pipe.preflight_blocked",
                chat_id=__chat_id__,
                code=preflight.get("code", "INTERNAL"),
            )
            return _fail(
                "preflight",
                preflight.get("code", "INTERNAL"),
                preflight.get("message_ja", ""),
                preflight.get("hint", ""),
            )

        op = ""
        started = time.monotonic()
        try:
            op, args = self._dispatch(body or {})
            ctx = {
                "body": body or {},
                "request": __request__,
                "user_raw": __user__,
                "user": _as_user_model(__user__),
                "user_valves": self._user_valves(__user__),
                "chat_id": __chat_id__ or (__metadata__ or {}).get("chat_id"),
                "message_id": __message_id__ or (__metadata__ or {}).get("message_id"),
                "session_id": __session_id__ or (__metadata__ or {}).get("session_id"),
                "metadata": __metadata__ or {},
                "files": __files__ or [],
                # Filter が RAG から退避した動画添付（契約書 §1.4）。
                # file_handler=True のとき form_data['files'] は
                # utils/filter.py:230-233 で削除されるため、__files__ ではなく
                # こちらが通常チャット経路の唯一の添付入力になる。
                "media": self._as_media_list((body or {}).get("descript_media")),
                "emitter": __event_emitter__,
                "emit_status": _make_emit_status(__event_emitter__),
            }
            ctx["state"] = await _load_state(ctx["chat_id"])
            _log_info(
                "pipe.start",
                op=op,
                chat_id=ctx["chat_id"],
                args=sorted(args.keys()),
                media=len(ctx["media"]),
                project_id=(ctx["state"] or {}).get("project_id"),
            )
            envelope = await self._open_mcp_and_run(op, args, ctx)
            _log_info("pipe.done", op=op,
                      chat_id=ctx["chat_id"], ms=_ms(started))
            return envelope
        except DescriptError as exc:
            # 封筒として返す失敗はここが唯一の ERROR 行。下位で二重に出さない。
            _log_error("pipe.fail", op=op, code=exc.code,
                       ms=_ms(started), message=exc.message_ja)
            return json.dumps(exc.envelope(op), ensure_ascii=False)
        except Exception as exc:  # 例外は絶対に外へ漏らさない
            _LOGGER.exception(
                "descript pipe.crash op=%s ms=%s", op, _ms(started))
            return _fail(
                op,
                "INTERNAL",
                "内部エラーが発生しました。",
                "Open WebUI のサーバログをご確認ください。",
                {"raw": str(exc)[:800]},
            )

    # -- ディスパッチ -------------------------------------------------------

    def _dispatch(self, body: dict) -> tuple[str, dict]:
        """descript_op があれば RPC。無ければ通常チャット経路として解釈する。"""
        op = str(body.get("descript_op") or "").strip()
        if op:
            args = body.get("descript_args")
            return op, dict(args) if isinstance(args, dict) else {}

        text = self._last_user_text(body)
        if text.startswith("@descript"):
            payload = _strip_code_fence(text[len("@descript"):].strip())
            try:
                parsed = json.loads(payload)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                # §6.3 のフォームは {"op": "revise"|"confirm"} を送る。
                # §1.3 の RPC 引数名は "action"。両方を受ける。
                action = str(parsed.get("action") or parsed.get(
                    "op") or "revise").strip()
                return "resume_edit_loop", {"action": action, "text": str(parsed.get("text") or "")}
            return "resume_edit_loop", {"action": "revise", "text": payload}

        # 短いコマンド句だけをインテントとして扱う（契約書 §1.6）。
        # 新規チャット画面の Suggestion Prompts はクリックで即送信されるため、
        # ここが Action を介さない開始導線になる。
        if bool(getattr(self.valves, "enable_chat_intents", True)):
            intent = _match_chat_intent(text)
            if intent:
                _log_info("pipe.intent", op=intent,
                          command=_normalize_command(text))
                # コマンド句自体を編集指示として渡しても意味がないので空にする。
                return intent, ({"instruction": ""} if intent == "start_edit_loop" else {})

        # 自然文の編集指示
        return "start_edit_loop", {"instruction": text}

    @staticmethod
    def _last_user_text(body: dict) -> str:
        messages = body.get("messages") or []
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "".join(parts).strip()
        return ""

    @staticmethod
    def _as_media_list(raw: Any) -> list:
        """body['descript_media'] を正規化する（契約書 §1.4）。

        要素は {"name", "url", "file_id", "content_type"}。url / file_id の
        いずれも無い要素は取り込みようがないので落とす。
        """
        if not isinstance(raw, list):
            return []
        media = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or None
            file_id = item.get("file_id") or None
            if not url and not file_id:
                continue
            media.append(
                {
                    "name": str(item.get("name") or ""),
                    "url": str(url) if url else None,
                    "file_id": str(file_id) if file_id else None,
                    "content_type": str(item.get("content_type") or "") or None,
                }
            )
        return media

    def _user_valves(self, user_raw: Any) -> "Pipe.UserValves":
        valves = (dict(user_raw or {})).get("valves")
        if isinstance(valves, self.UserValves):
            return valves
        if isinstance(valves, dict):
            try:
                return self.UserValves(**valves)
            except Exception:
                pass
        return self.UserValves()

    # -- MCP ライフサイクル -------------------------------------------------

    async def _open_mcp_and_run(self, op: str, args: dict, ctx: dict) -> str:
        """MCP に接続し op を処理して封筒を返す。

        ⚠️ connect と disconnect は必ずこの関数（＝同一 asyncio タスク）で対にする。
        asyncio.shield / asyncio.wait_for / anyio.CancelScope / anyio.fail_after で
        包むと MCP SDK の TaskGroup 制約に違反して壊れる
        （backend/open_webui/utils/mcp/client.py:163-172）。
        """
        server_id = str(self.valves.mcp_server_id or "").strip()
        if not server_id:
            raise DescriptError(
                "MCP_NOT_FOUND",
                "Descript MCP が登録されていません。",
                "Admin Settings → External Tools で Descript MCP を登録し、その info.id を "
                "Valves の mcp_server_id に設定してください。",
                {"server_id": ""},
            )

        # MCP_NOT_FOUND と MCP_FORBIDDEN は connect_mcp_server がどちらも None を返して
        # 区別できないため、先に接続定義の有無を自前で走査して切り分ける（契約書 §8）。
        try:
            connections = await Config.get("tool_server.connections", []) or []
        except Exception:
            connections = []
        found = any(
            isinstance(c, dict)
            and c.get("type") == "mcp"
            and (c.get("info") or {}).get("id") == server_id
            for c in connections
        )
        if not found:
            raise DescriptError(
                "MCP_NOT_FOUND",
                "Descript MCP が登録されていません。",
                "Admin Settings → External Tools に MCP (Streamable HTTP) 接続を追加し、"
                "その info.id を Valves の mcp_server_id に設定してください。",
                {"server_id": server_id},
            )

        # ⚠️ connect_mcp_server の呼び出しは disconnect と同じ関数（＝同一タスク）に
        # 置く。別タスクに逃がすと MCP SDK の TaskGroup が壊れる。
        attempts = max(0, int(self.valves.mcp_connect_retries)) + 1
        delay = 1.0
        last_exc: Optional[Exception] = None
        # build_tool_server_headers が参照する extra_params。oauth_2.1 接続では
        # request.app.state.oauth_client_manager が使われるためここは空でよい。
        extra_params = {"__oauth_token__": None}

        client = None
        specs = None
        try:
            for attempt in range(attempts):
                try:
                    connected = await connect_mcp_server(
                        ctx["request"], server_id, ctx["user"], ctx["metadata"], extra_params
                    )
                except Exception as exc:
                    last_exc = exc
                    classified = _classify_mcp_error(exc, f"mcp:{server_id}")
                    # 認可・権限・クレジット系は再試行しても結果が変わらない
                    if classified.code in ("MCP_OAUTH_REQUIRED", "MCP_FORBIDDEN", "QUOTA_EXCEEDED"):
                        raise classified from exc
                    connected = None

                if connected is not None:
                    client, specs = connected
                    break

                if last_exc is None:
                    # 例外なしで None ＝ has_connection_access 失敗（再試行しても同じ）
                    raise DescriptError(
                        "MCP_FORBIDDEN",
                        "Descript MCP へのアクセス権がありません。",
                        "Admin Settings → External Tools で当該接続のアクセス許可をご確認ください。",
                        {"server_id": server_id},
                    )

                if attempt < attempts - 1:
                    await ctx["emit_status"](
                        f"Descript MCP に再接続しています（{attempt + 2}/{attempts}）"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, 8.0)

            if client is None:
                raise DescriptError(
                    "MCP_CONNECT_FAILED",
                    "Descript MCP に接続できませんでした。",
                    "MCP_INITIALIZE_TIMEOUT（既定 10 秒）を延ばすか、しばらく待って再実行してください。",
                    {"server_id": server_id, "raw": str(
                        last_exc)[:800] if last_exc else ""},
                )

            specs_by_name = {
                str(spec.get("name")): spec
                for spec in (specs or [])
                if isinstance(spec, dict) and spec.get("name")
            }
            _log_info("mcp.connected", server_id=server_id,
                      tools=len(specs_by_name))
            tool_map = await self._resolve_tools(ctx, specs_by_name)
            _log_info(
                "mcp.resolved",
                mode=self.valves.tool_resolution_mode,
                resolved={k: v for k, v in tool_map.items() if v},
                unresolved=[
                    o for o in _LOGICAL_OPS_EXT if not tool_map.get(o)] or None,
            )
            arg_maps = _parse_json_valve(self.valves.tool_arg_map)
            return await self._handle(op, args, ctx, client, specs_by_name, tool_map, arg_maps, server_id)
        finally:
            if client is not None:
                await client.disconnect()
                _log_debug("mcp.disconnected", server_id=server_id)

    # -- ツール名の解決（契約書 §2） ----------------------------------------

    async def _resolve_tools(self, ctx: dict, specs_by_name: dict) -> dict:
        """論理操作 → 実ツール名の写像を作る。3 段フォールバック。"""
        mode = str(self.valves.tool_resolution_mode or "regex_then_llm").strip()

        # ① Valve の明示指定（実在するツール名のみ採用）
        valve_map = {}
        for op in _LOGICAL_OPS_EXT:
            name = str(getattr(self.valves, f"tool_{op}", "") or "").strip()
            if name and name in specs_by_name:
                valve_map[op] = name

        resolved = dict(valve_map)

        # ② 同一チャット内のキャッシュ（Valve が優先）
        cached = (ctx.get("state") or {}).get("tool_map")
        if isinstance(cached, dict):
            for op, name in cached.items():
                if op in _LOGICAL_OPS_EXT and op not in resolved and name in specs_by_name:
                    resolved[op] = name

        if mode == "valve_only":
            return resolved

        # ③ 正規表現スコアリング
        missing = [op for op in _LOGICAL_OPS_EXT if op not in resolved]
        if missing:
            used = set(resolved.values())
            for op in missing:
                name = self._resolve_by_regex(op, specs_by_name, used)
                if name:
                    resolved[op] = name
                    used.add(name)

        # ④ LLM 選択
        missing = [op for op in _LOGICAL_OPS_EXT if op not in resolved]
        if missing and mode == "regex_then_llm":
            try:
                guessed = await self._resolve_by_llm(ctx, specs_by_name, missing)
            except Exception:
                guessed = {}
            for op, name in guessed.items():
                if op in missing and name in specs_by_name:
                    resolved[op] = name

        if resolved != (cached if isinstance(cached, dict) else None):
            await _save_state(ctx.get("chat_id"), {"tool_map": resolved})
            ctx["state"] = {**(ctx.get("state") or {}), "tool_map": resolved}

        return resolved

    @staticmethod
    def _resolve_by_regex(op: str, specs_by_name: dict, used: set) -> Optional[str]:
        """パターン配列の前方ほど高得点。description のキーワードで加点。

        最高得点が 1 件に定まらなければ（同点が複数）採用しない＝次の段へ落とす。
        """
        patterns = _TOOL_PATTERNS.get(op, [])
        keywords = _TOOL_KEYWORDS.get(op, ())

        scores = {}
        for name, spec in specs_by_name.items():
            if name in used:
                continue
            base = 0
            for index, pattern in enumerate(patterns):
                try:
                    if re.search(pattern, name, re.IGNORECASE):
                        base = max(base, (len(patterns) - index) * 10)
                except re.error:
                    continue
            if base == 0:
                continue
            description = str((spec or {}).get("description") or "").lower()
            bonus = sum(3 for kw in keywords if kw.lower() in description)
            scores[name] = base + min(bonus, 12)

        if not scores:
            return None
        top = max(scores.values())
        winners = [name for name, score in scores.items() if score == top]
        return winners[0] if len(winners) == 1 else None

    async def _resolve_by_llm(self, ctx: dict, specs_by_name: dict, missing: list) -> dict:
        """ツール一覧を LLM に渡し、論理操作 → 実ツール名の JSON を返させる。"""
        model_id = self._resolve_llm_model(ctx)
        catalog = []
        for name, spec in specs_by_name.items():
            schema = (spec or {}).get("parameters") or {}
            catalog.append(
                {
                    "name": name,
                    "description": str((spec or {}).get("description") or "")[:280],
                    "required": list((schema.get("required") or []))[:8],
                }
            )

        instruction = (
            "以下は Descript MCP が公開しているツールの一覧です。\n"
            "論理操作名に最も適合するツール名を 1 つずつ選び、JSON オブジェクトだけを出力してください。\n"
            "該当が無い論理操作は値を null にしてください。説明文・Markdown は書かないこと。\n\n"
            f"# 解決したい論理操作\n{json.dumps(missing, ensure_ascii=False)}\n\n"
            "# 論理操作の意味\n"
            "- list_projects: プロジェクトの一覧取得\n"
            "- get_project: 単一プロジェクトの詳細取得（メディア・コンポジションを含む）\n"
            "- import_media: メディアファイル/URL の取り込み\n"
            "- agent_edit: Underlord エージェントによる編集の実行\n"
            "- publish: 書き出し・共有リンクの生成\n"
            "- job_status: 非同期ジョブの完了待ち・結果取得\n"
            "- export_timeline: 編集タイムラインの XML 書き出し（Final Cut Pro / Premiere 等）\n"
            "- report_upload_status: 直接アップロードの成否を取込ジョブに通知\n\n"
            f"# ツール一覧\n{json.dumps(catalog, ensure_ascii=False)}"
        )

        res = await generate_chat_completion(
            ctx["request"],
            {
                "model": model_id,
                "messages": [{"role": "user", "content": instruction}],
                "stream": False,
                "metadata": self._llm_metadata(ctx),
            },
            ctx["user"],
            bypass_filter=True,
        )
        parsed = json.loads(_strip_code_fence(_extract_llm_text(res)))
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str) and v}

    # -- LLM 呼び出しの共通部品 --------------------------------------------

    def _resolve_llm_model(self, ctx: dict) -> str:
        """編集プロンプト合成に使うモデル ID を決める。

        本 Pipe 自身（descript_pipe）を指定すると無限再帰になるため必ず除外する。
        優先順: editing_model_id Valve → body['model'] → Open WebUI のタスクモデル。
        """
        candidates = [
            str(self.valves.editing_model_id or "").strip(),
            str((ctx.get("body") or {}).get("model") or "").strip(),
        ]

        request = ctx.get("request")
        config = getattr(getattr(request, "app", None), "state", None)
        config = getattr(config, "config", None)
        for attr in ("TASK_MODEL_EXTERNAL", "TASK_MODEL"):
            try:
                candidates.append(str(getattr(config, attr, "") or "").strip())
            except Exception:
                candidates.append("")

        for candidate in candidates:
            if candidate and candidate.split(".", 1)[0] != _FUNCTION_ID:
                return candidate

        raise DescriptError(
            "INTERNAL",
            "編集プロンプト生成に使う LLM モデルが設定されていません。",
            "Valves の editing_model_id に OpenAI 等のモデル ID（例: gpt-5.4）を設定してください。",
            {"tried": candidates},
        )

    @staticmethod
    def _llm_metadata(ctx: dict) -> dict:
        """入力 __metadata__ を引き継ぐ。message_id は必ず同一のものを再利用する。

        files / tool_ids は空にする。合成用の LLM 呼び出しに動画ファイルや
        ツールを引き回す必要がなく、巨大メディアを再送する事故を防ぐため。
        """
        metadata = dict(ctx.get("metadata") or {})
        metadata.update(
            {
                "chat_id": ctx.get("chat_id"),
                "session_id": ctx.get("session_id"),
                "message_id": ctx.get("message_id"),
                "task": None,
                "task_body": None,
                "files": [],
                "tool_ids": [],
            }
        )
        return metadata

    # -- 操作のハンドリング -------------------------------------------------

    async def _handle(self, op, args, ctx, client, specs, tool_map, arg_maps, server_id) -> str:
        if op == "probe":
            return await self._op_probe(ctx, specs, tool_map, server_id)
        if op == "list_projects":
            return await self._op_list_projects(args, ctx, client, specs, tool_map, arg_maps)
        if op == "get_project":
            return await self._op_get_project(args, ctx, client, specs, tool_map, arg_maps)
        if op == "ensure_project":
            return await self._op_ensure_project(args, ctx, client, specs, tool_map, arg_maps)
        if op == "import_media":
            return await self._op_import_media(args, ctx, client, specs, tool_map, arg_maps)
        if op == "agent_edit":
            return await self._op_agent_edit(args, ctx, client, specs, tool_map, arg_maps)
        if op == "publish":
            return await self._op_publish(args, ctx, client, specs, tool_map, arg_maps)
        if op == "job_status":
            return await self._op_job_status(args, ctx, client, specs, tool_map, arg_maps)
        if op == "export_timeline":
            return await self._op_export_timeline(args, ctx, client, specs, tool_map, arg_maps)
        if op == "start_edit_loop":
            return await self._op_start_edit_loop(args, ctx, client, specs, tool_map, arg_maps)
        if op == "resume_edit_loop":
            return await self._op_resume_edit_loop(args, ctx, client, specs, tool_map, arg_maps)

        raise DescriptError(
            "INTERNAL",
            f"未知の操作 '{op}' が指定されました。",
            "descript_op の値をご確認ください。",
            {"op": op},
        )

    async def _op_probe(self, ctx, specs, tool_map, server_id) -> str:
        """依存ゼロの疎通確認。MCP 接続とツール名確定の唯一の手段。"""
        data = {
            "tools": list(specs.values()),
            "resolved": {op: tool_map.get(op) for op in _LOGICAL_OPS_EXT},
            "server": {"id": server_id, "tool_count": len(specs)},
        }
        await ctx["emit_status"](f"Descript MCP に接続できました（ツール {len(specs)} 件）", done=True)
        return _ok("probe", data, ctx.get("state"))

    async def _op_list_projects(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        projects = await self._list_projects(ctx, client, specs, tool_map, arg_maps)
        name_filter = str(args.get("name") or "").strip().lower()
        if name_filter:
            projects = [p for p in projects if name_filter in str(
                p.get("name") or "").lower()]
        await ctx["emit_status"](f"プロジェクトを {len(projects)} 件取得しました", done=True)
        return _ok("list_projects", {"projects": projects}, ctx.get("state"))

    async def _op_get_project(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        project_id = str(args.get("project_id") or "").strip()
        if not project_id:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "project_id が指定されていません。",
                "先にプロジェクトを選択してください。",
                {"missing": ["project_id"]},
            )
        await ctx["emit_status"]("プロジェクト情報を取得しています")
        raw = await _mcp_call(client, specs, tool_map, "get_project", {"project_id": project_id}, arg_maps)
        project = raw if isinstance(raw, dict) else {
            "id": project_id, "raw": raw}
        await ctx["emit_status"]("プロジェクト情報を取得しました", done=True)
        return _ok("get_project", {"project": project}, ctx.get("state"))

    async def _op_ensure_project(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "プロジェクト名が指定されていません。",
                "",
                {"missing": ["name"]},
            )
        project_id, created = await self._ensure_project(name, ctx, client, specs, tool_map, arg_maps)
        state = await _save_state(
            ctx.get("chat_id"), {
                "project_id": project_id, "project_name": name}
        )
        await ctx["emit_status"](f"プロジェクト『{name}』を確定しました", done=True)
        return _ok(
            "ensure_project",
            {"project_id": project_id, "project_name": name, "created": created},
            state,
        )

    async def _op_import_media(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        project_id = str(args.get("project_id") or "").strip()
        project_name = str(args.get("project_name") or "").strip()

        if not project_id and project_name:
            project_id, _ = await self._ensure_project(
                project_name, ctx, client, specs, tool_map, arg_maps
            )
        if not project_id:
            project_id = str((ctx.get("state") or {}).get(
                "project_id") or "").strip()

        entries = []
        if args.get("source_url"):
            entries.append(
                {"name": args.get("name") or args.get(
                    "media_name"), "url": args["source_url"]}
            )
        if args.get("file_id"):
            entries.append(
                {
                    "name": args.get("name") or args.get("media_name"),
                    "file_id": args["file_id"],
                    "content_type": args.get("content_type"),
                }
            )
        if not entries:
            # 通常チャット経路のフォールバック: Filter が退避した添付を使う（契約書 §1.4）
            entries = list(ctx.get("media") or [])
        if not entries:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "取り込むメディア（source_url または file_id）が指定されていません。",
                "動画の URL、または Open WebUI にアップロード済みのファイル ID を指定してください。",
                {"missing": ["source_url|file_id"]},
            )

        if not project_id and not project_name:
            # 通常チャットのインテント経路（契約書 §1.6）では名前が渡ってこない。
            # 無名で作らせず、最初のメディア名を使う（_import_attached_media と同じ規則）。
            first = entries[0] if isinstance(entries[0], dict) else {}
            project_name = str(first.get("name") or "").strip()

        await ctx["emit_status"]("Descript にメディアを取り込んでいます")
        outcome = await self._run_import_media(
            ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            project_name=project_name,
            entries=entries,
        )
        result = outcome["result"] if isinstance(
            outcome["result"], dict) else {}
        new_project_id = str(
            _pick(result, "project_id", "projectId",
                  default=project_id) or project_id or ""
        )
        project_url = _pick(result, "project_url",
                            "projectUrl", "app_url", "edit_url")

        patch = {"project_id": new_project_id,
                 "last_job_id": outcome.get("job_id")}
        if project_name:
            patch["project_name"] = project_name
        if project_url:
            patch["project_url"] = project_url
        state = await _save_state(ctx.get("chat_id"), patch)
        await _append_history(
            ctx.get("chat_id"),
            {
                "op": "import_media",
                "job": outcome.get("job_id"),
                "result": "partial" if outcome.get("partial") else "success",
                "note": project_name or new_project_id,
            },
        )
        await ctx["emit_status"]("メディアの取り込みが完了しました", done=True)

        data = {"project_id": new_project_id,
                "project_url": project_url, "media": result}
        if outcome.get("partial"):
            data["warning_code"] = "JOB_PARTIAL"
            data["warning_ja"] = "一部のメディアの処理に失敗しました。"
        return _ok("import_media", data, state)

    async def _op_agent_edit(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        project_id = str(args.get("project_id") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        if not project_id or not prompt:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "project_id または prompt が指定されていません。",
                "",
                {"missing": [k for k, v in (
                    ("project_id", project_id), ("prompt", prompt)) if not v]},
            )

        await ctx["emit_status"]("Descript Underlord が編集しています")
        edit = await self._run_agent_edit(
            ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            composition_id=args.get("composition_id"),
            prompt=prompt,
            conversation_id=args.get("conversation_id"),
        )
        state = await _save_state(
            ctx.get("chat_id"),
            {
                "project_id": project_id,
                "conversation_id": edit.get("conversation_id"),
                "last_job_id": edit.get("job_id"),
                "edit_prompt": prompt,
            },
        )
        await ctx["emit_status"]("編集が完了しました", done=True)
        return _ok(
            "agent_edit",
            {
                "agent_response": edit.get("agent_response"),
                "project_changed": edit.get("project_changed"),
                "conversation_id": edit.get("conversation_id"),
            },
            state,
        )

    async def _op_publish(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        project_id = str(args.get("project_id") or "").strip() or str(
            (ctx.get("state") or {}).get("project_id") or ""
        ).strip()
        if not project_id:
            raise DescriptError(
                "TOOL_ARGS_MISSING", "project_id が指定されていません。", "", {
                    "missing": ["project_id"]}
            )

        await ctx["emit_status"]("書き出しています")
        published = await self._run_publish(
            ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            composition_id=args.get("composition_id"),
            media_type=args.get("media_type"),
            resolution=args.get("resolution"),
        )
        state = await self._save_publish_state(ctx, project_id, published)
        await ctx["emit_status"]("書き出しが完了しました", done=True)

        if self.valves.embed_player and (published.get("download_url") or published.get("share_url")):
            await _emit_embeds(ctx["emitter"], self._render_player(ctx, published, state))

        return _ok("publish", self._publish_data(published), state)

    async def _op_job_status(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        job_id = str(args.get("job_id") or "").strip() or str(
            (ctx.get("state") or {}).get("last_job_id") or ""
        ).strip()
        if not job_id:
            raise DescriptError(
                "TOOL_ARGS_MISSING", "job_id が指定されていません。", "", {
                    "missing": ["job_id"]}
            )
        raw = await _mcp_call(client, specs, tool_map, "job_status", {"job_id": job_id}, arg_maps)
        job_state = str(_pick(raw, "job_state", "state",
                        "status", default="") or "")
        result = _pick(raw, "result", default={}) or {}
        await ctx["emit_status"](f"ジョブ {job_id[:8]} の状態: {job_state or '不明'}", done=True)
        return _ok("job_status", {"job_state": job_state, "result": result}, ctx.get("state"))

    async def _op_export_timeline(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        """編集タイムラインを FCPXML 等で書き出す（契約書 §2.0.1 ④ / UC3）。

        非同期。完了結果の download_url は期限付きなので state には保存しない。
        メディアファイルは同梱されず、タイムライン/XML ファイルのみが出力される。
        """
        state = ctx.get("state") or {}
        project_id = str(args.get("project_id") or "").strip() or str(
            state.get("project_id") or ""
        ).strip()
        if not project_id:
            project_id = await self._infer_project_id(ctx, client, specs, tool_map, arg_maps)

        fmt = str(args.get("format")
                  or self.valves.default_timeline_format or "fcp").strip().lower()
        if fmt not in _TIMELINE_FORMATS:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                f"タイムラインの書き出し形式 '{fmt}' は指定できません。",
                f"指定できるのは {' / '.join(_TIMELINE_FORMATS)} です。",
                {"format": fmt, "allowed": list(_TIMELINE_FORMATS)},
            )

        await ctx["emit_status"]("タイムラインを書き出しています")
        outcome = await self._call_job(
            ctx, client, specs, tool_map, arg_maps,
            "export_timeline",
            {
                "project_id": project_id,
                "composition_id": args.get("composition_id") or state.get("composition_id") or None,
                "format": fmt,
            },
        )
        result = outcome["result"] if isinstance(
            outcome["result"], dict) else {}
        download_url = _pick(result, "download_url",
                             "downloadUrl", "url", "file_url")
        expires_at = _pick(
            result, "download_url_expires_at", "downloadUrlExpiresAt", "expires_at", "expiresAt"
        )
        if not download_url:
            raise DescriptError(
                "JOB_FAILED",
                "タイムラインのダウンロード URL を取得できませんでした。",
                "Descript 側で書き出しが完了したかご確認ください。",
                {"job_id": outcome.get("job_id"), "result": result},
            )

        note_ja = (
            "タイムライン（XML）ファイルのみが書き出されます。メディアファイルは同梱されません。"
            "ダウンロード URL は期限付きです。"
        )
        # download_url は期限付きなので state には保存しない（契約書 §11）
        new_state = await _save_state(
            ctx.get("chat_id"),
            {"project_id": project_id, "last_job_id": outcome.get("job_id")},
        )
        await _append_history(
            ctx.get("chat_id"),
            {
                "op": "export_timeline",
                "job": outcome.get("job_id"),
                "result": "partial" if outcome.get("partial") else "success",
                "note": fmt,
            },
        )
        await ctx["emit_status"](f"タイムラインを書き出しました（{fmt}）。{note_ja}", done=True)

        data = {
            "download_url": str(download_url),
            "expires_at": str(expires_at) if expires_at else None,
            "format": fmt,
            "note_ja": note_ja,
        }
        if outcome.get("partial"):
            data["warning_code"] = "JOB_PARTIAL"
            data["warning_ja"] = "一部の処理に失敗しました。"
        return _ok("export_timeline", data, new_state)

    # -- 編集ループ ---------------------------------------------------------

    async def _op_start_edit_loop(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        state = ctx.get("state") or {}
        project_id = str(args.get("project_id") or "").strip()
        if not project_id and not str(state.get("project_id") or "").strip() and ctx.get("media"):
            # 動画を添付して送った通常チャット経路。編集に入る前に取り込む（契約書 §1.4）
            project_id = await self._import_attached_media(ctx, client, specs, tool_map, arg_maps)
            state = ctx.get("state") or state
        if not project_id:
            project_id = await self._infer_project_id(ctx, client, specs, tool_map, arg_maps)

        user_valves = ctx["user_valves"]
        style = str(args.get("style")
                    or user_valves.editing_style or "jet_cut").strip()
        aspect = str(
            args.get("aspect") or _aspect_from_resolution(
                self.valves.default_resolution)
        ).strip()
        try:
            duration_sec = int(args.get("duration_sec")
                               or state.get("duration_sec") or 60)
        except Exception:
            duration_sec = 60
        instruction = str(args.get("instruction") or "").strip()
        auto_confirm = bool(args.get("auto_confirm"))

        return await self._edit_loop_once(
            "start_edit_loop", ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            instruction=instruction,
            style=style,
            aspect=aspect,
            duration_sec=duration_sec,
            auto_confirm=auto_confirm,
            conversation_id=state.get("conversation_id"),
            revision=int(state.get("revision") or 0),
        )

    async def _op_resume_edit_loop(self, args, ctx, client, specs, tool_map, arg_maps) -> str:
        state = ctx.get("state") or {}
        project_id = str(state.get("project_id") or "").strip()
        if not project_id:
            project_id = await self._infer_project_id(ctx, client, specs, tool_map, arg_maps)

        action = str(args.get("action") or args.get(
            "op") or "revise").strip().lower()
        text = str(args.get("text") or "").strip()
        style = str(state.get("style")
                    or ctx["user_valves"].editing_style or "jet_cut")
        aspect = str(state.get("aspect") or _aspect_from_resolution(
            self.valves.default_resolution))
        try:
            duration_sec = int(state.get("duration_sec") or 60)
        except Exception:
            duration_sec = 60
        revision = int(state.get("revision") or 0)

        if action == "confirm":
            return await self._finalize_edit_loop(
                ctx, client, specs, tool_map, arg_maps,
                project_id=project_id, revision=revision, state=state,
            )

        instruction = text or str(state.get("instruction") or "")
        return await self._edit_loop_once(
            "resume_edit_loop", ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            instruction=instruction,
            style=style,
            aspect=aspect,
            duration_sec=duration_sec,
            auto_confirm=False,
            conversation_id=state.get("conversation_id"),
            revision=revision,
        )

    async def _edit_loop_once(
        self, op, ctx, client, specs, tool_map, arg_maps, *,
        project_id, instruction, style, aspect, duration_sec,
        auto_confirm, conversation_id, revision,
    ) -> str:
        """編集ループの 1 周。

        プロンプト合成 → agent_edit → publish → プレビュー表示 → state 保存。
        __event_call__ を開いたまま待たず、embeds フォームを出して 1 周を終える
        （契約書 §7 の 300 秒タイムアウト回避）。
        """
        user_valves = ctx["user_valves"]
        emit_status = ctx["emit_status"]

        # 1) 編集プロンプトの合成
        await emit_status("編集プロンプトを組み立てています")
        hints = self._build_feature_hints(user_valves, style)
        edit_prompt = await self._compose_edit_prompt(
            ctx,
            instruction=instruction,
            hints=hints,
            style=style,
            aspect=aspect,
            duration_sec=duration_sec,
            caption_language=user_valves.caption_language,
        )

        # 2) Underlord 編集
        await emit_status("Descript Underlord が編集しています")
        edit = await self._run_agent_edit(
            ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            composition_id=(ctx.get("state") or {}).get("composition_id"),
            prompt=edit_prompt,
            conversation_id=conversation_id,
        )

        # 3) 書き出し
        await emit_status("プレビュー用に書き出しています")
        published = await self._run_publish(
            ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            composition_id=edit.get("composition_id") or (
                ctx.get("state") or {}).get("composition_id"),
            media_type=None,
            resolution=None,
        )

        next_revision = revision + 1
        remaining = max(
            0, int(self.valves.max_edit_iterations) - next_revision)
        awaiting = "done" if (auto_confirm or remaining <= 0) else "user"

        # 4) status(done) を先に出してからフォームを出す（契約書 §7 の順序原則）
        await emit_status(
            "編集が完了しました。プレビューをご確認ください。" if awaiting == "user" else "編集を確定しました。",
            done=True,
        )

        # 5) state 保存
        patch = {
            "project_id": project_id,
            "composition_id": published.get("composition_id") or edit.get("composition_id"),
            "conversation_id": edit.get("conversation_id") or conversation_id,
            "last_job_id": published.get("job_id") or edit.get("job_id"),
            "share_url": published.get("share_url"),
            "revision": next_revision,
            "instruction": instruction,
            "edit_prompt": edit_prompt,
            "awaiting": awaiting,
            "style": style,
            "aspect": aspect,
            "duration_sec": duration_sec,
        }
        if published.get("project_url"):
            patch["project_url"] = published["project_url"]
        # download_url は署名付き・期限付きなので state には保存しない（契約書 §11）
        state = await _save_state(ctx.get("chat_id"), patch)
        await _append_history(
            ctx.get("chat_id"),
            {
                "op": "agent_edit",
                "job": published.get("job_id") or edit.get("job_id"),
                "result": "partial" if (edit.get("partial") or published.get("partial")) else "success",
                "note": (instruction or "")[:120],
            },
        )

        # 6) プレビュー表示
        agent_response = str(edit.get("agent_response") or "")
        if self.valves.redact_download_url:
            agent_response = _redact_signed_urls(agent_response)

        if awaiting == "user":
            await _emit_embeds(
                ctx["emitter"],
                _render(
                    _EDIT_LOOP_FORM,
                    css=_BASE_CSS,
                    height_js=_HEIGHT_JS,
                    title=_esc(f"プレビュー（rev {next_revision}）"),
                    src=_esc(published.get("download_url")
                             or published.get("share_url") or ""),
                    agent_response=_esc(agent_response or "編集が完了しました。"),
                    app_url=_esc(self._app_url(published, state)),
                    revision=next_revision,
                    remaining=remaining,
                ),
            )
        elif self.valves.embed_player:
            await _emit_embeds(ctx["emitter"], self._render_player(ctx, published, state, next_revision))

        data = {
            "revision": next_revision,
            "share_url": published.get("share_url"),
            "agent_response": agent_response,
            "awaiting": awaiting,
        }
        if not self.valves.redact_download_url and published.get("download_url"):
            data["download_url"] = published["download_url"]
        if edit.get("partial") or published.get("partial"):
            data["warning_code"] = "JOB_PARTIAL"
            data["warning_ja"] = "一部のメディアの処理に失敗しました。"
        return _ok(op, data, state)

    async def _finalize_edit_loop(
        self, ctx, client, specs, tool_map, arg_maps, *, project_id, revision, state
    ) -> str:
        """ユーザが「これで確定する」を押したときの締め。最終版を書き出して終える。"""
        await ctx["emit_status"]("確定版を書き出しています")
        published = await self._run_publish(
            ctx, client, specs, tool_map, arg_maps,
            project_id=project_id,
            composition_id=state.get("composition_id"),
            media_type=None,
            resolution=None,
        )
        await ctx["emit_status"]("確定しました。", done=True)

        new_state = await self._save_publish_state(ctx, project_id, published, awaiting="done")
        await _append_history(
            ctx.get("chat_id"),
            {
                "op": "publish",
                "job": published.get("job_id"),
                "result": "partial" if published.get("partial") else "success",
                "note": "confirmed",
            },
        )

        if self.valves.embed_player:
            await _emit_embeds(ctx["emitter"], self._render_player(ctx, published, new_state, revision))

        data = {
            "revision": revision,
            "share_url": published.get("share_url"),
            "agent_response": "編集を確定しました。",
            "awaiting": "done",
        }
        if not self.valves.redact_download_url and published.get("download_url"):
            data["download_url"] = published["download_url"]
        return _ok("resume_edit_loop", data, new_state)

    # -- 編集プロンプトの合成 -----------------------------------------------

    @staticmethod
    def _build_feature_hints(user_valves, style: str) -> list:
        """BA 要件の編集機能を Underlord 向けの自然文（英語）に展開する。

        docs/RequirementDefinition/BA/business_process_architecture.mmd の
        ジェットカッティング / オートキャプショニング＆音声処理 / ダイナミックエフェクト
        を UserValves のトグルに対応させる。
        """
        hints = []
        if user_valves.enable_silence_cut:
            hints.append("remove silent gaps between sentences")
        if user_valves.enable_filler_removal:
            hints.append(
                "remove filler words (um, uh, えーっと, あの) while keeping natural flow")
        if user_valves.enable_studio_sound:
            hints.append(
                "apply Studio Sound to reduce background noise and normalize loudness")
        if user_valves.enable_auto_captions:
            hints.append(
                f"add captions in {user_valves.caption_language} synced to the transcript"
            )
        if user_valves.enable_pan_zoom:
            hints.append("add subtle auto pan and zoom on emphasis moments")
        if user_valves.enable_broll:
            hints.append("suggest and place b-roll inserts at topic changes")

        # スタイルのプリセットで強調点を足す
        preset = {
            "jet_cut": [
                "tighten the timeline aggressively so the pacing feels like a jet cut",
                "keep only the segments that carry the main message",
            ],
            "caption_focus": [
                "make the captions the primary visual element and keep them readable on mobile",
                "align caption timing tightly to the spoken words",
            ],
            "dynamic_effects": [
                "vary the framing with pan and zoom so it feels like a multi-camera edit",
                "place inserts at topic changes to keep the viewer engaged",
            ],
            "full": [
                "tighten the timeline, keep captions readable, and vary the framing",
                "prioritize a clean, publish-ready short video",
            ],
        }.get(style, [])
        hints.extend(preset)
        return hints

    async def _compose_edit_prompt(
        self, ctx, *, instruction, hints, style, aspect, duration_sec, caption_language
    ) -> str:
        """システムプロンプト＋ユーザ指示＋機能フラグから最終編集プロンプトを合成する。"""
        system_prompt = ""
        command = str(self.valves.system_prompt_command or "").strip()
        if command:
            try:
                prompt_model = await Prompts.get_prompt_by_command(command)
            except Exception:
                prompt_model = None
            if prompt_model is not None:
                system_prompt = str(getattr(prompt_model, "content", "") or "")
        if not system_prompt.strip():
            system_prompt = str(
                self.valves.system_prompt_fallback or _DEFAULT_SYSTEM_PROMPT)

        feature_lines = "\n".join(f"- {h}" for h in hints) or "- (指定なし)"
        user_message = (
            "# ユーザの編集指示（原文・日本語）\n"
            f"{instruction or '(指示なし。有効な編集機能のみを適用してください)'}\n\n"
            "# 有効な編集機能\n"
            f"{feature_lines}\n\n"
            "# 出力条件\n"
            f"- ターゲット尺: {duration_sec} 秒\n"
            f"- アスペクト比: {aspect}\n"
            f"- 編集スタイル: {style}\n"
            f"- 字幕言語: {caption_language}\n"
        )

        try:
            model_id = self._resolve_llm_model(ctx)
            res = await generate_chat_completion(
                ctx["request"],
                {
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "stream": False,
                    "metadata": self._llm_metadata(ctx),
                },
                ctx["user"],
                bypass_filter=True,
            )
            composed = _strip_code_fence(_extract_llm_text(res)).strip()
        except DescriptError:
            raise
        except Exception:
            composed = ""

        if composed:
            return composed
        # LLM が使えなかった場合でも編集は続行できるよう、機能フラグから決定的に組み立てる
        return self._fallback_prompt(instruction, hints, aspect, duration_sec)

    @staticmethod
    def _fallback_prompt(instruction: str, hints: list, aspect: str, duration_sec: int) -> str:
        parts = list(hints)
        parts.append(f"target a final length of about {duration_sec} seconds")
        parts.append(f"format the result for a {aspect} vertical short video")
        body = "; ".join(parts)
        tail = f" Additional request from the user (Japanese): {instruction}" if instruction else ""
        return f"Edit this project as a short-form video: {body}.{tail}"

    # -- MCP 呼び出しのラッパ -----------------------------------------------

    async def _call_job(self, ctx, client, specs, tool_map, arg_maps, op, args) -> dict:
        """ツールを呼び、非同期ジョブが返ったら完了まで追跡する。

        Descript は queued -> running -> stopped。stopped は成功ではないので
        _poll_job が result.status を見て判定する。
        """
        raw = await _mcp_call(client, specs, tool_map, op, args, arg_maps)
        return await self._track_job(ctx, client, specs, tool_map, arg_maps, raw)

    async def _track_job(self, ctx, client, specs, tool_map, arg_maps, raw) -> dict:
        """ツールの戻り値にジョブ ID があれば完了まで追跡し、無ければそのまま返す。"""
        if not isinstance(raw, dict):
            return {"job_id": None, "result": {}, "partial": False, "raw": raw}

        job_id = _extract_job_id(raw)
        if job_id is None:
            # ジョブを作らず同期に結果を返す実装
            return {"job_id": None, "result": _pick(raw, "result", default=raw) or raw, "partial": False, "raw": raw}

        polled = await _poll_job(
            client, specs, tool_map, arg_maps, job_id, ctx["emit_status"], self.valves
        )
        return {
            "job_id": job_id,
            "result": polled.get("result") or {},
            "partial": bool(polled.get("partial")),
            "raw": raw,
        }

    # -- メディア取込（契約書 §2.0.1 ①）------------------------------------

    async def _run_import_media(
        self, ctx, client, specs, tool_map, arg_maps, *, project_id, project_name, entries
    ) -> dict:
        """add_media マップを組み立てて import_media を呼ぶ。

        直接アップロード経路（file_id 指定）では、レスポンスの upload_urls に
        実体を PUT してからジョブの完了を待つ。PUT を済ませないと import ジョブが
        そのファイルを待ち続ける。
        """
        prepared = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("url"):
                prepared.append(
                    {
                        "name": entry.get("name"),
                        "url": str(entry["url"]),
                        "content_type": entry.get("content_type"),
                    }
                )
                continue
            if entry.get("file_id"):
                prepared.append(await self._resolve_stored_file(entry))
        if not prepared:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "取り込めるメディアがありませんでした。",
                "動画の URL、または Open WebUI にアップロード済みのファイルを指定してください。",
                {"missing": ["source_url|file_id"]},
            )

        add_media, uploads = _build_add_media(prepared)
        args = {
            "add_media": add_media,
            "project_id": project_id or None,
            "project_name": project_name or None,
        }
        # 新規プロジェクトには add_compositions を渡す。渡さないと取り込んだメディアが
        # タイムラインに載らない。既存プロジェクトには渡さない（既存の編集が壊れる）。
        if not project_id:
            args["add_compositions"] = [
                {"clips": [{"media": key} for key in add_media]}]

        _log_info(
            "import.request",
            chat_id=ctx.get("chat_id"),
            project_id=project_id or "(new)",
            media=sorted(add_media.keys()),
            direct_uploads=sorted(uploads.keys()),
            compositions="add_compositions" in args,
        )

        raw = await _mcp_call(client, specs, tool_map, "import_media", args, arg_maps)
        job_id = _extract_job_id(raw)
        if uploads:
            await self._upload_media(
                ctx, client, specs, tool_map, arg_maps, raw=raw, job_id=job_id, uploads=uploads
            )
        return await self._track_job(ctx, client, specs, tool_map, arg_maps, raw)

    @staticmethod
    async def _resolve_stored_file(entry: dict) -> dict:
        """Open WebUI に保存済みのファイル（file_id）の実体パス・サイズを解決する。

        Files.get_file_by_id で DB レコードを引き（backend/open_webui/models/files.py:153）、
        Storage.get_file で実体をローカルパスに解決する
        （backend/open_webui/storage/provider.py:70 / 162）。
        STORAGE_PROVIDER=s3（MinIO）でも S3StorageProvider.get_file が UPLOAD_DIR に
        ダウンロードしてそのパスを返すため同じ手順で読める（provider.py:162-170）。
        boto3 は同期なので upstream と同じく asyncio.to_thread に逃がす
        （backend/open_webui/routers/files.py:799）。
        """
        file_id = str(entry.get("file_id") or "")
        try:
            record = await Files.get_file_by_id(file_id)
        except Exception:
            record = None
        if record is None or not record.path:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "添付ファイルの実体を取得できませんでした。",
                "ファイルを添付し直すか、動画の URL を指定してください。",
                {"file_id": file_id},
            )

        try:
            path = await asyncio.to_thread(Storage.get_file, record.path)
            file_size = int(await asyncio.to_thread(os.path.getsize, path))
        except Exception as exc:
            raise DescriptError(
                "TOOL_ARGS_MISSING",
                "添付ファイルの実体を読み出せませんでした。",
                "動画の URL を指定して取り込んでください。",
                {"file_id": file_id, "raw": str(exc)[:800]},
            ) from exc

        meta = record.meta if isinstance(record.meta, dict) else {}
        resolved = {
            "name": entry.get("name") or meta.get("name") or record.filename,
            "path": path,
            "file_size": file_size,
            "content_type": entry.get("content_type")
            or meta.get("content_type")
            or "application/octet-stream",
        }
        # content_type が octet-stream に落ちていると Descript 側で弾かれうるので、
        # 実体のパスではなく「何をどう判定したか」を残す。
        _log_info(
            "file.resolved",
            file_id=file_id,
            name=resolved["name"],
            bytes=file_size,
            content_type=resolved["content_type"],
        )
        return resolved

    async def _upload_media(
        self, ctx, client, specs, tool_map, arg_maps, *, raw, job_id, uploads
    ) -> None:
        """import_media が返した upload_urls に実体を PUT する。

        upload_urls は {メディアキー: {upload_url, asset_id, artifact_id}}。
        Content-Type: application/octet-stream で送り、宣言した file_size と
        実サイズを一致させる必要がある（契約書 §2.0.1 ①）。
        失敗時は report_upload_status で取込ジョブに通知し、待ち続けさせない。
        """
        upload_urls = _pick(raw, "upload_urls", "uploadUrls", default={}) or {}
        if not isinstance(upload_urls, dict):
            upload_urls = {}

        # ここが噛み合わないと「アップロード先 URL が返りませんでした」になる。
        # 送ったキーと返ってきたキーを必ず突き合わせられるようにしておく。
        _log_info(
            "import.upload_urls",
            job_id=job_id,
            expected=sorted(uploads.keys()),
            returned=sorted(str(k) for k in upload_urls.keys()),
            response_type=type(raw).__name__,
        )
        if not upload_urls:
            _log_warn("import.upload_urls_empty", job_id=job_id,
                      diag=_diag_response(raw, uploads.keys()))

        timeout = httpx.Timeout(
            float(self.valves.upload_timeout_sec), connect=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
            for key, entry in uploads.items():
                target = upload_urls.get(key)
                if not isinstance(target, dict):
                    raise DescriptError(
                        "JOB_FAILED",
                        f"'{key}' のアップロード先 URL が返りませんでした。",
                        "動画の URL を指定して取り込んでください。",
                        {
                            "media": key,
                            "job_id": job_id,
                            "stage": "upload_urls_missing",
                            "raw": _diag_response(raw, uploads.keys()),
                        },
                    )
                url = _pick(target, "upload_url", "uploadUrl", "url")
                media_id = _pick(target, "media_id", "mediaId",
                                 "asset_id", "assetId", "artifact_id")
                if not url:
                    raise DescriptError(
                        "JOB_FAILED",
                        f"'{key}' のアップロード先 URL の形式が想定と異なります。",
                        "動画の URL を指定して取り込んでください。",
                        {
                            "media": key,
                            "job_id": job_id,
                            "stage": "upload_url_field_missing",
                            "raw": _diag_response(target, uploads.keys()),
                        },
                    )

                await ctx["emit_status"](f"『{key}』をアップロードしています")
                file_size = int(entry.get("file_size") or 0)
                _log_info("upload.start", job_id=job_id,
                          media=key, bytes=file_size)
                put_started = time.monotonic()
                try:
                    response = await http.put(
                        str(url),
                        content=_file_chunks(str(entry.get("path"))),
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(file_size),
                        },
                    )
                    response.raise_for_status()
                    _log_info(
                        "upload.done",
                        job_id=job_id,
                        media=key,
                        bytes=file_size,
                        status=response.status_code,
                        ms=_ms(put_started),
                    )
                except Exception as exc:
                    _log_error(
                        "upload.failed",
                        job_id=job_id,
                        media=key,
                        bytes=file_size,
                        ms=_ms(put_started),
                        error=str(exc)[:400],
                    )
                    await self._report_upload_failure(
                        client, specs, tool_map, arg_maps, job_id, media_id
                    )
                    raise DescriptError(
                        "JOB_FAILED",
                        f"『{key}』のアップロードに失敗しました。",
                        "ファイルサイズが変化していないかご確認のうえ、再実行してください。",
                        {"media": key, "job_id": job_id, "raw": str(exc)[
                            :800]},
                    ) from exc

    @staticmethod
    async def _report_upload_failure(client, specs, tool_map, arg_maps, job_id, media_id) -> None:
        """アップロード失敗を取込ジョブに通知する。通知自体の失敗は握り潰す。"""
        if not job_id or not media_id:
            return
        try:
            await _mcp_call(
                client, specs, tool_map, "report_upload_status",
                {"job_id": str(job_id), "media_id": str(
                    media_id), "status": "failed"},
                arg_maps,
            )
        except Exception:
            pass

    async def _run_agent_edit(
        self, ctx, client, specs, tool_map, arg_maps, *, project_id, composition_id, prompt, conversation_id
    ) -> dict:
        outcome = await self._call_job(
            ctx, client, specs, tool_map, arg_maps,
            "agent_edit",
            {
                "project_id": project_id,
                "composition_id": composition_id or None,
                "prompt": prompt,
                "conversation_id": conversation_id or None,
            },
        )
        result = outcome["result"] if isinstance(
            outcome["result"], dict) else {}
        return {
            "job_id": outcome.get("job_id"),
            "partial": outcome.get("partial"),
            "agent_response": _pick(
                result, "agent_response", "response", "message", "summary", "text", default=""
            ),
            "project_changed": bool(_pick(result, "project_changed", "projectChanged", default=False)),
            "conversation_id": _pick(result, "conversation_id", "conversationId", "thread_id"),
            "composition_id": _pick(result, "composition_id", "compositionId"),
        }

    async def _run_publish(
        self, ctx, client, specs, tool_map, arg_maps, *, project_id, composition_id, media_type, resolution
    ) -> dict:
        """publish_project を呼ぶ。

        media_type は実測値 'Video' / 'Audio' に正規化する（契約書 §2.0.1 ②）。
        resolution は probe のツール説明に無いため渡さない（引数 resolution は
        呼び出し側の互換のために受けるだけで、MCP には送らない）。
        """
        wanted = _normalize_media_type(
            media_type or self.valves.default_media_type)
        args = {
            "project_id": project_id,
            "composition_id": composition_id or None,
            "media_type": wanted,
        }
        try:
            outcome = await self._call_job(
                ctx, client, specs, tool_map, arg_maps, "publish", args
            )
        except DescriptError as exc:
            # 'Video' を明示しかつ映像が無いと 422。その場合だけ Audio で 1 回再試行する。
            if wanted != "Video" or not _looks_like_no_video(exc):
                raise
            await ctx["emit_status"]("映像トラックが無いため音声として書き出します")
            outcome = await self._call_job(
                ctx, client, specs, tool_map, arg_maps, "publish", {
                    **args, "media_type": "Audio"}
            )

        published = _extract_publish(outcome.get("result"))
        published["job_id"] = outcome.get("job_id")
        published["partial"] = outcome.get("partial")
        return published

    async def _list_projects(self, ctx, client, specs, tool_map, arg_maps) -> list:
        raw = await _mcp_call(client, specs, tool_map, "list_projects", {}, arg_maps)
        return _as_project_list(raw)

    async def _ensure_project(self, name, ctx, client, specs, tool_map, arg_maps) -> tuple[str, bool]:
        """同名プロジェクトがあれば再利用、無ければ新規扱いにする。

        MCP に「プロジェクト作成」の独立ツールがあるかは未確定のため、
        存在しない場合は import_media 側で project_name を渡して作らせる前提とし、
        ここでは project_id を空のまま created=True を返す。
        """
        projects = await self._list_projects(ctx, client, specs, tool_map, arg_maps)
        target = name.strip().lower()
        for project in projects:
            if str(project.get("name") or "").strip().lower() == target:
                return str(project.get("id")), False
        return "", True

    async def _import_attached_media(self, ctx, client, specs, tool_map, arg_maps) -> str:
        """Filter が退避した添付動画（契約書 §1.4）を取り込み、project_id を返す。"""
        entries = list(ctx.get("media") or [])
        name = str((entries[0] if entries else {}).get("name") or "").strip()

        await ctx["emit_status"]("添付された動画を Descript に取り込んでいます")
        outcome = await self._run_import_media(
            ctx, client, specs, tool_map, arg_maps,
            project_id="",
            project_name=name,
            entries=entries,
        )
        result = outcome["result"] if isinstance(
            outcome["result"], dict) else {}
        project_id = str(_pick(result, "project_id",
                         "projectId", default="") or "")

        patch = {"project_id": project_id,
                 "last_job_id": outcome.get("job_id")}
        if name:
            patch["project_name"] = name
        project_url = _pick(result, "project_url",
                            "projectUrl", "app_url", "edit_url")
        if project_url:
            patch["project_url"] = project_url
        ctx["state"] = await _save_state(ctx.get("chat_id"), patch)
        await _append_history(
            ctx.get("chat_id"),
            {
                "op": "import_media",
                "job": outcome.get("job_id"),
                "result": "partial" if outcome.get("partial") else "success",
                "note": name,
            },
        )

        if not project_id:
            raise DescriptError(
                "JOB_FAILED",
                "取り込んだメディアのプロジェクト ID を取得できませんでした。",
                "Descript 側でプロジェクトが作成されたかご確認ください。",
                {"result": result},
            )
        return project_id

    async def _infer_project_id(self, ctx, client, specs, tool_map, arg_maps) -> str:
        """project_id が渡されなかったとき、state → 一覧の順で確定させる。"""
        state_project = str((ctx.get("state") or {}).get(
            "project_id") or "").strip()
        if state_project:
            return state_project

        projects = await self._list_projects(ctx, client, specs, tool_map, arg_maps)
        if not projects:
            raise DescriptError(
                "NO_PROJECT",
                "Descript にプロジェクトがありません。",
                "先に「動画のアップロード」で動画を取り込んでください。",
                {"projects": []},
            )
        if len(projects) == 1:
            return str(projects[0].get("id"))
        raise DescriptError(
            "PROJECT_AMBIGUOUS",
            "対象のプロジェクトを特定できませんでした。",
            "「動画の編集」アクションからプロジェクトを選択してください。",
            {"projects": projects[:50]},
        )

    # -- 表示・保存の小物 ---------------------------------------------------

    def _app_url(self, published: dict, state: Optional[dict] = None) -> str:
        """Descript App で開くための URL。

        URL 形式をコードに直書きしない（契約書 §11）ため、MCP が返した
        project_url / share_url のみを使う。
        """
        return str(
            published.get("project_url")
            or (state or {}).get("project_url")
            or published.get("share_url")
            or (state or {}).get("share_url")
            or ""
        )

    def _render_player(self, ctx, published: dict, state: dict, revision: Optional[int] = None) -> str:
        rev = revision if revision is not None else int(
            (state or {}).get("revision") or 0)
        note = "一部のメディアの処理に失敗しました" if published.get("partial") else "書き出し完了"
        return _render(
            _PLAYER,
            css=_BASE_CSS,
            height_js=_HEIGHT_JS,
            title=_esc(str((state or {}).get(
                "project_name") or "Descript プレビュー")),
            src=_esc(published.get("download_url")
                     or published.get("share_url") or ""),
            app_url=_esc(self._app_url(published, state)),
            share_url=_esc(published.get("share_url") or ""),
            revision=rev,
            note=_esc(note),
        )

    async def _save_publish_state(
        self, ctx, project_id: str, published: dict, awaiting: Optional[str] = None
    ) -> dict:
        patch = {
            "project_id": project_id,
            "share_url": published.get("share_url"),
            "last_job_id": published.get("job_id"),
        }
        if published.get("composition_id"):
            patch["composition_id"] = published["composition_id"]
        if published.get("project_url"):
            patch["project_url"] = published["project_url"]
        if awaiting is not None:
            patch["awaiting"] = awaiting
        # download_url は署名付き・期限付きなので state に残さない（契約書 §11）
        return await _save_state(ctx.get("chat_id"), patch)

    def _publish_data(self, published: dict) -> dict:
        data = {
            "share_url": published.get("share_url"),
            "composition_id": published.get("composition_id"),
        }
        if not self.valves.redact_download_url and published.get("download_url"):
            data["download_url"] = published["download_url"]
        if published.get("partial"):
            data["warning_code"] = "JOB_PARTIAL"
            data["warning_ja"] = "一部のメディアの処理に失敗しました。"
        return data
