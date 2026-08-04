"""
title: Descript Studio
author: A-clear
author_url: https://github.com/A-clear/short-video-creation
version: 0.1.0
license: MIT
description: ショート動画作成 - 入力フォーム収集と Descript オーケストレータの起動
required_open_webui_version: 0.11.0
requirements:
"""

# ---------------------------------------------------------------------------
# Descript Studio — Action Function（マルチアクション 4 サブ）
#
# 本ファイルは UI 層のみを担当する。MCP 接続・ジョブ・LLM・状態書き込みは
# すべて Pipe（descript_pipe）に委譲し、ここは
#   ① 入力フォームの収集  ② Pipe への RPC  ③ 結果の表示
# の 3 つしか行わない。
#
# 契約の正本: docs/DetailedDesign/functions_contract.md
# 1 Function = 自己完結した 1 ファイルのため、契約書 §5/§6 のヘルパは
# インライン展開している（相対 import は Open WebUI 側で解決されない）。
# ---------------------------------------------------------------------------

import json
import logging
import re
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from fastapi.responses import HTMLResponse

from open_webui.models.chats import Chats
from open_webui.models.users import UserModel
from open_webui.utils.chat import generate_chat_completion

# ===========================================================================
# 契約書 §5.1 定数・例外
# ===========================================================================

_STATE_KEY = "descript"
_STATE_VERSION = 1
_HISTORY_MAX = 50
_LOGICAL_OPS = ("list_projects", "get_project", "import_media", "agent_edit", "publish", "job_status")


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
    _LOGGER.log(level, "descript %s%s", event, (" " + " ".join(parts)) if parts else "")


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
        out = out.replace("{{" + key + "}}", "" if value is None else str(value))
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
# 契約書 §5.3 状態の読み取り
#
# 契約書 §3.3 のとおり state の書き手は Pipe のみ。Action は読み取り専用なので
# _save_state / _append_history はここには展開しない。
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


# ===========================================================================
# 契約書 §5.6 封筒の開け
# ===========================================================================


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
# 契約書 §5.7 __event_call__ の戻り値判定（Action 専用）
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
# 契約書 §6.0 HTML 共通フッタ
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

# 契約書 §6.0.1 embeds 直後の最下部ピン留め（3 ファイル共通展開）
_SCROLL_BOTTOM_JS = """
const TAG = '[descript:scroll]';
const el = document.getElementById('messages-container');
if (!el) {
  console.warn(TAG, 'no #messages-container');
} else {
  let stop = false;
  const abort = (e) => { stop = true; console.log(TAG, 'aborted by ' + e.type); };
  el.addEventListener('wheel', abort, { passive: true, once: true });
  el.addEventListener('touchstart', abort, { passive: true, once: true });
  const gap = () => el.scrollHeight - el.scrollTop - el.clientHeight;
  console.log(TAG, 'start gap=' + gap() + ' h=' + el.scrollHeight);
  const t0 = Date.now();
  let lastH = -1;
  let stable = 0;
  const iv = setInterval(() => {
    if (stop) { clearInterval(iv); return; }
    el.scrollTop = el.scrollHeight;
    if (el.scrollHeight === lastH) { stable += 1; } else { stable = 0; lastH = el.scrollHeight; }
    const over = Date.now() - t0 > 15000;
    if (stable >= 12 || over) {
      clearInterval(iv);
      el.scrollTop = el.scrollHeight;
      console.log(TAG, 'end gap=' + gap() + ' h=' + el.scrollHeight +
                  ' ms=' + (Date.now() - t0) + (over ? ' (timeout)' : ''));
    }
  }, 80);
}
"""


async def _emit_scroll_bottom(event_emitter) -> None:
    """embeds 直後にチャットを最下部へ寄せ直す（契約書 §6.0.1）。

    上流は embeds 受信の 100ms 後に scrollIntoView({block:'center'}) を撃つが
    （Chat.svelte:1012-1017）、その時点では iframe の高さが未確定。
    _HEIGHT_JS が load / 100ms / 600ms / ResizeObserver で高さを報告するたびに
    レイアウトが伸びるため、そのスクロール位置は最下部から離れていく。
    高さが落ち着くまで #messages-container を最下部へ寄せ直す。

    execute は emitter でも届く（socket/main.py:986 と :1112 は同じ events
    チャンネル）。応答は不要なので __event_call__ は使わない。
    """
    if event_emitter is None:
        return
    try:
        await event_emitter({"type": "execute", "data": {"code": _SCROLL_BOTTOM_JS}})
    except Exception:
        pass

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

# 契約書 §6.1 _PLAYER — publish 結果のプレビュー
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

# 契約書 §6.2 _TOOL_TABLE — probe / TOOL_UNRESOLVED のツール一覧
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

# 契約書 §6 に準じた追加テンプレート（プロジェクト内メディア一覧）。
# UC2 の「プロジェクトに含まれている動画の表示」用。契約書には未収録のため
# 採用時は §6.4 として正本に取り込むこと。
_MEDIA_TABLE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">{{title}}</p>
  <p class="muted">{{lead}}</p>
  <div class="scroll"><table>
    <thead><tr><th style="width:46%">メディア</th><th style="width:18%">種別</th><th>長さ / 補足</th></tr></thead>
    <tbody>{{rows}}</tbody>
  </table></div>
</div></div>{{height_js}}</body></html>"""

# 契約書 §6 に準じた追加テンプレート（export_timeline の結果カード）。
# UC3 のタイムライン書き出し用。契約書には未収録のため
# 採用時は §6.5 として正本に取り込むこと。
#
# download_url は署名付き・期限付き（契約書 §11）。この embeds の中だけに置き、
# 本文（messages）には載せない。
_TIMELINE_EXPORT = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">{{title}}</p>
  <p class="muted" style="margin:0 0 4px">形式: {{format_label}}</p>
  <div class="row">
    <a class="btn primary" href="{{download_url}}" target="_blank" rel="noopener">ダウンロード</a>
    {{app_link}}
  </div>
  <p class="muted" style="margin:12px 0 0">{{notice}}</p>
  {{expiry}}
</div></div>{{height_js}}</body></html>"""


# ===========================================================================
# 契約書 §4.1 MCP_OAUTH_REQUIRED の固定文言（そのまま使う）
# ===========================================================================

_OAUTH_REQUIRED_TEXT = (
    "Descript との連携が未認可です。チャット入力欄の ＋ ボタン → Integrations → Tools から\n"
    "Descript を一度有効化し、ブラウザに表示される同意画面を完了してください。\n"
    "（一度完了すれば、以降のトークン更新は自動で行われます）"
)

# 契約書 §4 の「リトライ」列に対応する通知レベル。表に無いコードは error 扱い。
_WARNING_CODES = ("JOB_PARTIAL", "JOB_TIMEOUT", "RATE_LIMITED")
_INFO_CODES = ("USER_CANCELLED",)


# ===========================================================================
# UC3 のエクスポート形式（契約書 §1.3 / §2.0.1 ②④）
#
# share_* は publish（共有リンクを作る）、それ以外は export_timeline
# （タイムライン/XML ファイルをダウンロードする）に分岐する。
# publish の media_type は "Video" / "Audio"（先頭大文字）でなければ通らない。
# export_timeline の value は Descript MCP の format 値そのもの。
# 先頭要素が select の既定表示になるため、順序に意味がある。
# ===========================================================================

_EXPORT_FORMATS = (
    {"value": "share_video", "label": "動画の共有リンク（Video）", "op": "publish", "media_type": "Video"},
    {"value": "share_audio", "label": "音声の共有リンク（Audio）", "op": "publish", "media_type": "Audio"},
    {"value": "fcp", "label": "Final Cut Pro X（.fcpxml）", "op": "export_timeline"},
    {"value": "premiere", "label": "Premiere Pro XML", "op": "export_timeline"},
    {"value": "davinci_resolve", "label": "DaVinci Resolve XML", "op": "export_timeline"},
    {"value": "aaf", "label": "Pro Tools / Logic（AAF）", "op": "export_timeline"},
    {"value": "edl", "label": "EDL（Samplitude / Reaper）", "op": "export_timeline"},
    {"value": "sesx", "label": "Adobe Audition（.sesx）", "op": "export_timeline"},
)

