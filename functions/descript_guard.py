"""
title: Descript Guard
author: A-clear
author_url: https://github.com/A-clear/short-video-creation
version: 0.1.0
license: MIT
description: 動画添付の RAG 迂回・署名付き URL の恒久化・Descript 連携の事前診断
required_open_webui_version: 0.11.0
requirements:
"""

# ---------------------------------------------------------------------------
# descript_guard — Filter Function
#
# 【この Filter が走る経路】
# Action → Pipe の直接呼び出し（generate_chat_completion）では **走らない**。
# generate_chat_completion は process_chat_payload を通らないため。
# 実際に走るのは「ユーザが Descript Pipe モデルを選んで通常チャットした時」だけ。
# したがって本ファイルの各フックは、空振り（state も添付も無い）しても
# 完全に無害であることを最優先に実装している。
#
# 【落ちてはいけない】
# Filter が例外を投げるとチャット全体が停止する（middleware.py:2525-2526 が
# inlet の例外をそのまま再送出する）。inlet / stream / outlet は全体を
# try/except で包み、失敗時は入力をそのまま返す。
# ---------------------------------------------------------------------------

import asyncio  # noqa: F401  (§5.0 import 規約: 3 ファイルで同一の import 群を保つ)
import json
import logging
import re
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from open_webui.models.chats import Chats
from open_webui.models.users import UserModel

# add_or_update_system_message は backend/open_webui/utils/misc.py:580 に実在する
# （v0.11.0 で確認済み）。将来の upstream 変更で消えても Filter 全体が
# ロード不能にならないよう、フォールバックを用意しておく。
try:
    from open_webui.utils.misc import add_or_update_system_message
except Exception:  # pragma: no cover - upstream 変更時のみ通る

    def add_or_update_system_message(content: str, messages: list, append: bool = False) -> list:
        """messages 先頭の system メッセージに content を足す（本家と同じ挙動）。"""
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            base = messages[0].get("content")
            if isinstance(base, list):
                for item in base:
                    if isinstance(item, dict) and item.get("type") == "text":
                        item["text"] = f'{item.get("text", "")}\n{content}' if append else f'{content}\n{item.get("text", "")}'
            else:
                messages[0]["content"] = f"{base}\n{content}" if append else f"{content}\n{base}"
        else:
            messages.insert(0, {"role": "system", "content": content})
        return messages


# ===========================================================================
# §5.1 定数・例外（契約書 functions_contract.md からの転記。書き換え禁止）
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
# §5.10 ロギング（契約書からの転記。書き換え禁止）
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
# §5.2 小物（契約書からの転記。書き換え禁止）
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
# §5.3 状態の読み書き（契約書からの転記。書き換え禁止）
#
# Filter は原則読み取り専用。書き込みが許されるのは outlet からの
# history append のみ（契約書 §3.3）。
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
# §5.9 署名付き URL の秘匿（契約書からの転記。書き換え禁止）
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
# ここから descript_guard 固有のヘルパ
# ===========================================================================

_PREFLIGHT_KEY = "descript_preflight"
_MEDIA_KEY = "descript_media"

_OAUTH_HINT = (
    "チャット入力欄の ＋ ボタン → Integrations → Tools から Descript を一度有効化し、"
    "ブラウザに表示される同意画面を完了してください。"
    "（一度完了すれば、以降のトークン更新は自動で行われます）"
)

# 生 JSON ブロックを畳むときに探すフェンス。```json ... ``` のみを対象にする。
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _model_id(body: Any, model: Any) -> str:
    """body / __model__ からモデル ID を取り出す。取れなければ空文字。"""
    for candidate in (model, body):
        if isinstance(candidate, dict):
            value = candidate.get("id") if candidate is model else candidate.get("model")
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                return value["id"]
    if isinstance(model, str) and model:
        return model
    return ""


def _split_csv(value: Any) -> list:
    """カンマ区切りの Valve を小文字トークンの配列にする。"""
    return [t.strip().lower().lstrip(".") for t in str(value or "").split(",") if t.strip()]


