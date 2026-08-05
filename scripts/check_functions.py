#!/usr/bin/env python3
"""functions/*.py の静的検証（読み取りのみ。ファイルを生成しない）。

Open WebUI の「1 Function = 自己完結 1 ファイル」制約により、共通ヘルパは
3 ファイルに重複展開されている。コピー元は docs/DetailedDesign/functions_contract.md
だが、片方だけ直すドリフトが起きやすい。本スクリプトはそれを検出する。

検査内容:
  1. 構文（ast.parse）
  2. frontmatter（1 行目が厳密に \"\"\" / 必須キー / requirements が空）
  3. Function クラスの種別と単一性
  4. クラス属性の位置（actions / file_handler / toggle）
  5. __init__ での self.valves 初期化
  6. Filter.stream の第 1 引数名が event
  7. outlet が要求してはいけない dunder
  8. Pipe が pipes() を定義していないこと
  9. replace_imports が壊す import / 相対 import
 10. Action が body['files'] を参照していないこと
 11. 共通ヘルパのドリフト（2 ファイル以上に存在するものはバイト単位で同一）
 12. 秘匿情報の直書き

使い方:
    python3 scripts/check_functions.py
    echo $?        # 0 = 問題なし / 1 = 問題あり
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNC_DIR = ROOT / "functions"

# plugin.py:156 と同じ
FM_LINE = re.compile(r"^\s*([a-z_]+):\s*(.*)\s*$", re.IGNORECASE)
# replace_imports() が素の文字列置換で壊す接頭辞（plugin.py:194-201）
DANGEROUS_IMPORT = re.compile(r"^\s*from\s+(utils|apps|main|config)\b", re.MULTILINE)
# public リポジトリに置いてはいけないもの
SECRETS = re.compile(
    r"sk-[A-Za-z0-9]{16,}|api\.descript\.com|descriptapi\.com|Bearer\s+[A-Za-z0-9._-]{12,}",
    re.IGNORECASE,
)

FUNCTION_CLASSES = ("Pipe", "Filter", "Action", "Event")

SPECS = {
    "descript_studio.py": {"cls": "Action", "class_attrs": ["actions"], "forbid": []},
    "descript_pipe.py": {"cls": "Pipe", "class_attrs": [], "forbid": ["pipes"]},
    "descript_guard.py": {"cls": "Filter", "class_attrs": ["file_handler", "toggle"], "forbid": []},
}

# 契約書 §5 の共通ヘルパ。2 ファイル以上に存在するものは同一でなければならない。
SHARED = [
    "DescriptError",
    "_pick",
    "_norm_key",
    "_esc",
    "_render",
    "_as_user_model",
    "_load_state",
    "_save_state",
    "_append_history",
    "_coerce_args",
    "_json_from_text",
    "_unwrap_mcp",
    "_classify_mcp_error",
    "_mcp_call",
    "_ok",
    "_fail",
    "_unpack_pipe_response",
    "_form_result",
    "_poll_job",
    "_redact_signed_urls",
    # §6.0.1 embeds 直後の最下部ピン留め
    "_emit_scroll_bottom",
    # §5.10 ロギング
    "_log",
    "_log_debug",
    "_log_info",
    "_log_warn",
    "_log_error",
    "_apply_log_level",
    "_ms",
]
SHARED_CONSTS = [
    "_STATE_KEY",
    "_STATE_VERSION",
    "_HISTORY_MAX",
    "_SIGNED_URL_RE",
    "_SIGNED_URL_PLACEHOLDER",
    "_LOG_VALUE_MAX",
    "_LOGGER",
    "_SCROLL_BOTTOM_JS",
]

problems: list[str] = []
oks: list[str] = []


def norm(src: str) -> str:
    """比較用の正規化。末尾空白と空行の差だけは無視する。"""
    return "\n".join(line.rstrip() for line in src.strip().splitlines() if line.strip())


def top_defs(tree: ast.Module, src: str) -> dict[str, str]:
    """モジュール直下の関数 / クラス / 定数のソースを名前で引けるようにする。"""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seg = ast.get_source_segment(src, node)
            if seg:
                out[node.name] = seg
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    seg = ast.get_source_segment(src, node)
                    if seg:
                        out[t.id] = seg
    return out


def check_file(path: Path, spec: dict) -> dict[str, str]:
    tag = path.name
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()

    # 1 / 2. frontmatter -------------------------------------------------
    if not lines or lines[0].rstrip() != '"""':
        problems.append(f'{tag}: 1 行目が厳密に """ ではない -> frontmatter が全無視される')
    else:
        keys, closed = [], False
        for line in lines[1:]:
            if '"""' in line:
                closed = True
                break
            m = FM_LINE.match(line)
            if m:
                keys.append(m.group(1).lower())
                if m.group(1).lower() == "requirements" and m.group(2).strip():
                    problems.append(f"{tag}: requirements が空でない -> 本番で pip 競合の恐れ")
            elif line.strip():
                problems.append(f"{tag}: frontmatter に解釈されない行: {line!r}")
        if not closed:
            problems.append(f'{tag}: frontmatter が """ で閉じられていない')
        for req in ("title", "version", "required_open_webui_version"):
            if req not in keys:
                problems.append(f"{tag}: frontmatter に {req} が無い")

    stem = path.stem
    if not stem.isidentifier() or stem != stem.lower():
        problems.append(f"{tag}: function_id として不正（isidentifier / 小文字）")

    # 9. import ------------------------------------------------------------
    for m in DANGEROUS_IMPORT.finditer(src):
        problems.append(f"{tag}: replace_imports に壊される import: {m.group(0).strip()!r}")

    # 12. 秘匿情報 ----------------------------------------------------------
    for m in SECRETS.finditer(src):
        problems.append(f"{tag}: 秘匿情報/エンドポイントの直書き: {m.group(0)[:40]!r}")

    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            problems.append(f"{tag}: 相対 import は解決されない (行 {node.lineno})")

    # 3. Function クラス ----------------------------------------------------
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    found = [c.name for c in classes if c.name in FUNCTION_CLASSES]
    if found != [spec["cls"]]:
        problems.append(f"{tag}: Function クラスが {found}。{spec['cls']} のみであるべき")
    cls = next((c for c in classes if c.name == spec["cls"]), None)
    if cls is None:
        problems.append(f"{tag}: class {spec['cls']} が無い")
        return {}

    # 4. クラス属性 ---------------------------------------------------------
    cls_attrs = set()
    for node in cls.body:
        if isinstance(node, ast.Assign):
            cls_attrs |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            cls_attrs.add(node.target.id)

    mod_names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            mod_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    for attr in spec["class_attrs"]:
        if attr in cls_attrs:
            oks.append(f"{tag}: {attr} はクラス属性")
        elif attr in mod_names:
            problems.append(
                f"{tag}: {attr} がモジュールレベル。plugin.py:296-306 はインスタンスを返すため無視される"
            )
        else:
            problems.append(f"{tag}: {attr} が class {spec['cls']} 直下に無い")

    methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    # 8. 禁止メソッド -------------------------------------------------------
    for banned in spec["forbid"]:
        if banned in methods:
            problems.append(f"{tag}: {banned}() を定義してはいけない")

    # 5. self.valves --------------------------------------------------------
    if "__init__" not in methods:
        problems.append(f"{tag}: __init__ が無い -> Valves が注入されない")
    elif "self.valves" not in (ast.get_source_segment(src, methods["__init__"]) or ""):
        problems.append(f"{tag}: __init__ で self.valves を初期化していない")

    # 6. stream -------------------------------------------------------------
    if "stream" in methods:
        args = [a.arg for a in methods["stream"].args.args]
        if len(args) < 2 or args[1] != "event":
            problems.append(f"{tag}: stream の第 1 引数名が {args[1:2]}。'event' でないと TypeError")
        else:
            oks.append(f"{tag}: stream(self, event, ...)")

    # 7. outlet -------------------------------------------------------------
    if "outlet" in methods:
        bad = {a.arg for a in methods["outlet"].args.args} & {"__chat_id__", "__message_id__"}
        if bad:
            problems.append(f"{tag}: outlet が {sorted(bad)} を要求。middleware.py:3505-3512 に無い")

    # 10. Action の body['files'] -------------------------------------------
    if spec["cls"] == "Action" and re.search(r"""body\s*(\[\s*|\.\s*get\s*\(\s*)["']files["']""", src):
        problems.append(f"{tag}: body['files'] を参照。Chat.svelte:2192-2199 は送らない")

    return top_defs(tree, src)