# UserValves の select に渡す値の一覧。_EXPORT_FORMATS から導出してドリフトを防ぐ。
_EXPORT_FORMAT_VALUES = [entry["value"] for entry in _EXPORT_FORMATS]
_DEFAULT_EXPORT_FORMAT = _EXPORT_FORMAT_VALUES[0]

# タイムライン書き出しの注記。ツール説明に明記されている事実で、
# 知らないと「動画が入っていない」と誤解される。
_TIMELINE_NOTICE = "メディアファイルは含まれません。タイムライン/XML ファイルのみです。"


def _export_format(value: Any) -> dict:
    """形式値からエントリを引く。未知の値は既定（先頭 = share_video）に落とす。"""
    wanted = str(value or "")
    for entry in _EXPORT_FORMATS:
        if entry["value"] == wanted:
            return entry
    return _EXPORT_FORMATS[0]


def _export_format_options(default_value: Any) -> list:
    """select の選択肢を組み立てる。既定値を先頭に寄せる。

    NativeSelect は value が未設定だと先頭を表示するため、順序自体が
    既定値の提示になる（value も併せて渡すが、二重の保険）。
    """
    default = _export_format(default_value)["value"]
    ordered = [e for e in _EXPORT_FORMATS if e["value"] == default]
    ordered += [e for e in _EXPORT_FORMATS if e["value"] != default]
    return [{"label": e["label"], "value": e["value"]} for e in ordered]


# ===========================================================================
# UC1 アップロード用カスタムモーダル（form_mode == "modal"）
#
# Chat.svelte:1102-1116 が `new Function('return (async () => { <code> })()')`
# としてメインページ origin で評価し、**その戻り値**を cb() に渡す。
# したがってこのコードは最後に return で値を返す（cb は参照できない）。
#
# 動画は Base64 で返さず /api/v1/files/ にアップロードして file id だけ返す。
# socket.io の ack サイズ制限に引っかかるため。
# `?process=false` は必須に近い: 既定の RAG 許可拡張子に mp4 等は含まれず、
# process=true だと拡張子チェックで 400 になる（routers/files.py:344-352）。
# ===========================================================================

_UPLOAD_MODAL_JS = r"""
const OVERLAY_ID = 'dsc-upload-overlay';
const STYLE_ID = 'dsc-upload-style';

const existing = document.getElementById(OVERLAY_ID);
if (existing) { existing.remove(); }

const css = [
  '#' + OVERLAY_ID + '{position:fixed;inset:0;z-index:2147483000;display:flex;',
  'align-items:center;justify-content:center;background:rgba(0,0,0,.55);',
  'font:14px/1.6 system-ui,-apple-system,"Segoe UI","Noto Sans JP",sans-serif}',
  '#' + OVERLAY_ID + ' .dsc-card{width:min(460px,92vw);max-height:88vh;overflow:auto;',
  'background:#fff;color:#1a1a1e;border:1px solid #e3e3e8;border-radius:16px;',
  'padding:20px;box-shadow:0 18px 48px rgba(0,0,0,.34)}',
  '#' + OVERLAY_ID + ' .dsc-h{font-weight:650;font-size:16px;margin:0 0 4px}',
  '#' + OVERLAY_ID + ' .dsc-muted{opacity:.62;font-size:12px;margin:0 0 14px}',
  '#' + OVERLAY_ID + ' label{display:block;font-size:12px;opacity:.8;margin:12px 0 4px}',
  '#' + OVERLAY_ID + ' input[type=text],#' + OVERLAY_ID + ' input[type=file]{width:100%;',
  'padding:9px 10px;border-radius:10px;border:1px solid #d9d9e0;background:#fff;',
  'color:inherit;font:inherit;font-size:13px}',
  '#' + OVERLAY_ID + ' .dsc-row{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}',
  '#' + OVERLAY_ID + ' .dsc-btn{padding:9px 18px;border-radius:10px;border:1px solid #d9d9e0;',
  'background:#f4f4f6;color:#1a1a1e;font:inherit;font-size:13px;cursor:pointer}',
  '#' + OVERLAY_ID + ' .dsc-btn:disabled{opacity:.5;cursor:default}',
  '#' + OVERLAY_ID + ' .dsc-btn.dsc-primary{background:#3b6fe0;border-color:#3b6fe0;color:#fff}',
  '#' + OVERLAY_ID + ' .dsc-bar{height:6px;border-radius:99px;background:#e8e8ee;overflow:hidden;',
  'margin-top:12px;display:none}',
  '#' + OVERLAY_ID + ' .dsc-bar > i{display:block;height:100%;width:0;background:#3b6fe0;',
  'transition:width .15s linear}',
  '#' + OVERLAY_ID + ' .dsc-msg{font-size:12px;margin-top:8px;min-height:16px}',
  '#' + OVERLAY_ID + ' .dsc-err{color:#d43b3b}',
  '@media (prefers-color-scheme: dark){',
  '#' + OVERLAY_ID + ' .dsc-card{background:#1c1c20;color:#e8e8ec;border-color:#303038}',
  '#' + OVERLAY_ID + ' input[type=text],#' + OVERLAY_ID + ' input[type=file]{background:#16161a;',
  'border-color:#3a3a44;color:#e8e8ec}',
  '#' + OVERLAY_ID + ' .dsc-btn{background:#2a2a31;border-color:#3a3a44;color:#e8e8ec}',
  '#' + OVERLAY_ID + ' .dsc-btn.dsc-primary{background:#3b6fe0;border-color:#3b6fe0;color:#fff}',
  '#' + OVERLAY_ID + ' .dsc-bar{background:#2c2c34}',
  '}'
].join('');

let styleEl = document.getElementById(STYLE_ID);
if (!styleEl) {
  styleEl = document.createElement('style');
  styleEl.id = STYLE_ID;
  document.head.appendChild(styleEl);
}
styleEl.textContent = css;

const overlay = document.createElement('div');
overlay.id = OVERLAY_ID;
// 静的マークアップのみ。ユーザ入力は差し込まない。
overlay.innerHTML = [
  '<div class="dsc-card" role="dialog" aria-modal="true">',
  '<p class="dsc-h">動画のアップロード</p>',
  '<p class="dsc-muted">Descript のプロジェクト名と、取り込む動画ファイルを指定してください。</p>',
  '<label for="dsc-name">プロジェクト名</label>',
  '<input id="dsc-name" type="text" autocomplete="off" placeholder="例: 新商品紹介ショート">',
  '<label for="dsc-file">動画ファイル</label>',
  '<input id="dsc-file" type="file" accept="video/*">',
  '<div class="dsc-bar"><i></i></div>',
  '<p class="dsc-msg"></p>',
  '<div class="dsc-row">',
  '<button type="button" class="dsc-btn" data-act="cancel">キャンセル</button>',
  '<button type="button" class="dsc-btn dsc-primary" data-act="submit">アップロード</button>',
  '</div></div>'
].join('');
document.body.appendChild(overlay);

const card = overlay.querySelector('.dsc-card');
const nameEl = overlay.querySelector('#dsc-name');
const fileEl = overlay.querySelector('#dsc-file');
const barEl = overlay.querySelector('.dsc-bar');
const barFill = overlay.querySelector('.dsc-bar > i');
const msgEl = overlay.querySelector('.dsc-msg');
const cancelBtn = overlay.querySelector('[data-act="cancel"]');
const submitBtn = overlay.querySelector('[data-act="submit"]');

let onKeyDown = null;
let activeXhr = null;
let settled = false;

const cleanup = () => {
  if (onKeyDown) { window.removeEventListener('keydown', onKeyDown, true); onKeyDown = null; }
  if (overlay && overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
};

const setBusy = (busy) => {
  nameEl.disabled = busy;
  fileEl.disabled = busy;
  submitBtn.disabled = busy;
  barEl.style.display = busy ? 'block' : 'none';
};

const showError = (text) => {
  msgEl.textContent = text;
  msgEl.className = 'dsc-msg dsc-err';
};

const result = await new Promise((resolve) => {
  const finish = (value) => {
    if (settled) { return; }
    settled = true;
    if (activeXhr) { try { activeXhr.abort(); } catch (e) { /* noop */ } }
    cleanup();
    resolve(value);
  };

  onKeyDown = (ev) => {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      finish(false);
    }
  };
  window.addEventListener('keydown', onKeyDown, true);

  overlay.addEventListener('mousedown', (ev) => { if (ev.target === overlay) { finish(false); } });
  card.addEventListener('mousedown', (ev) => { ev.stopPropagation(); });
  cancelBtn.addEventListener('click', () => { finish(false); });

  submitBtn.addEventListener('click', () => {
    const projectName = (nameEl.value || '').trim();
    const file = fileEl.files && fileEl.files[0];

    if (!projectName) { showError('プロジェクト名を入力してください。'); nameEl.focus(); return; }
    if (!file) { showError('動画ファイルを選択してください。'); return; }

    msgEl.className = 'dsc-msg';
    msgEl.textContent = 'アップロードを開始しています…';
    setBusy(true);

    const form = new FormData();
    form.append('file', file, file.name);

    const xhr = new XMLHttpRequest();
    activeXhr = xhr;
    // process=false: 動画は RAG の許可拡張子に含まれず、埋め込みパイプラインにも流さない
    xhr.open('POST', '/api/v1/files/?process=false&process_in_background=false');
    xhr.setRequestHeader('Authorization', 'Bearer ' + (localStorage.token || ''));
    xhr.setRequestHeader('Accept', 'application/json');

    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) { return; }
      const pct = Math.round((ev.loaded / ev.total) * 100);
      barFill.style.width = pct + '%';
      msgEl.textContent = 'アップロード中… ' + pct + '%';
    };

    xhr.onerror = () => {
      activeXhr = null;
      setBusy(false);
      showError('アップロードに失敗しました（ネットワークエラー）。');
    };

    xhr.onload = () => {
      activeXhr = null;
      let payload = null;
      try { payload = JSON.parse(xhr.responseText || 'null'); } catch (e) { payload = null; }

      if (xhr.status < 200 || xhr.status >= 300) {
        setBusy(false);
        const detail = (payload && (payload.detail || payload.message)) || ('HTTP ' + xhr.status);
        showError('アップロードに失敗しました: ' + detail);
        return;
      }
      if (!payload || !payload.id) {
        setBusy(false);
        showError('アップロード結果にファイル ID がありませんでした。');
        return;
      }

      msgEl.textContent = 'アップロードが完了しました。';
      finish({
        project_name: projectName,
        file_id: payload.id,
        file_name: payload.filename || file.name,
        file_size: file.size,
        file_url: '/api/v1/files/' + payload.id + '/content'
      });
    };

    xhr.send(form);
  });

  setTimeout(() => { try { nameEl.focus(); } catch (e) { /* noop */ } }, 0);
});

return result;
"""


