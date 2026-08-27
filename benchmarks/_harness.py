"""ベンチマークから functions/*.py を読み込むためのローダ。

functions/ 配下の 3 ファイルは Open WebUI の中で exec される前提の
「自己完結 1 ファイル」であり、モジュール先頭で open_webui.* を import する。
ベンチマークは Open WebUI 本体（submodule）無しで走らせたいので、
import 時に解決が必要なシンボルだけを最小のスタブで差し替える。

スタブは I/O を一切行わない。計測対象はあくまで Functions 側の
純粋な CPU 処理（JSON 整形 / 正規表現 / HTML 生成 / 静的検証）である。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS_DIR = ROOT / "functions"
SCRIPTS_DIR = ROOT / "scripts"


class UserModel:
    """open_webui.models.users.UserModel の最小スタブ。"""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", "")
        self.name = kwargs.get("name", "")
        self.email = kwargs.get("email", "")
        self.role = kwargs.get("role", "user")
        self.__dict__.update(kwargs)


class ChatModel:
    """Chats が返す行オブジェクトの最小スタブ。"""

    def __init__(self, chat_id: str, user_id: str, chat: dict, title: str = "New Chat") -> None:
        self.id = chat_id
        self.user_id = user_id
        self.chat = chat
        self.title = title


class Chats:
    """所有者検証つきのチャット取得 / 更新をインメモリで再現する。

    実装は DB アクセスだが、ベンチマークで測りたいのは呼び出し側の
    state マージ処理なので、辞書で置き換えて I/O を排除する。
    """

    store: dict[tuple[str, str], ChatModel] = {}

    @classmethod
    def seed(cls, chat_id: str, user_id: str, chat: dict, title: str = "動画編集") -> ChatModel:
        row = ChatModel(chat_id, user_id, dict(chat), title)
        cls.store[(chat_id, user_id)] = row
        return row

    @classmethod
    async def get_chat_by_id_and_user_id(cls, chat_id: str, user_id: str):
        return cls.store.get((chat_id, user_id))

    @classmethod
    async def update_chat_by_id(cls, chat_id: str, chat: dict, touch: bool = True):
        for key, row in cls.store.items():
            if key[0] == chat_id:
                row.chat = chat
                return row
        return None


def _unsupported(name: str):
    def _call(*_args: Any, **_kwargs: Any):
        raise RuntimeError(f"{name} はベンチマークでは呼び出さない")

    return _call


def add_or_update_system_message(content: str, messages: list, append: bool = False) -> list:
    """open_webui.utils.misc.add_or_update_system_message と同じ挙動。"""
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        base = messages[0].get("content")
        if isinstance(base, list):
            for item in base:
                if isinstance(item, dict) and item.get("type") == "text":
                    item["text"] = (
                        f'{item.get("text", "")}\n{content}' if append else f'{content}\n{item.get("text", "")}'
                    )
        else:
            messages[0]["content"] = f"{base}\n{content}" if append else f"{content}\n{base}"
    else:
        messages.insert(0, {"role": "system", "content": content})
    return messages


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def install_open_webui_stubs() -> None:
    """sys.modules に open_webui.* の最小スタブを登録する（冪等）。"""
    if "open_webui" in sys.modules:
        return

    package = _module("open_webui")
    package.__path__ = []  # type: ignore[attr-defined]
    modules = {
        "open_webui": package,
        "open_webui.models": _module("open_webui.models"),
        "open_webui.models.chats": _module("open_webui.models.chats", Chats=Chats),
        "open_webui.models.users": _module("open_webui.models.users", UserModel=UserModel),
        "open_webui.models.config": _module("open_webui.models.config", Config=object),
        "open_webui.models.files": _module("open_webui.models.files", Files=object),
        "open_webui.models.prompts": _module("open_webui.models.prompts", Prompts=object),
        "open_webui.storage": _module("open_webui.storage"),
        "open_webui.storage.provider": _module("open_webui.storage.provider", Storage=object),
        "open_webui.utils": _module("open_webui.utils"),
        "open_webui.utils.chat": _module(
            "open_webui.utils.chat", generate_chat_completion=_unsupported("generate_chat_completion")
        ),
        "open_webui.utils.middleware": _module(
            "open_webui.utils.middleware", connect_mcp_server=_unsupported("connect_mcp_server")
        ),
        "open_webui.utils.misc": _module(
            "open_webui.utils.misc", add_or_update_system_message=add_or_update_system_message
        ),
    }
    for name, mod in modules.items():
        mod.__path__ = getattr(mod, "__path__", [])  # type: ignore[attr-defined]
        sys.modules[name] = mod


def _load(path: Path, alias: str) -> types.ModuleType:
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 環境異常時のみ
        raise ImportError(f"{path} を読み込めません")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 読み込み途中のモジュールを残すと、次の import が中途半端な状態を掴む
        sys.modules.pop(alias, None)
        raise
    return module


def load_function(name: str) -> types.ModuleType:
    """functions/<name>.py を読み込む。

    ロガーは計測対象外なので黙らせる（出力の有無で結果がぶれないようにする）。
    """
    install_open_webui_stubs()
    module = _load(FUNCTIONS_DIR / f"{name}.py", f"bench_functions_{name}")
    logger = getattr(module, "_LOGGER", None)
    if isinstance(logger, logging.Logger):
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
    return module


def load_script(name: str) -> types.ModuleType:
    """scripts/<name>.py を読み込む（標準ライブラリのみに依存）。"""
    return _load(SCRIPTS_DIR / f"{name}.py", f"bench_scripts_{name}")