def cross_check() -> None:
    """ファイル間の名前の整合を検査する。

    並列に実装すると、Action が送る op を Pipe が知らない / Filter が書く
    body キーを Pipe が読まない、といったズレが静かに生まれる。
    """
    try:
        studio = (FUNC_DIR / "descript_studio.py").read_text(encoding="utf-8")
        pipe = (FUNC_DIR / "descript_pipe.py").read_text(encoding="utf-8")
        guard = (FUNC_DIR / "descript_guard.py").read_text(encoding="utf-8")
    except OSError:
        return

    # --- descript_op: Action が送るものを Pipe が扱えるか --------------------
    sent = set(re.findall(r'_call_pipe\(\s*["\']([a-z_]+)["\']', studio))
    handled: set[str] = set()
    for pat in (
        r'op\s*==\s*["\']([a-z_]+)["\']',
        r'["\']([a-z_]+)["\']\s*:\s*self\._op_',
        r"def _op_([a-z_]+)\(",
    ):
        handled |= set(re.findall(pat, pipe))
    unknown = sent - handled
    if unknown:
        problems.append(f"Pipe が扱えない descript_op を Action が送っている: {sorted(unknown)}")
    elif sent:
        oks.append(f"descript_op: Action が送る {len(sent)} 種すべてを Pipe が扱える")

    # --- エラーコード: Action が扱うものを Pipe が出しうるか ------------------
    code_re = r'["\']((?:MCP|TOOL|JOB|QUOTA|RATE|UI|USER|NO)_[A-Z_]+|INTERNAL)["\']'
    action_codes = set(re.findall(code_re, studio))
    pipe_codes = set(re.findall(code_re, pipe))
    orphan = action_codes - pipe_codes
    if orphan:
        problems.append(f"Pipe が出さないエラーコードを Action が扱っている: {sorted(orphan)}")
    elif action_codes:
        oks.append(f"エラーコード: Action の {len(action_codes)} 種はすべて Pipe が出しうる")

    # --- Filter -> Pipe の受け渡しキー（契約書 §1.4）------------------------
    for key in ("descript_preflight", "descript_media"):
        in_guard = key in guard
        in_pipe = key in pipe
        if in_guard and not in_pipe:
            problems.append(
                f"Filter が body['{key}'] を書くのに Pipe が読んでいない。"
                " docs/DetailedDesign/functions_contract.md §1.4 を参照"
            )
        elif in_guard and in_pipe:
            oks.append(f"Filter -> Pipe の受け渡し {key}: 両側に存在")

    # --- Action の pipe_model_id 既定値が Pipe の function_id と一致するか ----
    m = re.search(r'pipe_model_id[^=]*=\s*Field\(\s*default=["\']([^"\']+)["\']', studio)
    if m and m.group(1) != "descript_pipe":
        problems.append(
            f"Action の pipe_model_id 既定値 {m.group(1)!r} が Pipe の function_id 'descript_pipe' と不一致"
        )
    elif m:
        oks.append("Action の pipe_model_id 既定値は Pipe の function_id と一致")

    # --- Filter と Pipe の mcp_server_id Valve が両方に存在するか -------------
    if "mcp_server_id" in pipe and "mcp_server_id" not in guard:
        problems.append("Filter に mcp_server_id Valve が無い（OAuth 事前診断ができない）")


