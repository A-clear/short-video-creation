# 日本語向け Basic RAG（Tokenizer + Reranker）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open WebUI の Basic RAG に、日本語特化の OSS リランクモデルとトークナイザを組み込み、チャンク長の単位をリランカーの窓に揃える。

**Architecture:** リランクエンジンは SentenceTransformers（Open WebUI プロセス内の `CrossEncoder`）。リランカー本体は別コンテナにできない（エンジンの選択肢が `''` と `external` の 2 つしかないため）。代わりに `reranker-model-init` という一回きりの compose サービスを足し、モデルの事前取得と**ロード検証**を担わせる。これによりモデルのロード失敗が「Hybrid Search が黙って off に戻る」ではなく `docker compose up` の明示的なエラーとして現れる。テキスト分割は `token_transformers` にし、リランカーと同じトークナイザでチャンク長を数える。

**Tech Stack:** Docker Compose / Open WebUI v0.11.0 系（FastAPI, Python 3.11）/ sentence-transformers 5.5.1 / transformers 5.5.4 / PGVector / `hotchpotch/japanese-reranker-small-v2`

**Spec:** `docs/superpowers/specs/2026-08-28-japanese-rag-reranker-design.md`

## Global Constraints

このリポジトリのルール（`.claude/rules/`）が本計画のすべてのタスクに適用される。**スキルの既定手順よりリポジトリのルールが優先する。**

- **git のミューテート系コマンドを実行しない。** `git commit` / `git push` / `git merge` / `git rebase` / `git reset` / `git checkout -b` は使用禁止。参照系（`git status` / `git log` / `git diff` / `git show`）のみ可。**そのため本計画に「コミット」ステップは存在しない。** 各タスクの最後は検証ステップで終わる
- **テストコードを作成しない。実行しない。** 単体テスト・結合テストは明確な指示がない限り書かない。**そのため本計画は TDD の形を取らない。** 検証は構文検査（`py_compile`）・設定検査（`docker compose config -q`）・整合性 grep・`mcp-mermaid` による描画検証で行う
- **ビルド・デプロイコマンドを実行しない。** `docker compose build` / `docker compose up` / `docker compose restart` は実行せず、手順書に記載して利用者に委ねる。`docker compose config` は解析のみで副作用が無いため可
- **`python` / `pip` の実行前に venv を有効化する。** リポジトリ直下に `.venv` がある（Python 3.13.3）: `source .venv/bin/activate`
- **`npm` ではなく `pnpm` を使う**（本計画では該当なし）
- **このリポジトリは public。** API キー・エンドポイント・実在するプロジェクト名を平文でコミットしない
- ドキュメント・コメントは日本語

### 確定している値

| 項目                                    | 値                                                                                |
| --------------------------------------- | --------------------------------------------------------------------------------- |
| モデル ID（リランク・トークナイザ共通） | `hotchpotch/japanese-reranker-small-v2`                                           |
| リランクエンジン                        | SentenceTransformers（`RAG_RERANKING_ENGINE=` 空文字）                            |
| バックエンド                            | `torch`（`optimum` 未同梱のため `onnx` は不可）                                   |
| テキスト分割                            | `token_transformers`（管理画面の「トークン（Transformers）」）                    |
| チャンク                                | `CHUNK_SIZE=440` / `CHUNK_OVERLAP=64`                                             |
| 検索                                    | `RAG_TOP_K=40` / `RAG_TOP_K_RERANKER=20` / `RAG_RELEVANCE_THRESHOLD=0.0`          |
| ハイブリッド検索                        | `ENABLE_RAG_HYBRID_SEARCH=true` / `RAG_HYBRID_BM25_WEIGHT=0`                      |
| スレッド上限                            | `OMP_NUM_THREADS=1`（ホストが 4 コア、ワーカーが 4）                              |
| HF キャッシュ                           | `/app/backend/data/cache/embedding/models`（公式イメージ `Dockerfile:95` と同値） |
| init サービスのイメージ                 | `ghcr.io/open-webui/open-webui:main`（**open-webui 本体と同一**）                 |

### ⚠️ spec からの訂正 1 件

spec §5.1 は init サービスで `entrypoint:` を上書きすると書いているが、**公式イメージは `ENTRYPOINT` を持たず `CMD [ "bash", "start.sh"]` だけである**（`full-stack/open-webui/Dockerfile:219`）。したがって `command:` の上書きで足りる。本計画は `command:` を使う。

---

## File Structure

| ファイル                                                | 責務                                                                                      | Task |
| ------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ---- |
| `scripts/init_reranker_model.py`                        | モデルの事前取得とロード検証（新規）。compose の init サービスからのみ実行される          | 1    |
| `docker-compose.yml`                                    | `reranker-model-init` サービスの定義と、`open-webui` の `depends_on` / `environment` 配線 | 2    |
| `.env.example`                                          | §13 に RAG 設定 19 変数、§19 に compose 用 2 変数                                         | 3    |
| `docs/RequirementDefinition/TA/system_architecture.mmd` | 構成図に RAG 層（Docling）とプロセス内リランカーを追加                                    | 4    |
| `docs/Setup/multi_user_setup.md`                        | セットアップ順序のステップ 3 / 14 を更新し、再インデックス手順を追加                      | 5    |
| `.claude/CLAUDE.md`                                     | Basic RAG 節とディレクトリ図を更新                                                        | 6    |

依存関係: Task 1 → Task 2 → Task 3 の順（後段が前段の名前を参照する）。Task 4 / 5 / 6 は Task 3 の後なら順不同。

---

## Task 1: モデル事前取得・ロード検証スクリプト

**Files:**

- Create: `scripts/init_reranker_model.py`

**Interfaces:**

- Consumes: なし（このタスクが最初）
- Produces:
  - ファイルパス `scripts/init_reranker_model.py`（Task 2 が `./scripts/init_reranker_model.py:/scripts/init_reranker_model.py:ro` としてマウントし、`command: ["python", "/scripts/init_reranker_model.py"]` で実行する）
  - 読み取る環境変数（Task 2 が同じ名前で渡す）: `RERANKER_MODEL_ID` / `SENTENCE_TRANSFORMERS_HOME` / `SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND` / `SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION` / `RAG_RERANKING_MODEL_TRUST_REMOTE_CODE`
  - 終了コード: `0` 成功 / `1` 取得またはロードの失敗 / `2` 必須環境変数の欠落

- [ ] **Step 1: スクリプトを作成する**

`scripts/init_reranker_model.py` に以下をそのまま書く。