def _file_label(entry: Any) -> str:
    """添付ファイル 1 件から、拡張子/MIME 判定に使える文字列をまとめて連結する。

    Open WebUI の files 要素は経路によって形が違う
    （{'type','id','name','url',...} / {'file': {'filename','meta': {...}}} など）
    ため、既知のキーを総当たりで拾う。
    """
    if not isinstance(entry, dict):
        return str(entry or "").lower()
    inner = entry.get("file") if isinstance(entry.get("file"), dict) else {}
    meta = inner.get("meta") if isinstance(inner.get("meta"), dict) else {}
    entry_meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    parts = []
    for src in (entry, inner, meta, entry_meta):
        for key in ("name", "filename", "url", "path", "content_type", "mime_type", "type"):
            value = src.get(key)
            if isinstance(value, str):
                parts.append(value)
    return " ".join(parts).lower()


def _is_video_file(entry: Any, extensions: list) -> bool:
    label = _file_label(entry)
    if not label:
        return False
    if "video/" in label:
        return True
    for ext in extensions:
        # 拡張子の直後が英数字でないこと（.mp4x を誤検知しない）
        if re.search(r"\." + re.escape(ext) + r"(?![a-z0-9])", label):
            return True
    return False


def _delta_holder(event: dict) -> tuple:
    """ストリームチャンクから (choice, 'delta' or 'message', content 文字列) を取り出す。

    取り出せなければ (None, None, None)。
    """
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, None, None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None, None, None
    for key in ("delta", "message"):
        holder = choice.get(key)
        if isinstance(holder, dict) and isinstance(holder.get("content"), str):
            return choice, key, holder["content"]
    return None, None, None


def _is_droppable(event: dict, choice: dict, holder_key: str) -> bool:
    """本文が空になったチャンクを丸ごと捨ててよいかを判定する。

    finish_reason / usage / event / sources など、本文以外の情報を
    載せているチャンクは絶対に落とさない（落とすと会話が終了しなくなる）。
    """
    for key in ("usage", "event", "sources", "selected_model_id", "error"):
        if event.get(key):
            return False
    if choice.get("finish_reason"):
        return False
    holder = choice.get(holder_key)
    if not isinstance(holder, dict):
        return False
    return not any(v for k, v in holder.items() if k not in ("content", "role"))


def _strip_markers(text: str, prefix: str) -> tuple:
    """接頭辞で始まる行を取り除き、(残った本文, 取り出したペイロード配列) を返す。

    ⚠️ 予約機能。現行の Pipe はマーカーを一切出力しない（契約書 §1.5）。
    Pipe は単一の JSON 封筒文字列を返す設計で、進捗は __event_emitter__ の
    status イベントで直接送っているため。本関数は
      ① 将来 Pipe をストリーミング化したときの受け口
      ② 内部制御文字列が誤って本文に混ざった場合の防御
    の 2 目的で残している。

    将来マーカーを使う場合の必須条件: ストリームは任意の位置で分割されうる
    ため、マーカー行が 2 チャンクにまたがると検出できない。Pipe 側で
    「1 マーカー = 1 チャンク（前後を改行で囲む）」で送出することを前提とする。
    バッファリングは本文欠落のリスクが大きいので Filter 側では行わない。
    """
    if not prefix or prefix not in text:
        return text, []
    kept = []
    payloads = []
    for line in text.split("\n"):
        if line.lstrip().startswith(prefix):
            payloads.append(line.lstrip()[len(prefix) :].strip())
        else:
            kept.append(line)
    return "\n".join(kept), payloads


def _marker_label(payload: str) -> str:
    """マーカーのペイロードを status の description 用 1 行にする。"""
    try:
        obj = json.loads(payload)
    except Exception:
        return payload[:200]
    if not isinstance(obj, dict):
        return str(obj)[:200]
    label = _pick(obj, "label", "description", "message", "status", "op", default="処理中")
    percent = obj.get("percent")
    suffix = f" {percent}%" if percent is not None else ""
    return f"{label}{suffix}"[:200]