def main() -> int:
    if not FUNC_DIR.is_dir():
        print(f"NG  {FUNC_DIR} が存在しません")
        return 1

    defs_by_file: dict[str, dict[str, str]] = {}
    for name, spec in SPECS.items():
        path = FUNC_DIR / name
        if not path.exists():
            problems.append(f"{name}: ファイルが無い")
            continue
        try:
            defs_by_file[name] = check_file(path, spec)
        except SyntaxError as exc:
            problems.append(f"{name}: 構文エラー {exc}")

    # 11. 共通ヘルパのドリフト ---------------------------------------------
    for helper in SHARED + SHARED_CONSTS:
        present = {f: d[helper] for f, d in defs_by_file.items() if helper in d}
        if len(present) < 2:
            continue
        bodies = {f: norm(s) for f, s in present.items()}
        uniq = set(bodies.values())
        if len(uniq) == 1:
            oks.append(f"共通ヘルパ {helper}: {len(present)} ファイルで一致")
        else:
            groups: dict[str, list[str]] = {}
            for f, b in bodies.items():
                groups.setdefault(b, []).append(f)
            desc = " vs ".join("+".join(sorted(g)) for g in groups.values())
            problems.append(
                f"共通ヘルパ {helper} がドリフトしている: {desc}。"
                " docs/DetailedDesign/functions_contract.md §5 を正として揃えること"
            )

    # 13. ファイル間の名前の整合 ---------------------------------------------
    cross_check()

    print("--- 確認できたこと ---")
    for o in oks:
        print("  OK  " + o)
    print()
    if problems:
        print("--- 問題 ---")
        for p in problems:
            print("  NG  " + p)
        print(f"\n{len(problems)} 件の問題があります。")
        return 1
    print("問題は見つかりませんでした。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