```python
#!/usr/bin/env python3
"""リランクモデルの事前取得とロード検証。

docker-compose.yml の reranker-model-init サービスから、Open WebUI 本体と
**同じイメージ**で実行される。役割は 2 つある。

1. 事前取得 — モデルを open-webui-data ボリューム上の HuggingFace キャッシュへ
   入れる。これが無いと 4 つの uvicorn ワーカーが起動時に同時ダウンロードを始める
   （main.py:624 の get_rf は lifespan の中で走るため、遅延ロードではない）。

2. ロード検証 — 実際に CrossEncoder を構築して predict まで通す。
   Open WebUI 本体はモデルのロードに失敗しても HTTP エラーを返さず、
   ENABLE_RAG_HYBRID_SEARCH を黙って false へ戻すだけである
   （routers/retrieval.py:1185-1187）。管理者から見えるのは
   「保存したのにトグルが off に戻っている」という症状だけで、原因は
   log.error の 1 行にしか出ない。ここで先に失敗させることで、
   それを docker compose up 時点の明示的なエラーへ変える。

⚠️ Open WebUI 本体と同じ条件でロードすること。trust_remote_code や backend が
   違うと検証の意味が無くなる。参照元は routers/retrieval.py:204-222。

⚠️ device は "cpu" 固定。本体は env.py:45-65 で検出した DEVICE_TYPE を使うが、
   これは環境変数ではなく実行時の検出結果であり、外から同じ値を渡せない。
   本構成は CUDA を持たないため cpu で一致する。GPU を導入する場合はここも直すこと。
"""

from __future__ import annotations

import os
import sys


def _env_flag(name: str, default: str) -> bool:
    """Open WebUI と同じ真偽値の読み方（config.py / env.py 全体で共通）。"""
    return os.getenv(name, default).strip().lower() == "true"


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(
            f"[init-reranker] {name} が空です。docker-compose.yml の "
            f"reranker-model-init サービスの environment を確認してください。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


def main() -> int:
    model_id = _require("RERANKER_MODEL_ID")
    cache_dir = _require("SENTENCE_TRANSFORMERS_HOME")

    # 本体と同じ既定値に揃える（env.py:1065-1067 / env.py:1082-1083 / config.py:1026）。
    backend = os.getenv("SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND", "").strip() or "torch"
    sigmoid = _env_flag("SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION", "True")
    trust_remote_code = _env_flag("RAG_RERANKING_MODEL_TRUST_REMOTE_CODE", "True")

    print(
        f"[init-reranker] model={model_id} backend={backend} "
        f"sigmoid={sigmoid} trust_remote_code={trust_remote_code}",
        flush=True,
    )

    from huggingface_hub import snapshot_download

    # 1. キャッシュを先に見る。2 回目以降は HuggingFace が落ちていても成功する。
    #    ここを local_files_only=True から始めるのが肝で、compose を上げ直すたびに
    #    ネットワークへ出る構成にしてはいけない。
    try:
        path = snapshot_download(repo_id=model_id, cache_dir=cache_dir, local_files_only=True)
        print(f"[init-reranker] cache hit: {path}", flush=True)
    except Exception:
        print("[init-reranker] cache miss. downloading from HuggingFace ...", flush=True)
        path = snapshot_download(repo_id=model_id, cache_dir=cache_dir)
        print(f"[init-reranker] downloaded: {path}", flush=True)

    # 2. 本体と同じ条件で CrossEncoder を構築する（routers/retrieval.py:204-222）。
    #    get_model_path はスナップショットの絶対パスを本体へ渡すので、ここでも path を使う。
    import sentence_transformers
    import torch

    model = sentence_transformers.CrossEncoder(
        path,
        device="cpu",
        trust_remote_code=trust_remote_code,
        backend=backend,
        activation_fn=torch.nn.Sigmoid() if sigmoid else None,
    )

    scores = model.predict([("動作確認のクエリ", "これは動作確認用のダミー文書です。")])
    score = float(scores[0])
    print(f"[init-reranker] reranker ready. sample score={score:.4f}", flush=True)

    # 3. トークナイザも同じスナップショットから読めることを確かめる。
    #    RAG_TOKENIZER_MODEL は同じモデル ID を指し、本体は
    #    AutoTokenizer.from_pretrained で読む（routers/retrieval.py:1589-1594）。
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        path,
        local_files_only=True,
        trust_remote_code=trust_remote_code,
    )
    token_count = len(tokenizer.encode("トークナイザの動作確認"))
    print(f"[init-reranker] tokenizer ready. sample={token_count} tokens", flush=True)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 失敗理由をそのまま compose のログへ出す
        print(f"[init-reranker] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 2: 構文を検査する**

```bash
cd /home/a-style/Astyle/short-video-creation
source .venv/bin/activate
python -m py_compile scripts/init_reranker_model.py && echo OK
```

期待: `OK` が出る。エラーが出たら構文を直す。

> ⚠️ `.venv` は Python 3.13 で、コンテナ側は 3.11 である。スクリプトは `from __future__ import annotations` を使い 3.12 以降の構文を含まないため、この検査で構文互換性を確認できる。**実行はしない**（`sentence_transformers` / `torch` はローカル venv に無く、実行するとモデルのダウンロードが走るため）。

- [ ] **Step 3: 読み取る環境変数が本体と同じ名前であることを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
for v in SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND \
         SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION \
         RAG_RERANKING_MODEL_TRUST_REMOTE_CODE; do
  printf '%-62s script=%s upstream=%s\n' "$v" \
    "$(grep -c "\"$v\"" scripts/init_reranker_model.py)" \
    "$(grep -rc "^$v = " full-stack/open-webui/backend/open_webui/env.py full-stack/open-webui/backend/open_webui/config.py | awk -F: '{s+=$2} END {print s}')"
done
```

期待: 3 行とも `script=1` かつ `upstream=1`。`upstream=0` の行があれば、上流で変数名が変わっているので spec §3.4 の表とスクリプトの両方を直す。

- [ ] **Step 4: 秘匿情報が入っていないことを確認する**

```bash
grep -nE "sk-|api[_-]?key|password|secret" scripts/init_reranker_model.py || echo "OK: 秘匿情報なし"
```

期待: `OK: 秘匿情報なし`。

---

## Task 2: compose に init サービスを追加し、open-webui を配線する

**Files:**

- Modify: `docker-compose.yml`
  - `docling-serve` サービスの直後（現行 393 行目付近、`open-terminal:` の直前）に新サービスを挿入
  - `open-webui` の `depends_on`（現行 655-679 行目付近）
  - `open-webui` の `environment`（現行 682 行目以降）

**Interfaces:**

- Consumes: Task 1 の `scripts/init_reranker_model.py` と、それが読む 5 つの環境変数名
- Produces:
  - compose サービス名 `reranker-model-init`（コンテナ名 `svc-reranker-model-init`）
  - 環境変数 `RERANKER_MODEL_ID`（既定 `hotchpotch/japanese-reranker-small-v2`）と `OPEN_WEBUI_OMP_NUM_THREADS`（既定 `1`）— Task 3 が `.env.example` §19 に記載する

