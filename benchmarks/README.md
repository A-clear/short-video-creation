# benchmarks

CodSpeed で継続的に計測する性能テスト。`functions/` の 3 つの Function と
`scripts/check_functions.py` のうち、リクエストごとに必ず通る CPU 処理を対象にしている。

| ファイル                        | 対象                                                                        |
| ------------------------------- | --------------------------------------------------------------------------- |
| `test_bench_mcp_payloads.py`    | MCP 応答の解釈（`_json_from_text` / `_unwrap_mcp` / `_coerce_args` ほか）    |
| `test_bench_orchestration.py`   | ツール名の正規表現解決、編集プロンプトの合成、チャット意図の判定             |
| `test_bench_text_rendering.py`  | 署名 URL の置換、生 JSON の折り畳み、embeds の HTML 組み立て                 |
| `test_bench_filter_pipeline.py` | Filter の inlet / stream / outlet（チャンク 201 件・メッセージ 25 件を投入） |
| `test_bench_static_check.py`    | `scripts/check_functions.py` の静的検証一式（3 ファイル 6,600 行超）         |

`functions/*.py` は Open WebUI の中で exec される前提で `open_webui.*` を import するため、
`_harness.py` が I/O を持たない最小のスタブを `sys.modules` に差し込んでから読み込む。
入力データは `_data.py` に集約してあり、計測対象に生成コストが混ざらないようにしている。

## 実行方法

```bash
uv sync --group dev
uv run pytest benchmarks/ --codspeed          # 計測のみ（ローカル）
codspeed run --mode simulation -- uv run pytest benchmarks/ --codspeed   # CodSpeed へ送る
```

`--codspeed` を付けない場合、ベンチマークは 1 回だけ実行される（回帰の有無だけを見る使い方）。
