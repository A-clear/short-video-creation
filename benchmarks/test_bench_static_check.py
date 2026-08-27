"""scripts/check_functions.py（Functions の静的検証）のベンチマーク。

3 ファイル合計 6,600 行超を ast で解析し、共通ヘルパのドリフト検査まで
行う。開発中に最も頻繁に叩かれるスクリプトで、実行時間がそのまま
フィードバックの速さになる。
"""

from __future__ import annotations

import ast
from pathlib import Path

from _harness import load_script

check = load_script("check_functions")

SOURCES = {
    name: (check.FUNC_DIR / name).read_text(encoding="utf-8") for name in check.SPECS
}


def _reset() -> None:
    """モジュールグローバルの検出結果を空に戻す（累積させない）。"""
    check.problems.clear()
    check.oks.clear()


def test_check_functions_full_run(benchmark):
    """静的検証をひととおり（frontmatter / ast / ドリフト / 相互参照）走らせる。"""

    def run():
        _reset()
        defs_by_file = {
            name: check.check_file(check.FUNC_DIR / name, spec) for name, spec in check.SPECS.items()
        }
        for helper in check.SHARED + check.SHARED_CONSTS:
            present = {f: d[helper] for f, d in defs_by_file.items() if helper in d}
            if len(present) < 2:
                continue
            {f: check.norm(s) for f, s in present.items()}
        check.cross_check()
        return len(check.problems), len(check.oks)

    problems, oks = benchmark(run)
    assert problems == 0
    assert oks > 0


def test_parse_functions(benchmark):
    """ast.parse だけを切り出した下限（検証ロジックの取り分を見るため）。"""

    def run():
        return [ast.parse(source) for source in SOURCES.values()]

    trees = benchmark(run)
    assert len(trees) == 3


def test_top_defs_extraction(benchmark):
    """モジュール直下の定義をソース断片として取り出す（ドリフト検査の前段）。"""
    parsed = {name: (ast.parse(source), source) for name, source in SOURCES.items()}

    def run():
        return {name: check.top_defs(tree, source) for name, (tree, source) in parsed.items()}

    defs = benchmark(run)
    assert defs["descript_pipe.py"]


def test_helper_drift_comparison(benchmark):
    """3 ファイルに重複展開された共通ヘルパの一致検査。"""
    parsed = {name: check.top_defs(ast.parse(source), source) for name, source in SOURCES.items()}

    def run():
        matched = 0
        for helper in check.SHARED + check.SHARED_CONSTS:
            bodies = {name: check.norm(defs[helper]) for name, defs in parsed.items() if helper in defs}
            if len(bodies) >= 2 and len(set(bodies.values())) == 1:
                matched += 1
        return matched

    matched = benchmark(run)
    assert matched > 10


def test_cross_check(benchmark):
    """ファイル間の名前の整合（op / エラーコード / 受け渡しキー）。"""

    def run():
        _reset()
        check.cross_check()
        return len(check.problems)

    assert benchmark(run) == 0


def test_read_and_parse_from_disk(benchmark):
    """読み込みを含む実運用そのままの経路。"""
    paths = [Path(check.FUNC_DIR / name) for name in check.SPECS]

    def run():
        return [ast.parse(path.read_text(encoding="utf-8")) for path in paths]

    assert len(benchmark(run)) == 3