- [ ] **Step 1: `reranker-model-init` サービスを追加する**

`docker-compose.yml` の `docling-serve` サービス定義の直後（`  # ----` で始まる Open Terminal のコメントブロックの直前）に、次をそのまま挿入する。

```yaml
# ----------------------------------------------------------------------------
# リランクモデルの事前取得と検証（一回きり）
# ----------------------------------------------------------------------------
# リランカー本体はコンテナではありません。Open WebUI の Reranking Engine の
# 選択肢は「Default (SentenceTransformers)」と「External」の 2 つだけで
# （Documents.svelte:1214-1215）、前者を選ぶ以上モデルは Open WebUI の
# プロセス内で動きます。このサービスが担うのは次の 2 点です。
#
#   1. 事前取得 — open-webui-data ボリューム上の HuggingFace キャッシュへ
#      モデルを入れる。これが無いと 4 つの uvicorn ワーカーが起動時に
#      同時ダウンロードを始めます（main.py:624 の get_rf は lifespan の中で
#      走るため、遅延ロードではありません）。
#
#   2. ロード検証 — 実際に CrossEncoder を構築して predict まで通す。
#      ⚠️ Open WebUI はモデルのロードに失敗しても HTTP エラーを返さず、
#      ENABLE_RAG_HYBRID_SEARCH を黙って false へ戻すだけです
#      （routers/retrieval.py:1185-1187）。管理者から見えるのは
#      「保存したのにトグルが off に戻っている」という症状だけで、
#      原因は log.error の 1 行にしか出ません。ここで先に失敗させることで、
#      docker compose up 時点の明示的なエラーに変えています。
#
# ⚠️ image は open-webui 本体と同じものにしてください。huggingface_hub /
#    sentence-transformers / transformers のバージョンとキャッシュの
#    レイアウトが一致します。別イメージにすると「init は成功したのに
#    ワーカーがモデルを見つけられない」が起きます。同じイメージなので
#    追加の pull も発生しません。
#
# ⚠️ 公式イメージは ENTRYPOINT を持たず CMD [ "bash", "start.sh"] だけです
#    （Dockerfile:219）。したがって command の上書きで足ります。
reranker-model-init:
  image: ghcr.io/open-webui/open-webui:main
  container_name: svc-reranker-model-init
  restart: "no"
  environment:
    # ⚠️ 公式イメージの Dockerfile:95 と同じ値。ここがずれるとワーカーが
    #    キャッシュを見つけられません。
    SENTENCE_TRANSFORMERS_HOME: /app/backend/data/cache/embedding/models
    HF_HOME: /app/backend/data/cache/embedding/models
    RERANKER_MODEL_ID: ${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}
    # 本体と同じ条件でロードを検証するため、同じ値を渡します。
    SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND: ${SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND:-torch}
    SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION: ${SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION:-true}
    RAG_RERANKING_MODEL_TRUST_REMOTE_CODE: ${RAG_RERANKING_MODEL_TRUST_REMOTE_CODE:-false}
    # モデルのダウンロードとロードは 1 コアで足ります。
    OMP_NUM_THREADS: "1"
  command: ["python", "/scripts/init_reranker_model.py"]
  volumes:
    - open-webui-data:/app/backend/data
    - ./scripts/init_reranker_model.py:/scripts/init_reranker_model.py:ro
  # ⚠️ 一回きりで終了するため専用ネットワークは切っていません。必要な通信は
  #    HuggingFace への egress だけで、svc-net の他サービス（postgres /
  #    redis / minio）は使いません。
  networks: [svc-net]
```

- [ ] **Step 2: `open-webui` の `depends_on` に待ち合わせを追加する**

`open-webui` サービスの `depends_on:` ブロックの中、`docling-serve:` の項目の直後に次を挿入する。

```yaml
# モデルの取得と検証が終わるまで起動しない。ここだけ service_started ではなく
# service_completed_successfully を使う（一回きりのジョブなので healthy にならない）。
#
# ⚠️ 初回だけ HuggingFace への到達性が必須になります。オフラインで
#    起動したい場合はこの 2 行を消し、.env の
#    RAG_RERANKING_MODEL_AUTO_UPDATE を true に戻してください
#    （各ワーカーが自分で取得するようになります）。
reranker-model-init:
  condition: service_completed_successfully
```

- [ ] **Step 3: `open-webui` の `environment` にモデル ID とスレッド上限を追加する**

`open-webui` の `environment:` ブロックで、`DOCLING_SERVER_URL: http://docling-serve:5001` の行の直後に次を挿入する。

```yaml
#
# リランクとトークナイザで同じモデルを使う。
# ⚠️ ここが食い違うと、チャンクがリランカーの 512 トークン窓に収まらなくなり、
#    リランカーは各チャンクの先頭しか読まなくなります。エラーは出ません。
#    1 つの変数から両方を導くことでその食い違いを構造的に防いでいます。
# ⚠️ ※PersistentConfig です。既に一度起動した環境ではここを変えても
#    無視されるので、Admin Settings → Documents で直してください。
RAG_RERANKING_MODEL: ${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}
RAG_TOKENIZER_MODEL: ${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}
#
# ⚠️ torch は既定でホストの全コアを使おうとします。Open WebUI 側に上限を
#    設ける仕組みはありません（イメージにも backend にも OMP_NUM_THREADS /
#    torch.set_num_threads の設定が存在しないことを確認済み）。
#    ホストは 4 コアなので、4 ワーカー × 1 スレッド = 4 に揃えます。
#    2 にすると 8 スレッドになり、Docling や Computer と取り合って
#    かえって遅くなります。
#    これは素の環境変数なので、起動済みの環境でも再起動で反映されます。
OMP_NUM_THREADS: ${OPEN_WEBUI_OMP_NUM_THREADS:-1}
MKL_NUM_THREADS: ${OPEN_WEBUI_OMP_NUM_THREADS:-1}
```

- [ ] **Step 4: compose の構文と変数解決を検査する**

```bash
cd /home/a-style/Astyle/short-video-creation
docker compose config -q && echo "OK: 構文と変数解決に問題なし"
```

期待: `OK: 構文と変数解決に問題なし`。YAML の崩れや未定義変数があればここで落ちる。

- [ ] **Step 5: 解決後の値が意図どおりか確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
docker compose config | grep -A 2 "RAG_RERANKING_MODEL\|RAG_TOKENIZER_MODEL\|RERANKER_MODEL_ID" | grep -E "RAG_RERANKING_MODEL|RAG_TOKENIZER_MODEL|RERANKER_MODEL_ID"
```

期待: 3 つとも同じ値（既定なら `hotchpotch/japanese-reranker-small-v2`）が出る。**1 つでも違えば Step 1 か Step 3 の変数名を間違えている。**

- [ ] **Step 6: init サービスがマウントとイメージを正しく持つことを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
docker compose config | sed -n '/^  reranker-model-init:/,/^  [a-z]/p' | grep -E "image:|source:|target:|read_only:|restart:|command:"
```