class Action:
    # ★ actions は必ず class 直下のクラス属性（契約書 §9）。
    #   モジュールトップレベルに置くと単一アクション扱いに落ちて無視される。
    actions = [
        {"id": "upload", "name": "動画のアップロード"},
        {"id": "edit", "name": "動画の編集"},
        {"id": "export", "name": "タイムラインのエクスポート"},
        {"id": "probe", "name": "Descript MCP 診断"},
    ]

    class Valves(BaseModel):
        priority: int = Field(default=0, description="アクションボタンの表示順（昇順）")
        pipe_model_id: str = Field(
            default="descript_pipe",
            description="起動する Pipe のモデル ID。1 行で入力してください",
        )
        form_mode: str = Field(
            default="sequential",
            json_schema_extra={"input": {"type": "select", "options": ["sequential", "modal"]}},
            description="sequential=input ダイアログを連鎖 / modal=execute でカスタムフォームを表示",
        )
        bypass_model_access: bool = Field(
            default=True,
            description="Pipe モデルのアクセス制御をバイパスする（Action 自体が露出チェック済みのため既定 True）",
        )
        max_projects_in_select: int = Field(default=50, description="プロジェクト選択に表示する最大件数")
        action_soft_timeout_sec: int = Field(
            default=45,
            description="この秒数を超えそうな処理は早期 return する（リバースプロキシ切断対策）",
        )
        debug: bool = Field(default=False, description="詳細を status イベントに出す")
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
        default_aspect: str = Field(
            default="9:16",
            json_schema_extra={"input": {"type": "select", "options": ["9:16", "1:1", "16:9"]}},
        )
        default_duration_sec: int = Field(default=60, description="既定の出力尺（秒）")
        editing_style: str = Field(
            default="jet_cut",
            json_schema_extra={
                "input": {"type": "select", "options": ["jet_cut", "caption_focus", "dynamic_effects", "full"]}
            },
            description="編集スタイルのプリセット",
        )
        auto_confirm: bool = Field(default=False, description="編集ループの確認をスキップし 1 周で確定する")
        default_export_format: str = Field(
            default=_DEFAULT_EXPORT_FORMAT,
            json_schema_extra={"input": {"type": "select", "options": _EXPORT_FORMAT_VALUES}},
            description="エクスポート形式の既定値",
        )

    def __init__(self):
        # ★ 必須。これが無いと Open WebUI が Valves を注入しない（契約書 §9）。
        self.valves = self.Valves()

    # -------------------------------------------------------------------
    # イベント送出のラッパ
    # -------------------------------------------------------------------

    async def _status(self, emitter: Any, description: str, done: bool = False) -> None:
        if emitter is None:
            return
        try:
            await emitter({"type": "status", "data": {"description": description, "done": done}})
        except Exception:
            # UI が切れていても処理自体は続行させる
            pass

    async def _debug(self, emitter: Any, description: str) -> None:
        if not getattr(self.valves, "debug", False):
            return
        await self._status(emitter, f"[debug] {description}")

    async def _notify(self, emitter: Any, kind: str, content: str) -> None:
        if emitter is None:
            return
        try:
            await emitter({"type": "notification", "data": {"type": kind, "content": content}})
        except Exception:
            pass

    async def _embeds(self, emitter: Any, html_list: list) -> None:
        """embeds を送る。

        replace: True を必ず明示する（契約書 §6 冒頭）。backend は非 replace 時に
        保存側だけ既存 embeds へ追記する（socket/main.py:1036-1054）一方、
        フロントは全置換する（Chat.svelte:1007）ため DB と表示がズレる。
        """
        if emitter is None:
            return
        embeds = list(html_list or [])
        try:
            await emitter({"type": "embeds", "data": {"embeds": embeds, "replace": True}})
        except Exception:
            pass
        # 打ち消し用の空送信でスクロールを動かす理由はない（契約書 §6.0.1）
        if embeds:
            await _emit_scroll_bottom(emitter)

    # -------------------------------------------------------------------
    # 戻り値の組み立て
    # -------------------------------------------------------------------

    @staticmethod
    def _message(body: dict, content: str) -> dict:
        """フロントが history.messages にマージする形（Chat.svelte:2211-2222）。"""
        message_id = (body or {}).get("id")
        if not message_id:
            return {}
        return {"messages": [{"id": message_id, "content": _redact_signed_urls(content)}]}

    # -------------------------------------------------------------------
    # Pipe への RPC（契約書 §1.1）
    # -------------------------------------------------------------------

    async def _call_pipe(
        self,
        op: str,
        args: Optional[dict],
        *,
        body: dict,
        user: Any,
        request: Any,
        summary: str,
    ) -> dict:
        """descript_op を Pipe に投げ、封筒（§1.2）を返す。例外は封筒化する。"""
        # pipe_model_id は Valve が textarea でレンダリングされる（Valves.svelte:206-217）
        # ため、改行や前後空白が混入しうる。ここで正規化する。
        model_id = str(getattr(self.valves, "pipe_model_id", "") or "").strip()
        if not model_id:
            return {
                "ok": False,
                "op": op,
                "code": "INTERNAL",
                "message_ja": "Pipe のモデル ID が設定されていません。",
                "hint": "Admin → Functions → Descript Studio の Valve `pipe_model_id` を設定してください。",
                "data": None,
            }

        form_data = {
            "model": model_id,
            "messages": [{"role": "user", "content": summary}],
            "stream": False,  # ★ True にすると StreamingResponse が返り Action で扱えない
            "descript_op": op,
            "descript_args": dict(args or {}),
            "metadata": {
                "user_id": (user or {}).get("id") if isinstance(user, dict) else getattr(user, "id", None),
                "chat_id": body.get("chat_id"),
                "session_id": body.get("session_id"),
                # ★ 新規 UUID を振らない。振ると status/embeds が描画されず
                #    input/confirmation が 300 秒ハングする（契約書 §1.1）
                "message_id": body.get("id"),
                "task": None,
                "files": [],
                "tool_ids": [],
            },
        }

        _log_info(
            "rpc.start",
            op=op,
            chat_id=body.get("chat_id"),
            model=model_id,
            args=sorted((args or {}).keys()),
        )
        started = time.monotonic()
        try:
            res = await generate_chat_completion(
                request,
                form_data,
                _as_user_model(user),
                bypass_filter=bool(getattr(self.valves, "bypass_model_access", True)),
            )
        except Exception as exc:
            _log_error("rpc.crash", op=op, ms=_ms(started), error=str(exc)[:400])
            return {
                "ok": False,
                "op": op,
                "code": "INTERNAL",
                "message_ja": "オーケストレータ（Pipe）の呼び出しに失敗しました。",
                "hint": (
                    f"Valve `pipe_model_id`（現在: {model_id}）のモデルが存在し、"
                    "有効になっているかを確認してください。"
                ),
                "data": {"raw": str(exc)[:800]},
            }

        envelope = _unpack_pipe_response(res)
        if envelope.get("ok"):
            _log_info("rpc.done", op=op, ms=_ms(started))
        else:
            _log_warn(
                "rpc.fail",
                op=op,
                ms=_ms(started),
                code=envelope.get("code"),
                message=envelope.get("message_ja"),
            )
        return envelope

    # -------------------------------------------------------------------
    # エラー封筒の展開（契約書 §4）
    # -------------------------------------------------------------------

    @staticmethod
    def _notify_kind(code: str) -> str:
        if code in _WARNING_CODES:
            return "warning"
        if code in _INFO_CODES:
            return "info"
        return "error"

    def _error_markdown(self, env: dict) -> str:
        code = str(env.get("code") or "INTERNAL")
        message = str(env.get("message_ja") or "処理に失敗しました。")
        hint = str(env.get("hint") or "")

        if code == "MCP_OAUTH_REQUIRED":
            # 契約書 §4.1 の固定文言をそのまま使う
            return "### Descript との連携が未認可です\n\n" + _OAUTH_REQUIRED_TEXT

        lines = [f"### {message}", ""]
        if hint:
            lines.append(hint)
            lines.append("")

        if code == "NO_PROJECT":
            lines.append("「動画のアップロード」から先にプロジェクトを作成してください。")
            lines.append("")
        elif code == "TOOL_UNRESOLVED":
            lines.append("下の一覧から実ツール名を読み取り、Descript Orchestrator の `tool_*` Valve に設定してください。")
            lines.append("")
        elif code == "JOB_TIMEOUT":
            job_id = _pick(env.get("data") or {}, "job_id", default=None)
            lines.append("Descript 側では処理が継続している可能性があります。")
            if job_id:
                lines.append(f"（job id: `{job_id}`）")
            lines.append("")
        elif code == "QUOTA_EXCEEDED":
            lines.append("Descript の残高が回復するまで、再実行しても同じ結果になります。")
            lines.append("")

        raw = _pick(env.get("data") or {}, "raw", default=None)
        if raw:
            lines.append("<details><summary>詳細</summary>")
            lines.append("")
            lines.append("```")
            lines.append(_redact_signed_urls(str(raw)[:1500]))
            lines.append("```")
            lines.append("")
            lines.append("</details>")

        lines.append("")
        lines.append(f"`code: {code}`")
        return "\n".join(lines)

    async def _fail(self, body: dict, emitter: Any, env: dict, *, status_text: str = "") -> dict:
        """封筒（ok=False）を status + notification + 本文の 3 系統に展開する。"""
        code = str(env.get("code") or "INTERNAL")
        message = str(env.get("message_ja") or "処理に失敗しました。")

        await self._status(emitter, status_text or message, done=True)
        await self._notify(emitter, self._notify_kind(code), message)

        # TOOL_UNRESOLVED は解決動線（契約書 §4.2）としてツール表を出す
        if code == "TOOL_UNRESOLVED":
            data = env.get("data") or {}
            tools = data.get("tools") if isinstance(data, dict) else None
            if tools:
                html = self._tool_table_html(
                    tools,
                    resolved=None,
                    lead="必要なツールを特定できませんでした。下の表から実ツール名を Valve に設定してください。",
                )
                await self._embeds(emitter, [html])

        return self._message(body, self._error_markdown(env))

    # -------------------------------------------------------------------
    # HTML 組み立て
    # -------------------------------------------------------------------

    @staticmethod
    def _tool_table_html(tools: Any, resolved: Optional[dict] = None, lead: str = "") -> str:
        """契約書 §6.2 のツール一覧を組み立てる。"""
        # resolved: {論理op: 実ツール名 or None} → 実ツール名からの逆引きを作る
        reverse: dict = {}
        for op, name in (resolved or {}).items():
            if name:
                reverse.setdefault(str(name), []).append(str(op))

        rows = []
        items = tools if isinstance(tools, list) else []
        for spec in items:
            if not isinstance(spec, dict):
                continue
            name = _pick(spec, "name", "tool_name", default="")
            desc = _pick(spec, "description", "desc", default="")
            params = _pick(spec, "parameters", "input_schema", "inputSchema", default={}) or {}
            required_list = params.get("required") if isinstance(params, dict) else None
            required = ", ".join(str(r) for r in (required_list or [])) or "—"
            guess = " / ".join(reverse.get(str(name), [])) or "—"
            rows.append(
                "<tr><td><code>{name}</code></td><td>{guess}</td>"
                "<td>{desc}<div class='muted'>必須: {required}</div></td></tr>".format(
                    name=_esc(name), guess=_esc(guess), desc=_esc(desc), required=_esc(required)
                )
            )

        if not rows:
            rows.append("<tr><td colspan=\"3\">ツールが 1 件も返されませんでした。</td></tr>")

        return _render(
            _TOOL_TABLE,
            css=_BASE_CSS,
            height_js=_HEIGHT_JS,
            count=len(items),
            lead=_esc(lead),
            rows="".join(rows),
        )

    @staticmethod
    def _media_table_html(project: dict, lead: str = "") -> str:
        """プロジェクトに含まれるメディアの一覧。

        media_files の形（dict of dict / list）は MCP スキーマ未確定なので
        どちらでも読めるようにしている（契約書 §12）。
        """
        media = _pick(project or {}, "media_files", "media", "files", default={}) or {}
        if isinstance(media, dict):
            entries = list(media.values()) if media else []
        elif isinstance(media, list):
            entries = media
        else:
            entries = []

        rows = []
        for item in entries:
            if isinstance(item, str):
                rows.append(
                    "<tr><td>{n}</td><td>—</td><td>—</td></tr>".format(n=_esc(item))
                )
                continue
            if not isinstance(item, dict):
                continue
            name = _pick(item, "name", "filename", "title", "id", default="（名称不明）")
            kind = _pick(item, "type", "media_type", "kind", "mime_type", default="—")
            duration = _pick(item, "duration", "duration_sec", "length", default=None)
            note = f"{duration} 秒" if duration is not None else "—"
            rows.append(
                "<tr><td>{n}</td><td>{k}</td><td>{d}</td></tr>".format(
                    n=_esc(name), k=_esc(kind), d=_esc(note)
                )
            )

        if not rows:
            rows.append("<tr><td colspan=\"3\">メディアが登録されていません。</td></tr>")

        title = _pick(project or {}, "name", "title", default="プロジェクト")
        return _render(
            _MEDIA_TABLE,
            css=_BASE_CSS,
            height_js=_HEIGHT_JS,
            title=_esc(f"{title} に含まれるメディア"),
            lead=_esc(lead),
            rows="".join(rows),
        )

    @staticmethod
    def _player_html(title: str, src: str, app_url: str, share_url: str, revision: Any, note: str) -> str:
        return _render(
            _PLAYER,
            css=_BASE_CSS,
            height_js=_HEIGHT_JS,
            title=_esc(title),
            src=src or "",
            app_url=app_url or share_url or "",
            share_url=share_url or "",
            revision=_esc(revision if revision is not None else "-"),
            note=_esc(note),
        )

    @staticmethod
    def _timeline_export_html(
        *, title: str, format_label: str, download_url: str, app_url: str, expires_at: str
    ) -> str:
        """export_timeline の結果カードを組み立てる（契約書 §2.0.1 ④）。

        download_url は期限付きの署名 URL なので、この embeds の中だけに置く。
        app_url / expires_at が空のときは、その要素ごと出さない。
        """
        app_link = ""
        if app_url:
            app_link = (
                '<a class="btn" href="'
                + _esc(app_url)
                + '" target="_blank" rel="noopener">Descript で開く</a>'
            )

        expiry = ""
        if expires_at:
            expiry = (
                '<p class="muted" style="margin:6px 0 0">このリンクは '
                + _esc(expires_at)
                + " まで有効です。</p>"
            )

        return _render(
            _TIMELINE_EXPORT,
            css=_BASE_CSS,
            height_js=_HEIGHT_JS,
            title=_esc(title),
            format_label=_esc(format_label),
            download_url=_esc(download_url),
            app_link=app_link,
            notice=_esc(_TIMELINE_NOTICE),
            expiry=expiry,
        )

    # -------------------------------------------------------------------
    # 入力フォーム
    # -------------------------------------------------------------------

    async def _ask(self, event_call: Any, payload: dict) -> tuple[bool, Any, Optional[dict]]:
        """__event_call__ を呼び、契約書 §5.7 で戻り値を判定する。

        asyncio.wait_for では包まない（socket の ack を壊すため。契約書 §7）。
        """
        if event_call is None:
            return False, None, {
                "ok": False,
                "code": "UI_DISCONNECTED",
                "message_ja": "画面との接続がないため入力を受け取れませんでした。",
                "hint": "",
                "data": None,
            }
        try:
            value = await event_call(payload)
        except Exception as exc:
            return False, None, {
                "ok": False,
                "code": "UI_DISCONNECTED",
                "message_ja": "画面との接続が切れたため入力を受け取れませんでした。",
                "hint": "もう一度お試しください。",
                "data": {"raw": str(exc)[:800]},
            }
        return _form_result(value)

    async def _ask_text(
        self, event_call: Any, title: str, message: str, placeholder: str = "", value: str = ""
    ) -> tuple[bool, str, Optional[dict]]:
        ok, raw, err = await self._ask(
            event_call,
            {
                "type": "input",
                "data": {
                    "title": title,
                    "message": message,
                    "placeholder": placeholder,
                    "value": value,
                },
            },
        )
        if not ok:
            return False, "", err

        # ConfirmDialog は空欄のまま確定できる（空文字が返る）。キャンセル扱いにする。
        text = str(raw or "").strip()
        if not text:
            return False, "", {
                "ok": False,
                "code": "USER_CANCELLED",
                "message_ja": "入力が空だったため中止しました。",
                "hint": "",
                "data": None,
            }
        return True, text, None

    async def _ask_select(
        self, event_call: Any, title: str, message: str, options: list, placeholder: str = "", value: str = ""
    ) -> tuple[bool, str, Optional[dict]]:
        # data.value は ConfirmDialog の初期値になる（Chat.svelte:1126 →
        # ConfirmDialog.svelte:44 の _inputValue）。select では初期選択として効く。
        ok, raw, err = await self._ask(
            event_call,
            {
                "type": "input",
                "data": {
                    "title": title,
                    "message": message,
                    "placeholder": placeholder,
                    "value": value,
                    "input": {"type": "select", "options": options},
                },
            },
        )
        if not ok:
            return False, "", err

        value = str(raw or "").strip()
        if not value:
            return False, "", {
                "ok": False,
                "code": "USER_CANCELLED",
                "message_ja": "選択されなかったため中止しました。",
                "hint": "",
                "data": None,
            }
        return True, value, None

    async def _pick_project(
        self,
        *,
        body: dict,
        user: Any,
        request: Any,
        event_call: Any,
        emitter: Any,
        title: str,
        message: str,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """プロジェクト一覧を取得して選択させる。戻り値は (選択結果, エラー封筒)。"""
        await self._status(emitter, "プロジェクト一覧を取得中")
        env = await self._call_pipe(
            "list_projects",
            {},
            body=body,
            user=user,
            request=request,
            summary="Descript: プロジェクト一覧を取得",
        )
        if not env.get("ok"):
            return None, env

        projects = (env.get("data") or {}).get("projects") or []
        if not projects:
            return None, {
                "ok": False,
                "op": "list_projects",
                "code": "NO_PROJECT",
                "message_ja": "Descript にプロジェクトがありません。",
                "hint": "",
                "data": None,
            }

        try:
            limit = int(getattr(self.valves, "max_projects_in_select", 50))
        except Exception:
            limit = 50
        limit = max(1, limit)

        options = []
        names: dict = {}
        for project in projects[:limit]:
            if not isinstance(project, dict):
                continue
            project_id = _pick(project, "id", "project_id", default=None)
            if not project_id:
                continue
            name = str(_pick(project, "name", "title", default=project_id))
            options.append({"label": name, "value": str(project_id)})
            names[str(project_id)] = name

        if not options:
            return None, {
                "ok": False,
                "op": "list_projects",
                "code": "INTERNAL",
                "message_ja": "プロジェクト一覧の形式を解釈できませんでした。",
                "hint": "",
                "data": {"raw": str(projects)[:800]},
            }

        truncated = len(projects) > len(options)
        note = f"（{len(projects)} 件中 {len(options)} 件を表示）" if truncated else ""

        # 契約書 §7: 長時間処理のあとで __event_call__ を開く。ここで status を閉じる。
        await self._status(emitter, f"{len(projects)} 件のプロジェクトを取得しました", done=True)

        ok, project_id, err = await self._ask_select(
            event_call, title, message + note, options, placeholder="プロジェクトを選択"
        )
        if not ok:
            return None, err

        return {"project_id": project_id, "project_name": names.get(project_id, project_id)}, None

    # -------------------------------------------------------------------
    # サブアクション: probe（依存ゼロ。最初に動くべきもの）
    # -------------------------------------------------------------------

    async def _act_probe(self, *, body: dict, user: Any, request: Any, emitter: Any) -> Any:
        await self._status(emitter, "Descript MCP に接続して診断しています")
        env = await self._call_pipe(
            "probe",
            {},
            body=body,
            user=user,
            request=request,
            summary="Descript MCP 診断",
        )

        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="診断に失敗しました")

        data = env.get("data") or {}
        tools = data.get("tools") or []
        resolved = data.get("resolved") or {}
        server = data.get("server") or {}

        resolved_count = sum(1 for op in _LOGICAL_OPS if resolved.get(op))
        server_name = _pick(server, "name", "title", "id", default="Descript MCP")

        await self._status(
            emitter,
            f"{len(tools)} 件のツールを検出（論理操作 {resolved_count}/{len(_LOGICAL_OPS)} 件を解決）",
            done=True,
        )

        html = self._tool_table_html(
            tools,
            resolved=resolved,
            lead=(
                f"{server_name} に接続できました。"
                f"論理操作 {resolved_count}/{len(_LOGICAL_OPS)} 件が自動解決されています。"
            ),
        )

        unresolved = [op for op in _LOGICAL_OPS if not resolved.get(op)]
        lines = ["### Descript MCP 診断結果", ""]
        lines.append(f"- 接続先: **{server_name}**")
        lines.append(f"- 検出ツール数: **{len(tools)}**")
        lines.append(f"- 解決済みの論理操作: **{resolved_count} / {len(_LOGICAL_OPS)}**")
        lines.append("")
        if unresolved:
            lines.append("未解決の論理操作:")
            lines.append("")
            for op in unresolved:
                lines.append(f"- `{op}` → Valve `tool_{op}` に実ツール名を設定してください")
            lines.append("")
            lines.append("下の一覧から該当するツール名を読み取ってください。")
        else:
            lines.append("すべての論理操作が解決済みです。「動画のアップロード」から開始できます。")

        if resolved:
            lines.append("")
            lines.append("| 論理操作 | 解決された実ツール名 |")
            lines.append("| --- | --- |")
            for op in _LOGICAL_OPS:
                lines.append(f"| `{op}` | {resolved.get(op) or '—'} |")

        message = "\n".join(lines)

        # actions.py:137-145 は HTMLResponse 由来の embeds を replace 指定なしで送るため、
        # 同じ message_id で再実行すると DB 側にだけ embeds が積み上がる
        # （socket/main.py:1040-1044）。先に空配列を replace:True で送って打ち消す。
        await self._embeds(emitter, [])

        # この経路の embeds は return 後に actions.py が送るので _embeds では拾えない。
        # ピン留めは 3 秒回り続けるため、先に撃っておけば後続の embeds に間に合う。
        await _emit_scroll_bottom(emitter)

        # (HTMLResponse, result_context) タプルで返すと embeds 化と同時に
        # 戻り値も差し替えられる（utils/middleware.py:887-904, actions.py:130-148）。
        return (
            HTMLResponse(content=html, headers={"Content-Disposition": "inline"}),
            self._message(body, message),
        )

    # -------------------------------------------------------------------
    # サブアクション: upload（UC1）
    # -------------------------------------------------------------------

    async def _collect_upload_input(
        self, *, event_call: Any, emitter: Any
    ) -> tuple[Optional[dict], Optional[dict]]:
        mode = str(getattr(self.valves, "form_mode", "sequential") or "sequential").strip()

        if mode == "modal":
            await self._debug(emitter, "form_mode=modal: execute でカスタムフォームを表示")
            ok, value, err = await self._ask(
                event_call, {"type": "execute", "data": {"code": _UPLOAD_MODAL_JS}}
            )
            if not ok:
                return None, err
            if not isinstance(value, dict) or not value.get("file_id"):
                return None, {
                    "ok": False,
                    "code": "USER_CANCELLED",
                    "message_ja": "アップロードが完了しませんでした。",
                    "hint": "",
                    "data": None,
                }
            project_name = str(value.get("project_name") or "").strip()
            if not project_name:
                return None, {
                    "ok": False,
                    "code": "USER_CANCELLED",
                    "message_ja": "プロジェクト名が空だったため中止しました。",
                    "hint": "",
                    "data": None,
                }
            return {
                "project_name": project_name,
                "file_id": str(value.get("file_id")),
                "file_name": str(value.get("file_name") or ""),
                "source_url": None,
            }, None

        # sequential: input を 2 回連鎖する
        await self._debug(emitter, "form_mode=sequential: input を 2 回連鎖")
        ok, project_name, err = await self._ask_text(
            event_call,
            "プロジェクト名",
            "Descript のプロジェクト名を入力してください。同名があればそれを再利用します。",
            placeholder="例: 新商品紹介ショート",
        )
        if not ok:
            return None, err

        ok, source_url, err = await self._ask_text(
            event_call,
            "動画の URL",
            "取り込む動画の URL を入力してください。（Descript から到達できる公開 URL）",
            placeholder="https://…",
        )
        if not ok:
            return None, err

        return {
            "project_name": project_name,
            "file_id": None,
            "file_name": "",
            "source_url": source_url,
        }, None

    async def _act_upload(
        self, *, body: dict, user: Any, request: Any, event_call: Any, emitter: Any, started: float
    ) -> dict:
        collected, err = await self._collect_upload_input(event_call=event_call, emitter=emitter)
        if collected is None:
            return await self._fail(body, emitter, err or {}, status_text="中止しました")

        project_name = collected["project_name"]

        await self._status(emitter, f"プロジェクト「{project_name}」を確認しています")
        env = await self._call_pipe(
            "ensure_project",
            {"name": project_name},
            body=body,
            user=user,
            request=request,
            summary=f"Descript: プロジェクト「{project_name}」を確保",
        )
        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="プロジェクトの確保に失敗しました")

        data = env.get("data") or {}
        project_id = data.get("project_id")
        project_name = data.get("project_name") or project_name
        created = bool(data.get("created"))

        if self._soft_expired(started):
            # ここから import_media を始めるとリバースプロキシに切られる可能性が高い。
            # 何も返さないまま切断されるより、状態を伝えて明示的に打ち切る。
            await self._status(emitter, "プロジェクトを確保しました", done=True)
            await self._notify(emitter, "warning", "処理時間の上限に達したため取り込みを中断しました。")
            return self._message(
                body,
                "### プロジェクトまで作成しました\n\n"
                f"- プロジェクト: **{project_name}**"
                + ("（新規作成）" if created else "（既存を再利用）")
                + "\n\n"
                "処理時間が上限に達したため、メディアの取り込みは実行していません。\n"
                f"もう一度「動画のアップロード」を実行し、プロジェクト名に **{project_name}** を"
                "指定してください（同名のプロジェクトは再利用されます）。",
            )

        await self._status(emitter, "Descript にメディアを取り込んでいます")
        media_args = {
            "project_id": project_id,
            "project_name": project_name,
            # source_url / file_id はどちらか。どちらを使うかは MCP スキーマ確定後に決まる
            # （契約書 §12 の未確定事項）。両方渡し、Pipe 側で選択させる。
            "source_url": collected.get("source_url"),
            "file_id": collected.get("file_id"),
        }
        env = await self._call_pipe(
            "import_media",
            media_args,
            body=body,
            user=user,
            request=request,
            summary=f"Descript: 「{project_name}」へメディアを取り込み",
        )
        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="メディアの取り込みに失敗しました")

        data = env.get("data") or {}
        project_url = data.get("project_url")
        media = data.get("media") or {}
        media_name = _pick(media, "name", "filename", "title", default=collected.get("file_name") or "")

        await self._status(emitter, "取り込みが完了しました", done=True)
        await self._notify(emitter, "success", f"「{project_name}」に動画を取り込みました。")

        lines = ["### 動画を取り込みました", ""]
        lines.append(f"- プロジェクト: **{project_name}**" + ("（新規作成）" if created else "（既存を再利用）"))
        if media_name:
            lines.append(f"- メディア: {media_name}")
        if project_url:
            lines.append(f"- [Descript で開く]({project_url})")
        lines.append("")
        lines.append("続けて「動画の編集」から編集指示を出せます。")
        return self._message(body, "\n".join(lines))

    # -------------------------------------------------------------------
    # サブアクション: edit（UC2）
    # -------------------------------------------------------------------

    async def _act_edit(
        self, *, body: dict, user: Any, request: Any, event_call: Any, emitter: Any, started: float
    ) -> dict:
        picked, err = await self._pick_project(
            body=body,
            user=user,
            request=request,
            event_call=event_call,
            emitter=emitter,
            title="編集するプロジェクト",
            message="編集対象の Descript プロジェクトを選択してください。",
        )
        if picked is None:
            return await self._fail(body, emitter, err or {}, status_text="中止しました")

        project_id = picked["project_id"]
        project_name = picked["project_name"]

        await self._status(emitter, f"「{project_name}」の内容を取得中")
        env = await self._call_pipe(
            "get_project",
            {"project_id": project_id},
            body=body,
            user=user,
            request=request,
            summary=f"Descript: プロジェクト「{project_name}」を取得",
        )
        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="プロジェクトの取得に失敗しました")

        project = (env.get("data") or {}).get("project") or {}
        await self._embeds(
            emitter,
            [self._media_table_html(project, lead="このプロジェクトに対して編集指示を出します。")],
        )
        await self._status(emitter, "プロジェクトの内容を表示しました", done=True)

        ok, instruction, err = await self._ask_text(
            event_call,
            "編集指示",
            "どのように編集しますか。日本語で自由に指示してください。",
            placeholder="例: 沈黙と言い淀みをカットして 60 秒に圧縮し、字幕を付けて",
        )
        if not ok:
            return await self._fail(body, emitter, err or {}, status_text="中止しました")

        user_valves = self._user_valves(user)

        if self._soft_expired(started):
            # start_edit_loop は最も長い処理。budget を使い切った状態で始めない。
            await self._status(emitter, "処理時間の上限に達しました", done=True)
            await self._notify(emitter, "warning", "処理時間の上限に達したため編集を開始しませんでした。")
            return self._message(
                body,
                "### 編集を開始できませんでした\n\n"
                f"- プロジェクト: **{project_name}**\n"
                f"- 指示: {instruction}\n\n"
                "入力に時間がかかり、処理時間の上限に達しました。\n"
                "もう一度「動画の編集」を実行し、同じ指示を入力してください。",
            )

        await self._status(emitter, "編集プロンプトを生成して Descript に依頼しています")
        env = await self._call_pipe(
            "start_edit_loop",
            {
                "project_id": project_id,
                "instruction": instruction,
                "style": user_valves["editing_style"],
                "aspect": user_valves["default_aspect"],
                "duration_sec": user_valves["default_duration_sec"],
                "auto_confirm": user_valves["auto_confirm"],
            },
            body=body,
            user=user,
            request=request,
            summary=f"Descript: 「{project_name}」を編集 - {instruction}",
        )
        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="編集に失敗しました")

        data = env.get("data") or {}
        revision = data.get("revision")
        awaiting = str(data.get("awaiting") or "")
        agent_response = str(data.get("agent_response") or "")
        share_url = data.get("share_url") or ""
        project_url = str(data.get("project_url") or "")

        await self._status(emitter, "編集が完了しました", done=True)

        # 以降の編集ループは Pipe が embeds フォームで継続する（契約書 §6.3）。
        # Action はここで早期 return し、__event_call__ を開いたままにしない。
        lines = ["### 編集を実行しました", ""]
        lines.append(f"- プロジェクト: **{project_name}**")
        if revision is not None:
            lines.append(f"- リビジョン: rev {revision}")
        lines.append(f"- 指示: {instruction}")
        # embeds の中のリンクは sandbox を引き継いだタブで開かれて動かない（契約書 §6.0.2）。
        # 本文側のリンクは本体が描画するので sandbox の影響を受けない。
        if project_url:
            lines.append(f"- [Descript で開く]({project_url})")
        if share_url:
            lines.append(f"- [共有リンク]({share_url})")
        lines.append("")
        if agent_response:
            lines.append(_redact_signed_urls(agent_response))
            lines.append("")
        if awaiting == "user":
            lines.append("プレビューを確認し、下のフォームから「再編集」または「確定」を選んでください。")
        else:
            lines.append("編集は確定済みです。「タイムラインのエクスポート」に進めます。")
        return self._message(body, "\n".join(lines))

    # -------------------------------------------------------------------
    # サブアクション: export（UC3）
    # -------------------------------------------------------------------

    async def _act_export(
        self, *, body: dict, user: Any, request: Any, event_call: Any, emitter: Any, started: float
    ) -> dict:
        picked, err = await self._pick_project(
            body=body,
            user=user,
            request=request,
            event_call=event_call,
            emitter=emitter,
            title="エクスポートするプロジェクト",
            message="書き出す Descript プロジェクトを選択してください。",
        )
        if picked is None:
            return await self._fail(body, emitter, err or {}, status_text="中止しました")

        project_id = picked["project_id"]
        project_name = picked["project_name"]

        # 出力形式の選択。既定値は UserValves（毎回選び直さなくて済むように）。
        default_format = self._user_valves(user)["default_export_format"]
        ok, chosen, err = await self._ask_select(
            event_call,
            "出力形式",
            f"「{project_name}」をどの形式で書き出しますか。\n"
            "※ 映像を含まないプロジェクトで Video を選ぶと失敗します（契約書 §2.0.1 ②）。",
            _export_format_options(default_format),
            placeholder="出力形式を選択",
            value=default_format,
        )
        if not ok:
            return await self._fail(body, emitter, err or {}, status_text="中止しました")

        fmt = _export_format(chosen)

        state = await _load_state(body.get("chat_id"))
        composition_id = state.get("composition_id") if state.get("project_id") == project_id else None

        if self._soft_expired(started):
            await self._status(emitter, "処理時間の上限に達しました", done=True)
            await self._notify(emitter, "warning", "処理時間の上限に達したため書き出しを開始しませんでした。")
            return self._message(
                body,
                "### 書き出しを開始できませんでした\n\n"
                f"- プロジェクト: **{project_name}**\n"
                f"- 形式: **{fmt['label']}**\n\n"
                "選択に時間がかかり、処理時間の上限に達しました。\n"
                "もう一度「タイムラインのエクスポート」を実行してください。",
            )

        if fmt["op"] == "publish":
            return await self._export_share(
                body=body,
                user=user,
                request=request,
                emitter=emitter,
                project_id=project_id,
                project_name=project_name,
                composition_id=composition_id,
                fmt=fmt,
                revision=state.get("revision"),
            )
        return await self._export_timeline(
            body=body,
            user=user,
            request=request,
            emitter=emitter,
            project_id=project_id,
            project_name=project_name,
            composition_id=composition_id,
            fmt=fmt,
        )

    async def _export_share(
        self,
        *,
        body: dict,
        user: Any,
        request: Any,
        emitter: Any,
        project_id: str,
        project_name: str,
        composition_id: Optional[str],
        fmt: dict,
        revision: Any,
    ) -> dict:
        """共有リンク（publish）としての書き出し。"""
        await self._status(emitter, f"「{project_name}」を書き出しています（{fmt['label']}）")
        env = await self._call_pipe(
            "publish",
            {
                "project_id": project_id,
                "composition_id": composition_id,
                "media_type": fmt["media_type"],
            },
            body=body,
            user=user,
            request=request,
            summary=f"Descript: 「{project_name}」を書き出し（{fmt['label']}）",
        )
        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="書き出しに失敗しました")

        data = env.get("data") or {}
        share_url = str(data.get("share_url") or "")
        # download_url は署名付き・期限付き。表示のその場限りで使い、本文には残さない（契約書 §6.1 / §11）
        download_url = str(data.get("download_url") or "")
        app_url = str(_pick(data, "app_url", "project_url", "editor_url", default="") or share_url)

        await self._status(emitter, "書き出しが完了しました", done=True)
        await self._notify(emitter, "success", f"「{project_name}」を書き出しました。")

        await self._embeds(
            emitter,
            [
                self._player_html(
                    title=f"{project_name} の書き出し結果",
                    src=download_url or share_url,
                    app_url=app_url,
                    share_url=share_url,
                    revision=revision,
                    note=fmt["label"],
                )
            ],
        )

        lines = ["### 書き出しが完了しました", ""]
        lines.append(f"- プロジェクト: **{project_name}**")
        lines.append(f"- 形式: **{fmt['label']}**")
        if share_url:
            lines.append(f"- [共有リンク]({share_url})")
        if app_url and app_url != share_url:
            lines.append(f"- [Descript App で開く]({app_url})")
        lines.append("")
        lines.append("※ ダウンロードリンクは期限付きのため、履歴には残していません。")
        return self._message(body, "\n".join(lines))

    async def _export_timeline(
        self,
        *,
        body: dict,
        user: Any,
        request: Any,
        emitter: Any,
        project_id: str,
        project_name: str,
        composition_id: Optional[str],
        fmt: dict,
    ) -> dict:
        """タイムライン/XML ファイルとしての書き出し（契約書 §2.0.1 ④）。"""
        await self._status(emitter, f"「{project_name}」のタイムラインを書き出しています（{fmt['label']}）")
        env = await self._call_pipe(
            "export_timeline",
            {"project_id": project_id, "composition_id": composition_id, "format": fmt["value"]},
            body=body,
            user=user,
            request=request,
            summary=f"Descript: 「{project_name}」のタイムラインを書き出し（{fmt['label']}）",
        )
        if not env.get("ok"):
            return await self._fail(body, emitter, env, status_text="書き出しに失敗しました")

        data = env.get("data") or {}
        # download_url は期限付きの署名 URL。embeds にだけ載せ、本文には残さない（契約書 §5.9 / §11）
        download_url = str(data.get("download_url") or "")
        expires_at = str(_pick(data, "expires_at", "download_url_expires_at", default="") or "")
        app_url = str(_pick(data, "app_url", "project_url", "editor_url", default="") or "")

        if not download_url:
            return await self._fail(
                body,
                emitter,
                {
                    "ok": False,
                    "op": "export_timeline",
                    "code": "INTERNAL",
                    "message_ja": "書き出しは終了しましたが、ダウンロード URL を取得できませんでした。",
                    "hint": "もう一度実行するか、Descript App から書き出してください。",
                    "data": None,
                },
                status_text="書き出しに失敗しました",
            )

        await self._status(emitter, "書き出しが完了しました", done=True)
        await self._notify(emitter, "success", f"「{project_name}」を {fmt['label']} で書き出しました。")

        await self._embeds(
            emitter,
            [
                self._timeline_export_html(
                    title=f"{project_name} のタイムライン書き出し",
                    format_label=fmt["label"],
                    download_url=download_url,
                    app_url=app_url,
                    expires_at=expires_at,
                )
            ],
        )

        lines = ["### タイムラインの書き出しが完了しました", ""]
        lines.append(f"- プロジェクト: **{project_name}**")
        lines.append(f"- 形式: **{fmt['label']}**")
        if app_url:
            lines.append(f"- [Descript App で開く]({app_url})")
        lines.append("")
        lines.append("上のカードの「ダウンロード」ボタンからファイルを取得してください。")
        lines.append("")
        lines.append(f"※ {_TIMELINE_NOTICE}")
        lines.append("※ ダウンロードリンクは期限付きのため、履歴には残していません。")
        return self._message(body, "\n".join(lines))

    # -------------------------------------------------------------------
    # 補助
    # -------------------------------------------------------------------

    def _soft_expired(self, started: float) -> bool:
        """action_soft_timeout_sec を超えたか（契約書 §7）。"""
        try:
            limit = float(getattr(self.valves, "action_soft_timeout_sec", 45))
        except Exception:
            limit = 45.0
        return limit > 0 and (time.monotonic() - started) > limit

    def _user_valves(self, user: Any) -> dict:
        """UserValves を安全に読む。未設定でも既定値で動くようにする。"""
        defaults = self.UserValves()
        raw = None
        if isinstance(user, dict):
            raw = user.get("valves")
        if raw is None:
            raw = defaults

        def get(name: str) -> Any:
            value = getattr(raw, name, None)
            if value is None and isinstance(raw, dict):
                value = raw.get(name)
            return value if value is not None else getattr(defaults, name)

        return {
            "default_aspect": str(get("default_aspect")),
            "default_duration_sec": int(get("default_duration_sec")),
            "editing_style": str(get("editing_style")),
            "auto_confirm": bool(get("auto_confirm")),
            # 未知の値（Valve を手で書き換えた等）は _export_format が既定に落とす
            "default_export_format": _export_format(get("default_export_format"))["value"],
        }

    @staticmethod
    def _menu_markdown() -> str:
        return "\n".join(
            [
                "### Descript Studio",
                "",
                "以下のアクションから選んでください。",
                "",
                "- **動画のアップロード** — プロジェクトを作り、動画を Descript に取り込みます",
                "- **動画の編集** — 編集指示から編集し、プレビューを確認して確定します",
                "- **タイムラインのエクスポート** — 共有リンク / FCPXML など形式を選んで書き出します",
                "- **Descript MCP 診断** — 接続とツール名の解決状況を確認します",
                "",
                "初回は必ず「Descript MCP 診断」から実行してください。",
            ]
        )

    # -------------------------------------------------------------------
    # エントリポイント
    # -------------------------------------------------------------------

    async def action(
        self,
        body: dict,
        __user__=None,
        __event_emitter__=None,
        __event_call__=None,
        __model__=None,
        __id__=None,
        __request__=None,
    ) -> dict:
        body = body or {}
        started = time.monotonic()
        # ★ ログレベルの適用は __init__ ではなくここで行う。Open WebUI は呼び出しの
        #    たびに function_module.valves をインスタンスごと差し替える
        #    （utils/actions.py:87）ため、__init__ では常に既定値しか読めない。
        _apply_log_level(self.valves.log_level)
        # __id__ は sub_action_id（無ければ action_id）が入る（utils/actions.py:100）
        sub = str(__id__ or "").split(".")[-1]

        try:
            if not body.get("id"):
                # message_id が無いと status/embeds/input のいずれも届かない（契約書 §1.1）
                await self._notify(__event_emitter__, "error", "メッセージ ID を取得できませんでした。")
                return {}

            if __request__ is None:
                return self._message(
                    body, "### 内部エラー\n\nリクエストコンテキストを取得できませんでした。"
                )

            await self._debug(__event_emitter__, f"sub_action={sub}")
            _log_info(
                "action.start",
                sub=sub or "(menu)",
                chat_id=body.get("chat_id"),
                message_id=body.get("id"),
            )

            if sub == "probe":
                return await self._act_probe(
                    body=body, user=__user__, request=__request__, emitter=__event_emitter__
                )
            if sub == "upload":
                return await self._act_upload(
                    body=body,
                    user=__user__,
                    request=__request__,
                    event_call=__event_call__,
                    emitter=__event_emitter__,
                    started=started,
                )
            if sub == "edit":
                return await self._act_edit(
                    body=body,
                    user=__user__,
                    request=__request__,
                    event_call=__event_call__,
                    emitter=__event_emitter__,
                    started=started,
                )
            if sub == "export":
                return await self._act_export(
                    body=body,
                    user=__user__,
                    request=__request__,
                    event_call=__event_call__,
                    emitter=__event_emitter__,
                    started=started,
                )

            return self._message(body, self._menu_markdown())

        except Exception as exc:
            # 想定外の例外も日本語メッセージに変換する（正常系は封筒で受ける）
            _LOGGER.exception("descript action.crash sub=%s ms=%s", sub, _ms(started))
            await self._status(__event_emitter__, "処理を中断しました", done=True)
            await self._notify(__event_emitter__, "error", "内部エラーが発生しました。")
            return self._message(
                body,
                self._error_markdown(
                    {
                        "code": "INTERNAL",
                        "message_ja": "内部エラーが発生しました。",
                        "hint": "時間をおいて再実行してください。解消しない場合は管理者に連絡してください。",
                        "data": {"raw": str(exc)[:800]},
                    }
                ),
            )