def _redact_message_content(content: Any, replacement: str) -> tuple:
    """メッセージ content（str / マルチモーダル list）に署名 URL 置換を適用する。

    戻り値は (置換後 content, 変更があったか)。
    """
    if isinstance(content, str):
        new = _redact_signed_urls(content, replacement)
        return new, new != content
    if isinstance(content, list):
        changed = False
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                new = _redact_signed_urls(item["text"], replacement)
                if new != item["text"]:
                    item["text"] = new
                    changed = True
        return content, changed
    return content, False


def _fold_json_blocks(text: Any) -> tuple:
    """```json フェンスを <details> で畳む。戻り値は (変換後, 変更があったか)。"""
    if not isinstance(text, str) or "```json" not in text:
        return text, False
    if "<summary>ジョブの生 JSON</summary>" in text:
        return text, False

    def _repl(match):
        return "<details><summary>ジョブの生 JSON</summary>\n\n```json\n" + match.group(1) + "\n```\n\n</details>"

    new = _JSON_FENCE_RE.sub(_repl, text)
    return new, new != text


class Filter:
    # ★ file_handler / toggle は必ず class 直下のクラス属性として書く。
    #    load_function_module_by_id は module.Filter() というインスタンスを返し
    #    （backend/open_webui/utils/plugin.py:296-306）、以降の検出はすべて
    #    getattr(instance, ...) で行われる（utils/filter.py:173、utils/models.py:409）。
    #    モジュールトップレベルに書くと、エラーも警告も出ないまま無視される。
    #
    #    file_handler = True にすると utils/filter.py:229-233 が inlet 完了後に
    #    body['files'] と body['metadata']['files'] を **まとめて** 削除する。
    #    削除は Filter 単位ではなくパイプライン単位なので、動画以外の添付
    #    （PDF 等）も RAG に載らなくなる。toggle = True でユーザが明示的に
    #    ON にしたときだけ有効になるため、この副作用は許容している。
    file_handler = True
    toggle = True

    class Valves(BaseModel):
        priority: int = Field(default=0, description="Filter の実行順（昇順）")
        video_extensions: str = Field(
            default="mp4,mov,m4v,webm,mkv,avi",
            description="RAG 処理をスキップして Descript に回す拡張子（カンマ区切り）",
        )
        pipe_model_prefix: str = Field(
            default="descript_pipe",
            description="この接頭辞のモデルのときだけ Descript 固有の処理を行う。1 行で入力",
        )
        mcp_server_id: str = Field(
            default="",
            description="事前診断に使う Descript MCP の info.id。Pipe の同名 Valve と同じ値。1 行で入力",
        )
        preflight_mcp_check: bool = Field(
            default=True, description="LLM を呼ぶ前に MCP の OAuth 同意状況を検査する"
        )
        inject_project_hint: bool = Field(
            default=True, description="現在の対象プロジェクトを system メッセージに 1 行注入する"
        )
        redact_signed_urls: bool = Field(
            default=True, description="期限切れになる署名付き URL を恒久リンクに差し替える"
        )
        # ↓ 予約機能。現行の Pipe はマーカーを出力しないため通常は空振りする（契約書 §1.5）。
        strip_progress_markers: bool = Field(
            default=True, description="内部制御マーカー行をストリームから除去する（予約。現状 Pipe は出力しない）"
        )
        # 既定値の先頭は U+2063 INVISIBLE SEPARATOR（UTF-8 で e2 81 a3）。
        # 見た目に現れない文字なので、編集時に取り落とさないよう注意すること。
        progress_marker_prefix: str = Field(
            default="⁣DSC:", description="内部制御マーカーの接頭辞（予約）"
        )
        append_history_on_outlet: bool = Field(
            default=True, description="実行サマリを state の history に追記する"
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
        show_raw_job_json: bool = Field(default=False, description="デバッグ用。ジョブの生 JSON を本文に残す")

    def __init__(self):
        self.valves = self.Valves()

    # -- 共通の小物 -------------------------------------------------------

    def _matches_pipe(self, model_id: str) -> bool:
        prefix = (self.valves.pipe_model_prefix or "").strip()
        if not prefix:
            return True
        return bool(model_id) and model_id.startswith(prefix)

    def _user_valves(self, user: Any) -> "Filter.UserValves":
        """__user__['valves'] を安全に取り出す（未注入なら既定値）。"""
        valves = (user or {}).get("valves") if isinstance(user, dict) else None
        if isinstance(valves, self.UserValves):
            return valves
        if isinstance(valves, dict):
            try:
                return self.UserValves(**valves)
            except Exception:
                pass
        return self.UserValves()

    @staticmethod
    async def _emit(emitter, payload: dict) -> None:
        """__event_emitter__ が無い / 落ちても呼び出し側を壊さない。"""
        if not emitter:
            return
        try:
            await emitter(payload)
        except Exception:
            pass

    # -- inlet ------------------------------------------------------------

    async def inlet(
        self,
        body: dict,
        __user__=None,
        __request__=None,
        __metadata__=None,
        __model__=None,
        __event_emitter__=None,
    ) -> dict:
        # ★ ログレベルの適用は __init__ ではなくここで行う。Open WebUI は呼び出しの
        #    たびに function_module.valves をインスタンスごと差し替える
        #    （utils/filter.py:113-119）ため、__init__ では常に既定値しか読めない。
        _apply_log_level(self.valves.log_level)
        try:
            return await self._inlet(body, __user__, __request__, __metadata__, __model__, __event_emitter__)
        except Exception:
            # Filter の失敗でチャットを止めない。素通しする。
            # ただし黙って素通しすると原因が追えないので、ログには必ず残す。
            _LOGGER.exception("descript filter.inlet_crash")
            return body

    async def _inlet(self, body, user, request, metadata, model, emitter) -> dict:
        if not isinstance(body, dict):
            return body

        # ④ 対象モデルでなければ ①〜③ をすべてスキップする
        if not self._matches_pipe(_model_id(body, model)):
            _log_debug("filter.skip", model=_model_id(body, model))
            return body

        # ① 動画添付の RAG 迂回（最重要）
        #    file_handler = True により、この inlet が返った後に
        #    body['files'] / body['metadata']['files'] が削除される。
        #    削除される前に Descript 用の退避先へ移送しておく。
        self._stash_media(body)

        # ② 事前診断（MCP OAuth 同意済みか）
        await self._preflight(body, user, request, emitter)

        # ③ state のヒント注入
        if self.valves.inject_project_hint:
            await self._inject_hint(body, metadata)

        _log_info(
            "filter.inlet",
            chat_id=body.get("chat_id"),
            media=len(body.get(_MEDIA_KEY) or []),
            preflight=(body.get(_PREFLIGHT_KEY) or {}).get("code"),
        )
        return body

    def _stash_media(self, body: dict) -> None:
        extensions = _split_csv(self.valves.video_extensions)
        if not extensions:
            return

        files = body.get("files")
        if not isinstance(files, list):
            files = []
        meta = body.get("metadata")
        meta_files = meta.get("files") if isinstance(meta, dict) else None
        if isinstance(meta_files, list):
            # 同一ファイルが両方に載っていることがあるので id / name で重複排除する
            seen = {json.dumps(f, sort_keys=True, default=str) for f in files}
            for f in meta_files:
                key = json.dumps(f, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    files = files + [f]

        media = [f for f in files if _is_video_file(f, extensions)]
        if media:
            body[_MEDIA_KEY] = media
            # ファイル名は退避できたかの確認に要るが、URL は載せない（署名付きの場合がある）。
            # files 要素は {'name'...} と {'file': {'filename'...}} の 2 形態がある（_file_label 参照）。
            _log_info(
                "filter.stash_media",
                count=len(media),
                names=[
                    _pick(m, "name", "filename")
                    or _pick(m.get("file") if isinstance(m, dict) else None,
                             "filename", "name", default="?")
                    for m in media
                ],
            )

    async def _preflight(self, body: dict, user, request, emitter) -> None:
        if not self.valves.preflight_mcp_check:
            return
        server_id = (self.valves.mcp_server_id or "").strip()
        if not server_id or request is None:
            # server_id 未設定の環境では診断できない。空振りさせる。
            return

        user_id = (user or {}).get("id") if isinstance(user, dict) else getattr(user, "id", None)
        if not user_id:
            return

        manager = getattr(getattr(request, "app", None), "state", None)
        manager = getattr(manager, "oauth_client_manager", None)
        getter = getattr(manager, "get_oauth_token", None) if manager is not None else None
        if getter is None:
            return

        try:
            # OAuthClientManager.get_oauth_token(user_id, client_id, force_refresh=False)
            # → dict（access_token を含む）または None（utils/oauth.py:1019-1057）。
            #   期限切れは内部で refresh され、refresh 失敗時は None が返る。
            token = await getter(str(user_id), f"mcp:{server_id}")
        except Exception:
            # 診断そのものが失敗しても本処理は止めない
            return

        if token:
            return

        # 未同意。LLM を呼ぶ前に打ち切る。
        # inlet から例外を投げると Open WebUI 全体のエラー表示になり原因が
        # 分かりにくいので、body を書き換えて Pipe に伝える方式にする。
        message = "Descript との連携が未認可です。"
        await self._emit(
            emitter,
            {"type": "notification", "data": {"type": "error", "content": f"{message}\n{_OAUTH_HINT}"}},
        )
        await self._emit(
            emitter,
            {"type": "status", "data": {"description": message, "done": True}},
        )

        _log_warn("filter.preflight_blocked", code="MCP_OAUTH_REQUIRED", server_id=server_id)
        body[_PREFLIGHT_KEY] = {
            "ok": False,
            "code": "MCP_OAUTH_REQUIRED",
            "message_ja": message,
            "hint": _OAUTH_HINT,
        }
        messages = body.get("messages")
        if isinstance(messages, list):
            add_or_update_system_message(
                "[DESCRIPT_PREFLIGHT] code=MCP_OAUTH_REQUIRED\n"
                f"{message}\n{_OAUTH_HINT}\n"
                "Descript MCP を呼び出さず、この案内をそのままユーザに返してください。",
                messages,
                append=True,
            )

    async def _inject_hint(self, body: dict, metadata) -> None:
        chat_id = body.get("chat_id")
        if not chat_id and isinstance(metadata, dict):
            chat_id = metadata.get("chat_id")
        state = await _load_state(chat_id)
        if not state:
            return

        bits = []
        if state.get("project_name"):
            bits.append(f"プロジェクト「{state['project_name']}」")
        if state.get("project_id"):
            bits.append(f"project_id={state['project_id']}")
        if state.get("revision") is not None:
            bits.append(f"rev {state['revision']}")
        if state.get("awaiting"):
            bits.append(f"awaiting={state['awaiting']}")
        if not bits:
            return

        messages = body.get("messages")
        if not isinstance(messages, list):
            return
        # append=True で既存 system プロンプトの末尾に足す（本家の挙動を踏襲）。
        add_or_update_system_message("[Descript] 現在の対象: " + " / ".join(bits), messages, append=True)

    # -- stream -----------------------------------------------------------

    async def stream(self, event: dict, __user__=None, __event_emitter__=None) -> dict:
        # ★ 第 1 引数名は必ず event。utils/filter.py:124 が
        #   {'event': form_data} を渡すため、body という名前だと TypeError になる。
        #
        # __event_emitter__ は stream にも渡る（middleware.py:4218 の
        # filter_extra_params = {'__body__': form_data, **extra_params}、
        # extra_params は middleware.py:3766-3774 で構築され __event_emitter__ を含む）。
        # ただし event_emitter が無い経路（middleware.py:5598 以降のフォールバック）
        # では None が入るので、必ず None ガードして使う。
        try:
            return await self._stream(event, __event_emitter__)
        except Exception:
            return event

    async def _stream(self, event, emitter):
        # フォールバック経路では生の SSE 文字列が渡ることがある。dict 以外は素通し。
        if not isinstance(event, dict):
            return event

        choice, holder_key, content = _delta_holder(event)
        if content is None or not content:
            return event

        new_content = content
        payloads = []

        # ⑤ 進捗マーカーの除去（予約。現行 Pipe は出力しないので通常は空振りする。契約書 §1.5）
        if self.valves.strip_progress_markers:
            new_content, payloads = _strip_markers(new_content, self.valves.progress_marker_prefix or "")

        # ⑥ 署名 URL のライブマスキング
        if self.valves.redact_signed_urls:
            new_content = _redact_signed_urls(new_content)

        # ⑦ 何も変わらなかったチャンクは必ずそのまま返す
        if new_content == content and not payloads:
            return event

        # マーカーを status イベントへ振り替える（emitter が無ければ黙って捨てる）
        for payload in payloads:
            if not payload:
                continue
            await self._emit(
                emitter,
                {"type": "status", "data": {"description": _marker_label(payload), "done": False}},
            )

        if new_content:
            event["choices"][0][holder_key]["content"] = new_content
            return event

        # 本文が空になった。本文以外の情報を持たないチャンクだけ捨てる。
        # None を返すと middleware.py:5640 / 4269 の `if data:` で破棄される。
        if _is_droppable(event, choice, holder_key):
            return None

        event["choices"][0][holder_key]["content"] = ""
        return event

    # -- outlet -----------------------------------------------------------

    async def outlet(
        self,
        body: dict,
        __user__=None,
        __request__=None,
        __model__=None,
        __event_emitter__=None,
    ) -> dict:
        # ★ __chat_id__ / __message_id__ は要求しないこと。
        #   middleware.py:3505-3512 の extra_params に含まれていないため、
        #   宣言すると get_filter_params が値を渡せず呼び出しが失敗する。
        #   chat_id は body['chat_id'] から取る（middleware.py:3492）。
        _apply_log_level(self.valves.log_level)
        try:
            return await self._outlet(body, __user__)
        except Exception:
            # inlet と同じく、素通しはするがログには必ず残す。
            _LOGGER.exception("descript filter.outlet_crash")
            return body

    async def _outlet(self, body, user) -> dict:
        if not isinstance(body, dict):
            return body

        chat_id = body.get("chat_id")
        state = await _load_state(chat_id)

        # ★ 本文の書き換えは Descript を使っているチャットに限定する（契約書 §5.9 追加の防御）。
        #    outlet はチャット履歴の保存内容を不可逆に書き換えるため、
        #    chat.chat['descript'] が無いチャットには一切触れない。
        if not state:
            return body

        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []

        # ⑧ 署名付き URL の恒久化。
        #    Descript の download_url は署名付き・期限付きなので、履歴に残すと
        #    後日 403 になる。保存直前に走る outlet でしか差し替えられない。
        redacted = 0
        if self.valves.redact_signed_urls:
            share_url = state.get("share_url")
            replacement = str(share_url) if isinstance(share_url, str) and share_url else ""
            for message in messages:
                if not isinstance(message, dict):
                    continue
                new_content, changed = _redact_message_content(message.get("content"), replacement)
                if changed:
                    message["content"] = new_content
                    redacted += 1

        # ⑩ 生 JSON ブロックを畳む
        folded = 0
        if not self._user_valves(user).show_raw_job_json:
            for message in messages:
                if not isinstance(message, dict) or message.get("role") == "user":
                    continue
                new_content, changed = _fold_json_blocks(message.get("content"))
                if changed:
                    message["content"] = new_content
                    folded += 1

        # ⑨ history への追記。契約書 §3.3 のとおり、Filter に許された唯一の書き込み。
        #    state 不在のチャットは冒頭のゲートで既に return 済み。
        if self.valves.append_history_on_outlet and chat_id:
            try:
                await _append_history(
                    chat_id,
                    {
                        "op": "outlet",
                        "result": "success",
                        "note": (
                            f"messages={len(messages)} redacted={redacted} folded={folded} "
                            f"rev={state.get('revision')}"
                        ),
                    },
                )
            except Exception:
                pass

        if redacted or folded:
            _log_info(
                "filter.outlet",
                chat_id=chat_id,
                messages=len(messages),
                redacted=redacted,
                folded=folded,
            )
        return body