期待:

- `image:` が `open-webui` サービスと同じ `ghcr.io/open-webui/open-webui:main`
- `open-webui-data` と `scripts/init_reranker_model.py` の 2 マウントがある
- スクリプトのマウントが `read_only: true`
- `restart: "no"`

---

## Task 3: `.env.example` に RAG 設定を書く

**Files:**

- Modify: `.env.example`
  - §13「Basic RAG」の「チャンク分割」小節（現行 418-432 行目）と「検索」小節（現行 434-436 行目）
  - §19「docker-compose 用」の末尾付近（現行 950 行目以降）

**Interfaces:**

- Consumes: Task 2 が定義した `RERANKER_MODEL_ID` / `OPEN_WEBUI_OMP_NUM_THREADS` の変数名と既定値
- Produces: 利用者が `.env` へ写す値の一覧。Task 5 の手順書がこの節番号を参照する

- [ ] **Step 1: §13 の「チャンク分割」小節を差し替える**

`.env.example` の以下のブロック（`# --- チャンク分割（Admin Settings → Documents に対応）---` から `CHUNK_OVERLAP=200` まで）を、次でまるごと置き換える。

```
# --- チャンク分割（Admin Settings → Documents に対応）---
#
# ⚠️ **チャンク長はリランカーのトークナイザで数えます。** リランカーは
#    クエリとチャンクを同時に読んでスコアを付ける部品なので、チャンクが
#    リランカーの窓（512 トークン）を超えると**先頭だけ読んで残りを黙って
#    切り捨てます**。エラーは出ません。単位を揃えるため token（tiktoken）から
#    token_transformers へ変更しています。
#
# ⚠️ RAG_TOKENIZER_MODEL が効くのは token_transformers のときだけです
#    （routers/retrieval.py:1608-1615）。管理画面の表示名は
#    「トークン（Transformers）」で、Tokenizer Model の入力欄はこの値を
#    選んだときだけ現れます。
#
# ⚠️ **MeCab 系のトークナイザは使えません。** tohoku-nlp/bert-base-japanese 系は
#    fugashi と unidic-lite を要求しますが、backend/requirements.txt に
#    含まれていないため AutoTokenizer がインポートエラーで落ちます。
#    SentencePiece ベースのモデルを選んでください。
#
# ⚠️ docker-compose.yml の open-webui サービスが RERANKER_MODEL_ID から
#    この値を上書きします。ここを使うのは単体起動するときだけです。
RAG_TEXT_SPLITTER=token_transformers
RAG_TOKENIZER_MODEL=hotchpotch/japanese-reranker-small-v2

# token（tiktoken）を選んだときだけ参照されます。token_transformers では
# 未使用ですが、戻せるように残してあります。
TIKTOKEN_ENCODING_NAME=cl100k_base

# 見出しで先に区切ってからチャンク化する。（config.py:1039。既定 true）
ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER=true

# ⚠️ 単位は「リランカーのトークン」であって文字数でも tiktoken でもありません。
#    クロスエンコーダはクエリと文書を [CLS] クエリ [SEP] 文書 [SEP] という
#    **1 本の系列**にして 512 トークンの窓に入れます。したがって文書側に使える
#    予算は 512 − 特殊トークン(3) − クエリのトークン数です。日本語の質問文を
#    60 トークン程度と見積もって 440 にしています。日本語で 1 チャンクおよそ
#    650 文字になります（変更前は tiktoken 2000 = 約 1,800 文字）。
#    ⚠️ オーバーラップは 1 チャンクの長さに加算されないので、この計算には
#    入りません。CHUNK_OVERLAP を増やしても窓を圧迫しません。
#
# ⚠️ **この値を変えたら既存ナレッジベースの再インデックスが要ります。**
#    設定を変えても既存のベクトルは作り直されません。新旧のチャンクが
#    同じコレクションに混ざると、切り詰められる長いチャンクと収まる短い
#    チャンクが同じ土俵で比較され、スコアが歪みます。エラーは出ません。
CHUNK_SIZE=440
CHUNK_OVERLAP=64
```

- [ ] **Step 2: §13 の「検索」小節を差し替える**

`.env.example` の `# --- 検索 ---` から `RAG_TOP_K=15` までを、次でまるごと置き換える。

```
# --- 検索とリランキング ---
#
# ⚠️ **リランカーはハイブリッド検索が有効なときしか呼ばれません。**
#    RerankCompressor が現れるのは query_doc_with_native_hybrid_search
#    （retrieval/utils.py:434）と query_doc_with_hybrid_search の 2 経路だけで、
#    通常の query_doc には存在しません。false のままだと、以下の設定を
#    どれだけ正しく書いても再スコアは一度も走りません。
ENABLE_RAG_HYBRID_SEARCH=true

# ⚠️ **日本語では語彙検索が機能しないため 0 にしています。**
#    PGVector の native hybrid search は to_tsvector('simple', ...) を使いますが
#    （retrieval/vector/dbs/pgvector.py:566-578）、simple は空白と記号で区切って
#    小文字化するだけで、形態素解析も N-gram 分割もしません。日本語の文は
#    丸ごと 1 トークンになるため、完全一致しない限りヒットしません。
#    フォールバック経路の BM25Retriever も preprocess_func を渡していないので
#    LangChain 既定の str.split()、つまり同じく空白区切りです。
#    設定で差し替える口はありません。
#
#    そこで「ハイブリッド検索はリランカーを起動するためのスイッチとして使い、
#    語彙側には重みを配らない」という形にしています。
#
#    0 にしても結果が空になることはありません。統合は RRF による**和集合**で
#    あって積集合ではなく（retrieval/vector/utils.py:36-90）、さらに
#    `if bm25_weight > 0` のガードがあるので（pgvector.py:562）
#    **FTS の SQL がそもそも発行されません**。
RAG_HYBRID_BM25_WEIGHT=0

# リランクエンジン。空文字 = SentenceTransformers（Open WebUI のプロセス内で
# CrossEncoder を動かす）。管理画面の表示は「Default (SentenceTransformers)」。
# 選択肢はこれと external の 2 つだけです（Documents.svelte:1214-1215）。
RAG_RERANKING_ENGINE=

# ⚠️ **空にしてはいけません。** get_rf は `if reranking_model:` で早期に None を
#    返し（routers/retrieval.py:173 / 176 / 240）、RerankCompressor は reranking_function が
#    None のとき**埋め込みベースの再スコアへ静かにフォールバックします**
#    （retrieval/utils.py:1743-1758）。全候補に対して毎クエリ OpenAI の
#    embeddings が追加で叩かれ、精度は上がらないのに課金だけ増えます。
#    ログにも異常は出ません。
#
# ⚠️ docker-compose.yml が RERANKER_MODEL_ID からこの値を上書きします。
#    RAG_TOKENIZER_MODEL と必ず同じ値にしてください。
RAG_RERANKING_MODEL=hotchpotch/japanese-reranker-small-v2

# CrossEncoder に渡すバッチサイズ。（config.py:1028。既定 32）
# ⚠️ local 分岐でのみ使われます（retrieval/utils.py:1243-1245）。
#    CPU 実行なので控えめにしています。
RAG_RERANKING_BATCH_SIZE=16

# 取得するチャンク数 = リランカーへ渡す候補プール。（config.py:952。既定 3）
# ⚠️ **コレクションごとの取得数です。** ナレッジベースを 3 つ束ねたモデルでは
#    候補は 3×40 になり、再スコアも 3 回発生します。
RAG_TOP_K=40

# リランク後に LLM のコンテキストへ渡す件数。（config.py:953。既定 3）
# 選別比は 40:20 = 2:1。実測して余裕があれば RAG_TOP_K を 60 まで上げて
# よい（精度は上がる方向）。遅ければ RAG_TOP_K を 24 まで下げる。
RAG_TOP_K_RERANKER=20

# スコアのしきい値。（config.py:954。既定 0.0）
# ⚠️ 既定で activation_fn=torch.nn.Sigmoid() が適用されるため、スコアの尺度は
#    (0,1) です（env.py:1082-1083）。シグモイドは単調増加なので**並び順は
#    変わりません**。しきい値を入れるときだけこの尺度を意識してください。
RAG_RELEVANCE_THRESHOLD=0.0

# --- リランクモデルの取得と読み込み（PersistentConfig ではない素の環境変数）---
#
# ⚠️ **この 5 つは PersistentConfig ではありません。** 起動済みの環境でも
#    .env の変更が再起動で反映されます。上の RAG_* とは扱いが違います。

# ⚠️ **false は reranker-model-init サービスと対になっています。**
#    get_model_path は local_files_only = not update_model で動くので
#    （retrieval/utils.py:1666）、false にすると 4 つのワーカーは一度も
#    HuggingFace に触らず、init が用意したキャッシュだけを見ます。
#    init サービスを外すなら true に戻してください。
RAG_RERANKING_MODEL_AUTO_UPDATE=false

# トークナイザ側の local_files_only を決めます（routers/retrieval.py:1583）。
# 同じスナップショットに入っているので同様に締められます。
RAG_EMBEDDING_MODEL_AUTO_UPDATE=false

# ⚠️ true は「リポジトリ内の任意の Python コードを実行してよい」という意味です。
#    hotchpotch/japanese-reranker-small-v2 は config.json / model.safetensors /
#    tokenizer.* / special_tokens_map.json / training_args.bin / README.md だけで
#    .py を含まないこと、ModernBERT が transformers 4.48 以降でネイティブ対応
#    （本イメージは 5.5.4）であることを確認済みなので false にしています。
#    別のモデルへ変える場合は、そのリポジトリに .py が無いか確認してください。
RAG_RERANKING_MODEL_TRUST_REMOTE_CODE=false
RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE=false

# CrossEncoder のバックエンド。（env.py:1065-1067。既定 torch）
# ⚠️ **onnx / openvino は選べません。** sentence-transformers がそれらを
#    解決するには optimum[onnxruntime] が必要ですが、backend/requirements.txt に
#    あるのは onnxruntime だけで optimum は入っていません。公式イメージを
#    使う方針である以上 torch 固定です。
SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND=torch
```

- [ ] **Step 3: §19 に compose 用の 2 変数を追加する**

`.env.example` §19 の docling-serve ブロックの直後（`DOCLING_SERVE_CPU_LIMIT=4.0` の次の空行の後）に、次を挿入する。

```
# --- リランクモデル（13 節の Basic RAG で使う）-----------------------------
# ⚠️ この 1 つの変数が 3 箇所を駆動します。
#      ・reranker-model-init サービスが取得・検証するモデル
#      ・open-webui の RAG_RERANKING_MODEL
#      ・open-webui の RAG_TOKENIZER_MODEL
#    後ろ 2 つが食い違うと、チャンクがリランカーの窓に収まらなくなります
#    （無症状のまま精度が落ちます）。
#
# ⚠️ 変更したら既存ナレッジベースの再インデックスが要ります（13 節参照）。
#    軽くしたい場合の候補は hotchpotch/japanese-reranker-xsmall-v2
#    （36.8M パラメータ / JQaRA 0.7403。small-v2 は 70.2M / 0.7633）。
RERANKER_MODEL_ID=hotchpotch/japanese-reranker-small-v2

# Open WebUI コンテナの torch スレッド上限（OpenMP / MKL）。
# ⚠️ torch は既定でホストの全コアを使おうとし、Open WebUI 側に上限を設ける
#    仕組みはありません。UVICORN_WORKERS（6 節）× この値 が再スコア時の
#    最大スレッド数になります。ホストのコア数を超えないようにしてください。
OPEN_WEBUI_OMP_NUM_THREADS=1
```

- [ ] **Step 4: compose の既定値と `.env.example` の値が一致することを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
grep -n "RERANKER_MODEL_ID\|OPEN_WEBUI_OMP_NUM_THREADS" .env.example docker-compose.yml
```

期待: `.env.example` の `RERANKER_MODEL_ID=hotchpotch/japanese-reranker-small-v2` と、`docker-compose.yml` 内の 3 箇所の `${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}`（init サービス / `RAG_RERANKING_MODEL` / `RAG_TOKENIZER_MODEL`） が同じモデル ID を指している。`OPEN_WEBUI_OMP_NUM_THREADS` は `.env.example` が `1`、compose の既定が `:-1`。**食い違っていたら直す。**

- [ ] **Step 5: リランクとトークナイザのモデル ID が一致することを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
grep -E "^RAG_RERANKING_MODEL=|^RAG_TOKENIZER_MODEL=|^RERANKER_MODEL_ID=" .env.example
```

期待: 3 行とも同じモデル ID。これは spec §5.1 の「1 変数で 3 箇所を駆動する」制約の静的な確認である。

- [ ] **Step 6: 秘匿情報が入っていないことを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
git diff -- .env.example | grep -E "^\+.*(sk-[A-Za-z0-9]|=[A-Za-z0-9+/]{32,})" || echo "OK: 実値の混入なし"
```

期待: `OK: 実値の混入なし`。

- [ ] **Step 7: compose 全体が引き続き解決できることを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
docker compose config -q && echo "OK"
```

期待: `OK`。

---

## Task 4: 構成図に RAG 層を追加する

**Files:**

- Modify: `docs/RequirementDefinition/TA/system_architecture.mmd`

**Interfaces:**

- Consumes: なし（図はコードを参照しない）
- Produces: なし（他タスクは図を参照しない）

**背景:** 現在の図には Docling が載っていない（前回の変更時の積み残し）。今回の対象である RAG 層をまとめて反映する。リランカーは独立したコンテナではないため、デプロイ単位のノードとしては置かず、破線で区別する。`reranker-model-init` は起動時のみ存在するため図には含めない（この図は常時稼働の構成を表す）。

- [ ] **Step 1: `ContentExtraction` サブグラフを追加する**

`Data` サブグラフの閉じ `end` の直後（`user <--> |アクセス| internet` の行の前）に、次を挿入する。

```
    subgraph ContentExtraction["Content Extraction（文書抽出）"]
        direction LR
        docling@{ img: "https://api.iconify.design/mdi:file-document-outline.svg", label: "Docling", w: 60, h: 60, constraint: "on" }
    end
```

- [ ] **Step 2: プロセス内リランカーを `Vercel Compute` に追加する**

`Vercel Compute` サブグラフの中、`codex@{ ... }` の行の直後に次を挿入する。

```
            reranker@{ img: "https://api.iconify.design/mdi:sort-variant.svg", label: "Reranker (in-process)", w: 60, h: 60, constraint: "on" }
```

- [ ] **Step 3: エッジを 2 本追加する**

`open-webui <--> |ベクトル検索| pgvector` の行の直後に、次の 2 行を挿入する。

```
    open-webui <--> |文書のコンテンツ抽出（PDF / Office → Markdown）| docling
    open-webui -.-> |検索結果の再スコア（日本語クロスエンコーダ。別コンテナではなく同一プロセス内）| reranker
```

- [ ] **Step 4: `mcp-mermaid` で描画を検証する**

`mcp__mcp-mermaid__generate_mermaid_diagram` に `.mmd` の中身（先頭の `---` frontmatter を含む全文）を渡し、エラーなく描画されることを確認する。

期待: 図が生成される。パースエラーが出たら、`@{ ... }` のノード定義かサブグラフの `end` の対応を疑う。

> ⚠️ この図は Mermaid v11 のアイコン形状構文（`node@{ img: ..., label: ..., w: ..., h: ... }`）を使っている。既存ノードと同じ書式を崩さないこと。

- [ ] **Step 5: サブグラフと `end` の対応を数える**

```bash
cd /home/a-style/Astyle/short-video-creation
printf 'subgraph=%s end=%s\n' \
  "$(grep -c '^\s*subgraph' docs/RequirementDefinition/TA/system_architecture.mmd)" \
  "$(grep -c '^\s*end\s*$' docs/RequirementDefinition/TA/system_architecture.mmd)"
```

期待: 2 つの数が一致する（追加前は 7 / 7、追加後は 8 / 8）。

---

## Task 5: セットアップ手順書を更新する

**Files:**

- Modify: `docs/Setup/multi_user_setup.md`
  - ステップ 3「ビルドする」（現行 100-104 行目）
  - ステップ 14「Basic RAG を設定し、…」（現行 199-242 行目）

**Interfaces:**

- Consumes: Task 3 が定めた `.env.example` §13 / §19 の変数名と値
- Produces: なし

- [ ] **Step 1: ステップ 3 に init サービスの注意を足す**

ステップ 3 の `docker compose build` のコードブロックの直後に、次の引用ブロックを挿入する。

```
   > ⚠️ **ビルド対象は 2 つのままである。** リランクモデルはイメージに焼き込まず、
   > `reranker-model-init` サービスが実行時に取得する。このサービスは
   > `ghcr.io/open-webui/open-webui:main` をそのまま使うため追加のビルドは要らない。
```

- [ ] **Step 2: ステップ 14 (a) の設定表を差し替える**

ステップ 14 の「**(a) 抽出エンジンと分割・検索の設定**」にある表を、次でまるごと置き換える。

```
    | 項目                      | 値                                                                             |
    | ------------------------- | ------------------------------------------------------------------------------ |
    | Content Extraction Engine | Docling / `http://docling-serve:5001` / API キーは `.env` の `DOCLING_API_KEY` |
    | Docling Parameters        | `{"do_ocr":true,"ocr_engine":"tesseract","ocr_lang":["jpn","eng"],"pdf_backend":"dlparse_v4","table_mode":"accurate"}` |
    | Text Splitter             | **トークン（Transformers）**                                                   |
    | Tokenizer Model           | **`hotchpotch/japanese-reranker-small-v2`**                                    |
    | Markdown Header Splitting | On                                                                             |
    | Chunk Size / Overlap      | **440 / 64**                                                                   |
    | Hybrid Search             | **On**                                                                         |
    | BM25 Weight               | **0**                                                                          |
    | Reranking Engine          | **Default (SentenceTransformers)**                                             |
    | Reranking Model           | **`hotchpotch/japanese-reranker-small-v2`**（Tokenizer Model と同じ値）        |
    | Reranking Batch Size      | **16**                                                                         |
    | Top K                     | **40**                                                                         |
    | Top K Reranker            | **20**                                                                         |
    | Relevance Threshold       | **0.0**                                                                        |
    | Embedding Model           | `text-embedding-3-large`（OpenAI）                                             |
```

- [ ] **Step 3: ステップ 14 (a) の直後に、リランキング固有の警告 4 つを足す**

ステップ 14 (a) の既存の警告ブロック群（`⚠️ do_ocr: true は独自イメージが前提。` から `⚠️ RAG_EMBEDDING_MODEL を後から変えてはいけない。` まで）の直後に、次を挿入する。

```
    > ⚠️ **保存後に Hybrid Search が off に戻っていないか必ず確認する。** Open WebUI は
    > リランクモデルのロードに失敗しても HTTP エラーを返さず、`ENABLE_RAG_HYBRID_SEARCH`
    > を false へ戻すだけである（`routers/retrieval.py:1185-1187`）。原因は
    > `docker compose logs open-webui | grep 'Error loading reranking model'` にしか出ない。
    > `reranker-model-init` サービスは、この失敗を `docker compose up` の時点で
    > 起こすために存在する。init が成功していればここで戻ることは無い。

    > ⚠️ **Reranking Model を空にしてはいけない。** `get_rf` は `if reranking_model:` で
    > 早期に None を返し（`routers/retrieval.py:173 / 176 / 240`）、`RerankCompressor` は
    > 埋め込みベースの再スコアへ静かにフォールバックする（`retrieval/utils.py:1743-1758`）。
    > 全候補に対して毎クエリ OpenAI の embeddings が追加で叩かれ、
    > **精度は上がらないのに課金だけ増える。** エラーもログの警告も出ない。

    > ⚠️ **Reranking Model と Tokenizer Model は必ず同じ値にする。** 前者はチャンクを
    > 読む側、後者はチャンクを切る側で、食い違うとチャンクがリランカーの 512 トークン窓に
    > 収まらなくなる。リランカーは各チャンクの先頭しか読まなくなるが、エラーは出ない。
    > `.env` では `RERANKER_MODEL_ID` 1 つから両方を導いてこれを防いでいる。

    > ⚠️ **BM25 Weight を 0 より大きくしない。** PGVector の native hybrid search は
    > `to_tsvector('simple', ...)` を使うが（`retrieval/vector/dbs/pgvector.py:566-578`）、
    > `simple` は空白と記号で区切って小文字化するだけで、日本語の文は丸ごと 1 トークンに
    > なる。フォールバック経路の BM25Retriever も `preprocess_func` を渡していないため
    > 同じく空白区切りである。**日本語では語彙検索が機能しないので、重みを配った分だけ
    > ベクトル検索の順位が薄まって精度が落ちる。** ハイブリッド検索を on にしているのは
    > リランカーを起動するためであって、語彙検索のためではない。
```

- [ ] **Step 4: ステップ 14 に (c) として再インデックス手順を足す**

ステップ 14 の「**(b) ナレッジベースの作成と grant**」の末尾（`⚠️ 一般ユーザの**利用**は塞がっていない。` の引用ブロックの後）に、次を挿入する。

```
    **(c) 既存ナレッジベースの再インデックス（既存環境のみ）**

    チャンク分割の設定を変えても、既存のベクトルは作り直されない。新旧のチャンクが同じ
    コレクションに混在すると、**リランカーが 512 トークンで切り詰める長いチャンクと、
    収まる短いチャンクが同じ土俵で比較される。** スコアが歪むが、エラーは出ない。

    各ナレッジベースについて、既存ファイルを削除してから再アップロードする。

    - 管理者がチームへ配った read grant は、ナレッジベース自体を消さない限り維持される
    - `RAG_EMBEDDING_MODEL` は変更しないため、ベクトルの次元は変わらない。コレクションを
      作り直す必要はない
    - 目安として、チャンク数は 2〜3 倍に増える（tiktoken 2000 トークン ≒ 1,800 文字 に対し、
      SentencePiece 440 トークン ≒ 650 文字）
```

- [ ] **Step 5: ステップ 2 の `.env` 更新指示に 13 節の追加分を反映する**

ステップ 2 の「最低限、次の節を埋める:」の文中、`13 節（`DOCLING_API_KEY`。`openssl rand -hex 32`）` を次に置き換える。

```
13 節（`DOCLING_API_KEY`。`openssl rand -hex 32`。加えてリランクとチャンク分割の設定一式）、19 節（`RERANKER_MODEL_ID` / `OPEN_WEBUI_OMP_NUM_THREADS`）
```

- [ ] **Step 6: 手順書と `.env.example` の値が食い違っていないことを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
for pair in "440:CHUNK_SIZE" "64:CHUNK_OVERLAP" "40:RAG_TOP_K" "20:RAG_TOP_K_RERANKER" "16:RAG_RERANKING_BATCH_SIZE"; do
  val="${pair%%:*}"; key="${pair##*:}"
  printf '%-26s env=%s doc=%s\n' "$key" \
    "$(grep -c "^${key}=${val}$" .env.example)" \
    "$(grep -c "\*\*${val}" docs/Setup/multi_user_setup.md)"
done
grep -c "hotchpotch/japanese-reranker-small-v2" docs/Setup/multi_user_setup.md
```

期待: 各行の `env=1`（`.env.example` に該当行が 1 本ある）。`doc=` は手順書の表に太字で現れる回数で、0 でなければよい。最後の行は 2 以上（Tokenizer Model と Reranking Model の 2 箇所）。

- [ ] **Step 7: 手順書のステップ番号が壊れていないことを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
grep -n "^[0-9]\+\. \*\*" docs/Setup/multi_user_setup.md | head -20
```

期待: セットアップの順序が 1〜16 まで連番のまま。**新しい番号付きステップは追加していない**（既存のステップ 3 と 14 の中身を厚くしただけ）ので、番号は変わらない。

---

## Task 6: `.claude/CLAUDE.md` を更新する

**Files:**

- Modify: `.claude/CLAUDE.md`
  - ディレクトリ図の `scripts/` 相当箇所（現行 8-33 行目のツリー）
  - 「### Basic RAG（Embedding = OpenAI / 抽出 = Docling / ベクトル DB = PGVector）」節（現行 121 行目以降）

**Interfaces:**

- Consumes: Task 1〜3 で確定したファイルパスと変数名
- Produces: なし

- [ ] **Step 1: 節の見出しと冒頭を差し替える**

`### Basic RAG（Embedding = OpenAI / 抽出 = Docling / ベクトル DB = PGVector）` の見出し行と、その直後の段落を次で置き換える。

```
### Basic RAG（Embedding = OpenAI / 抽出 = Docling / ベクトル DB = PGVector / リランク = SentenceTransformers）

抽出・埋め込み・ベクトル DB の 3 点は**マルチユーザだから必要になる**選択で、独立した好みではない。既定の SentenceTransformers（埋め込み）は**ワーカーあたり約 500MB**、既定の pypdf は継続的な取り込みでメモリリーク、既定の ChromaDB は SQLite ベースで fork-safe ではない。ここに日本語特化のリランクとトークナイザ（`hotchpotch/japanese-reranker-small-v2`）が加わる。設定は `.env.example` §13 に集約し、判断の根拠は `docs/superpowers/specs/2026-08-27-multi-user-design.md` §4.7 と `docs/superpowers/specs/2026-08-28-japanese-rag-reranker-design.md` が正本。
```

- [ ] **Step 2: 既存の箇条書きの末尾にリランク固有の 5 点を足す**

同節の箇条書き（`- **ナレッジベースは管理者が作り…**` から始まり `- **`RAG_EMBEDDING_MODEL` を後から変えない。**…` で終わる）の末尾に、次を追加する。

```
- **リランカーはハイブリッド検索が有効なときしか呼ばれない。** `RerankCompressor` が現れるのは `query_doc_with_native_hybrid_search`（`retrieval/utils.py:434`）と `query_doc_with_hybrid_search` の 2 経路だけで、通常の `query_doc` には存在しない。そのうえで **`RAG_HYBRID_BM25_WEIGHT=0`** にしている — PGVector の native hybrid search は `to_tsvector('simple', ...)` を使い（`retrieval/vector/dbs/pgvector.py:566-578`）、`simple` は空白区切りなので**日本語の文は丸ごと 1 トークンになる**。フォールバックの `BM25Retriever` も `preprocess_func` 未指定で同じ。設定で差し替える口は無い。ハイブリッドは reranker を起動するためのスイッチとして使っている。0 なら `if bm25_weight > 0` のガード（`pgvector.py:562`）で FTS の SQL 自体が発行されない
- **モデルのロード失敗は「Hybrid Search が勝手に off に戻る」として現れる。** `routers/retrieval.py:1185-1187` が例外を握って `ENABLE_RAG_HYBRID_SEARCH = False` にするだけで、HTTP エラーを返さない。原因は `log.error` の 1 行にしか出ない。compose の **`reranker-model-init`** サービスは、この失敗を `docker compose up` 時点の明示的なエラーへ前倒しするために存在する。**このサービスの image は `open-webui` 本体と同一でなければならない**（`huggingface_hub` のバージョンとキャッシュのレイアウトを一致させるため）
- **`RAG_RERANKING_MODEL` を空にすると、エラーではなく「別のもの」が動く。** `get_rf` が `None` を返し（`routers/retrieval.py:173 / 176 / 240`）、`RerankCompressor` が**埋め込みベースの再スコアへ静かにフォールバックする**（`retrieval/utils.py:1743-1758`）。全候補に対して毎クエリ OpenAI の embeddings が追加で叩かれ、精度は上がらないのに課金だけ増える
- **`RAG_RERANKING_MODEL` と `RAG_TOKENIZER_MODEL` は同じ値でなければならない。** 前者はチャンクを読む側、後者は `RAG_TEXT_SPLITTER=token_transformers` のときチャンクを切る側（`routers/retrieval.py:1608-1615`）。食い違うとチャンクがリランカーの 512 トークン窓に収まらず、先頭しか読まれない。エラーは出ない。`docker-compose.yml` では **`RERANKER_MODEL_ID` 1 つから両方を導いて**構造的に防いでいる
- **モデルはワーカー起動時に、ワーカーごとに読まれる。** `main.py:624` の `get_rf` は lifespan の中で走るため遅延ロードではなく、`UVICORN_WORKERS=4` なら 4 プロセスが各自モデルを保持する（重みだけで 280.6MB × 4）。torch は既定でホストの全コアを使おうとし Open WebUI 側に上限は無いので、compose で **`OMP_NUM_THREADS=1`**（ホスト 4 コア ÷ ワーカー 4）を渡している。`onnx` バックエンドは `optimum` 未同梱のため選べない
```

- [ ] **Step 3: ディレクトリ図に新規スクリプトを足す**

冒頭のディレクトリ図で `functions/` ブロックの後、`full-stack/open-webui/` の行の前に、次を挿入する。

```
scripts/                    運用スクリプト
  check_functions.py        Functions の静的検証
  init_reranker_model.py    リランクモデルの事前取得とロード検証
                            （compose の reranker-model-init から実行）
  computer-urls.sh          Computer 3 台のアクセス URL を出す
```

- [ ] **Step 4: 記述と実ファイルの整合を確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
for f in scripts/init_reranker_model.py scripts/check_functions.py scripts/computer-urls.sh; do
  test -f "$f" && echo "OK  $f" || echo "NG  $f （CLAUDE.md に書いたファイルが存在しない）"
done
grep -c "reranker-model-init" .claude/CLAUDE.md docker-compose.yml
```

期待: 3 つとも `OK`。`grep -c` は `.claude/CLAUDE.md` が 2（本文 2 箇所）以上、`docker-compose.yml` が 2 以上（サービス定義と `depends_on`）。

- [ ] **Step 5: 「ビルドが要るのは 2 つ」の記述が残っていることを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
grep -n "ビルドが要るのは\|ビルドするのは\|ビルド対象" .claude/CLAUDE.md docs/Setup/multi_user_setup.md docker-compose.yml
```

期待: いずれも **Computer と Docling の 2 つ**のままであること。この変更でビルド対象は増えないので、**3 つに書き換えてはいけない**。

---

## 最終確認（全タスク完了後）

- [ ] **Step 1: compose 全体が解決できる**

```bash
cd /home/a-style/Astyle/short-video-creation
docker compose config -q && echo "OK"
```

- [ ] **Step 2: モデル ID が全ファイルで一致している**

```bash
cd /home/a-style/Astyle/short-video-creation
grep -rn "japanese-reranker" --include="*.yml" --include="*.example" --include="*.md" --include="*.py" . \
  | grep -v "docs/superpowers/" | grep -v "xsmall\|tiny\|base-v2"
```

期待: 出てくるモデル ID がすべて `hotchpotch/japanese-reranker-small-v2`。spec と plan（`docs/superpowers/`）は比較表を含むので除外している。

- [ ] **Step 3: Functions は無傷である**

```bash
cd /home/a-style/Astyle/short-video-creation
source .venv/bin/activate
python scripts/check_functions.py && echo "OK: Functions に影響なし"
```

期待: 終了コード 0。本計画は `functions/` を触らないので、ここが落ちるなら別の原因（フォーマッタによる共通ヘルパのドリフト等）である。

- [ ] **Step 4: 変更範囲が計画どおりであることを確認する**

```bash
cd /home/a-style/Astyle/short-video-creation
git status --short
```

期待: 次の 6 ファイルのみ。これ以外が出ていたら意図しない変更である。

```
?? scripts/init_reranker_model.py
 M docker-compose.yml
 M .env.example
 M docs/RequirementDefinition/TA/system_architecture.mmd
 M docs/Setup/multi_user_setup.md
 M .claude/CLAUDE.md
```

（`docs/superpowers/specs/2026-08-28-japanese-rag-reranker-design.md` と本計画ファイルも未コミットであれば併せて出る。）

- [ ] **Step 5: 利用者へ引き継ぐ**

以下はリポジトリのルールにより**実行しない**。手順書（`docs/Setup/multi_user_setup.md`）に記載済みであることを確認し、利用者に伝える。

```bash
docker compose up -d                    # reranker-model-init が走り、成功後に open-webui が起動する
docker compose logs reranker-model-init # "reranker ready" と "tokenizer ready" を確認する
docker compose restart open-webui       # Admin Settings 反映後、全ワーカーをキャッシュから起動し直す
```

実環境で確認してもらう項目（spec §9）:

1. `reranker-model-init` が終了コード 0 で完了する
2. 2 回目の `docker compose up -d` では init がネットワークに触れず即座に完了する（`cache hit` が出る）
3. Admin Settings で保存した直後、Hybrid Search が off に戻っていない
4. `docker stats` で `open-webui` の RSS 増分を実測し、spec §4.2 の見積もり（2〜3 GB）と照合する
5. 日本語 PDF をアップロードし、チャンク数が 2〜3 倍に増えている
6. 1 クエリあたりの再スコア所要時間を計測し、spec §3.7 の推定（約 30 ペア/秒）と照合する。乖離が大きければ spec §4.2 の縮小順序に従う

---

## 実行順序のまとめ

```
Task 1（スクリプト）
   ↓
Task 2（compose 配線）
   ↓
Task 3（.env.example）
   ↓
Task 4（構成図） ─┐
Task 5（手順書） ─┼─ 順不同
Task 6（CLAUDE.md）─┘
   ↓
最終確認
```
