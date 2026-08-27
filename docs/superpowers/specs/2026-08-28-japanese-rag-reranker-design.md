# 日本語向け Basic RAG — Tokenizer Model と Rerank Model の追加

作成日: 2026-08-28
対象: `docker-compose.yml` / `.env.example` / `docs/Setup/multi_user_setup.md` /
`docs/RequirementDefinition/TA/system_architecture.mmd` / `.claude/CLAUDE.md`
前提となる設計: [2026-08-27-multi-user-design.md](2026-08-27-multi-user-design.md) §4.7（Basic RAG の 3 点選択）

---

## 1. 背景と目的

本プロジェクトの Basic RAG は次の 3 点で構成されている。

| 層                 | 採用                            | 決定の根拠                                            |
| ------------------ | ------------------------------- | ----------------------------------------------------- |
| Embedding engine   | OpenAI `text-embedding-3-large` | 既定の SentenceTransformers はワーカーあたり約 500MB  |
| Content extraction | Docling                         | 既定の pypdf は継続的な取り込みでメモリリーク         |
| Vector database    | PGVector                        | 既定の ChromaDB は SQLite ベースで fork-safe ではない |

ここに **日本語に最適化した OSS の Tokenizer Model と Rerank Model** を足す。目的は 2 つ。

1. **検索精度** — ベクトル検索だけでは意味的に近いが的外れなチャンクが上位に混ざる。クロスエンコーダで再スコアすると、クエリとチャンクを同時に読んだうえで順位を付け直せる。
2. **チャンク境界の単位を正す** — 現在チャンク長は tiktoken `cl100k_base` で数えている。これは埋め込みモデルの単位ではあるが、**リランカーの単位ではない**。リランカーは窓を超えた分を黙って切り捨てるため、単位を揃えないと「チャンクの後半がスコアリングに一切使われない」状態が気付かれずに続く。

---

## 2. 決定事項

| #   | 決定                                                                                 | 補足                                                                                                                      |
| --- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| D1  | リランクエンジンは **SentenceTransformers（Open WebUI プロセス内 CrossEncoder）**    | `RAG_RERANKING_ENGINE=''`。管理画面の「Default (SentenceTransformers)」。**リランカー本体は別コンテナにできない**（§3.4） |
| D2  | テキスト分割は **トークン（Transformers）** = `RAG_TEXT_SPLITTER=token_transformers` | `RAG_TOKENIZER_MODEL` はこのときだけ効く（§3.3）                                                                          |
| D3  | モデルは **`hotchpotch/japanese-reranker-small-v2`**                                 | 70,151,041 パラメータ（F32 で 280.6MB）。リランクとトークナイザで同じモデルを使う                                         |
| D4  | チャンクは **512 窓に収める**（`CHUNK_SIZE=440` / `CHUNK_OVERLAP=64`）               | リランカーによる切り詰めを起こさない                                                                                      |
| D5  | `RAG_TOP_K=40` / `RAG_TOP_K_RERANKER=20`                                             | プロセス内実行のため再スコアの CPU が API ワーカーと競合する。候補プールを絞る                                            |
| D6  | バックエンドは **torch 固定**                                                        | `onnx` は `optimum` 未同梱のため不可。公式イメージを維持する方針を優先した（§3.5、§10）                                   |
| D7  | **`reranker-model-init` サービスを compose に追加する**                              | モデルの事前取得と**ロード検証**を担う一回きりのサービス。`minio-init` と同じパターン（§5.1）                             |

### 却下した案

| 案                                                           | 却下理由                                                                                                                                                            |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| リランカーを別コンテナに置く（Infinity 等）                  | `RAG_RERANKING_ENGINE` の選択肢は `''`（SentenceTransformers）と `external` の 2 つだけで、別コンテナに置いた時点で `external` になる。D1 と両立しない              |
| カスタム Open WebUI イメージで `optimum[onnxruntime]` を追加 | ONNX int8 で重みが 280MB → 約 70MB/ワーカーになり CPU 推論も 2〜4 倍速くなるが、公式イメージを維持する方針を優先した。メモリが逼迫した場合の退避先として §10 に残す |
| ModernBERT の 8192 窓を活かして大きいチャンクを維持          | `japanese-reranker-v2` は max_length=512 で学習・評価されており外挿になる                                                                                           |
| PGroonga / pg_bigm で日本語の語彙検索を成立させる            | SQL が `'simple'` を文字列リテラルで固定しているため、拡張を入れるだけでは使われない（§3.2）                                                                        |

---

## 3. 上流実装の調査結果

**この節が実装判断の正本である。** すべて submodule（`full-stack/open-webui`）の実ソースと、HuggingFace API の実レスポンスで確認済み。

### 3.1 リランカーはハイブリッド検索の中にしか存在しない

`RerankCompressor` が現れるのは次の 2 経路だけである。

- `retrieval/utils.py:434` — `query_doc_with_native_hybrid_search`（ベクトル DB が `hybrid_search` を実装している場合。PGVector は該当する）
- `retrieval/utils.py:556` 付近 — `query_doc_with_hybrid_search`（レガシー経路）

通常検索の `query_doc` には存在しない。したがって **`ENABLE_RAG_HYBRID_SEARCH=true` にしない限り、リランカーの設定をいくら正しく書いても一度も呼ばれない。** 現在の `.env.example` はこの値を設定しておらず、`config.py:957` の既定は `false` である。

同じ条件は `get_rf` の呼び出し側にもある（`main.py:623`）。

```python
if rag_config.get('rag.enable_hybrid_search') and not rag_config.get('rag.bypass_embedding_and_retrieval'):
    app.state.rf = get_rf(...)
else:
    app.state.rf = None
```

`BYPASS_EMBEDDING_AND_RETRIEVAL=true` にした場合も同様にリランカーは無効になる。

### 3.2 ⚠️ 日本語では、ハイブリッド検索の語彙側が機能しない

PGVector は native hybrid search を実装しており、SQL は次のようになっている（`retrieval/vector/dbs/pgvector.py:566-578`）。

```sql
WITH fts_query AS (SELECT plainto_tsquery('simple', :query) AS query)
SELECT ..., ts_rank_cd(to_tsvector('simple', coalesce(document_chunk.text, '')), fts_query.query) AS rank
FROM document_chunk, fts_query
WHERE document_chunk.collection_name = :collection_name
  AND to_tsvector('simple', coalesce(document_chunk.text, '')) @@ fts_query.query
```

`simple` は空白と記号で区切って小文字化するだけの設定で、形態素解析も N-gram 分割も行わない。**日本語の文は丸ごと 1 トークンになる**ため、クエリとチャンクが完全一致しない限りヒットしない。

レガシー経路も同じ結論になる。`BM25Retriever.from_texts()` に `preprocess_func` を渡していない（`retrieval/utils.py:524-527`）ので、LangChain 既定の `str.split()`、すなわち空白区切りである。

**設定で差し替える口は無い。** したがって本設計では語彙側に重みを配らない。

```
ENABLE_RAG_HYBRID_SEARCH=true    ← リランカーを起動するためのスイッチとして使う
RAG_HYBRID_BM25_WEIGHT=0         ← 語彙側の寄与をゼロにする
```

これは安全かつ無駄が無い。理由は 2 つ。

- 統合は **RRF による和集合**であって積集合ではない（`retrieval/vector/utils.py:36-90`）。重み 0 にしても結果が空になることはない
- `if bm25_weight > 0 and query and query.strip():` のガードがあるため（`pgvector.py:562`）、**重み 0 なら FTS の SQL がそもそも発行されない**

### 3.3 Tokenizer Model が効く条件

`RAG_TOKENIZER_MODEL`（`config.py:993`、PersistentConfig キー `rag.tokenizer_model`）は、**`RAG_TEXT_SPLITTER=token_transformers` のときだけ**参照される（`routers/retrieval.py:1608-1615`）。

```python
def get_splitter_length_function(request, config):
    if config.TEXT_SPLITTER == 'token':
        encoding = tiktoken.get_encoding(str(config.TIKTOKEN_ENCODING_NAME))
        return lambda text: len(encoding.encode(text, ...))
    if config.TEXT_SPLITTER == 'token_transformers':
        tokenizer = get_transformers_tokenizer(request, config)
        return lambda text: len(tokenizer.encode(text))
    return len
```

`TEXT_SPLITTER` に指定できるのは `''` / `character` / `token` / `token_transformers` の 4 つで、それ以外は `Invalid text splitter` で例外になる（`routers/retrieval.py:1702-1731`）。管理画面の表示名は「トークン（Transformers）」で、値は `token_transformers`（`src/lib/components/admin/Settings/Documents.svelte:868-884`。Tokenizer Model の入力欄はこの値を選んだときだけ現れる）。

`get_transformers_tokenizer`（同 `:1573-1600`）の挙動で重要な点は 4 つ。

1. **重みは読まない。** `AutoTokenizer.from_pretrained` のみで、消費メモリは数 MB。4 ワーカーに載っても問題にならない
2. **`/` を含まない ID は `sentence-transformers/` を前置される。** 本設計の ID は `hotchpotch/...` なのでそのまま使われる
3. **キャッシュ先は `SENTENCE_TRANSFORMERS_HOME` または `HF_HUB_CACHE`。** 公式イメージは前者を `/app/backend/data/cache/embedding/models` に設定済み（`Dockerfile:95`）で、これは `open-webui-data` ボリューム上にある
4. **`local_files_only = not RAG_EMBEDDING_MODEL_AUTO_UPDATE`（同 `:1583`）。** init サービスがキャッシュを保証するため、本設計ではこれを `false` にしてワーカーからのネットワークアクセスを断つ（§5.2）

**MeCab 系のトークナイザは使えない。** `tohoku-nlp/bert-base-japanese` 系は `fugashi` と `unidic-lite` を要求するが、`backend/requirements.txt` に含まれていない（確認済み: `transformers==5.5.4` / `sentence-transformers==5.5.1` / `sentencepiece==0.2.1` はあるが fugashi・unidic・ipadic は無い）。`AutoTokenizer.from_pretrained` がインポートエラーで落ちる。**SentencePiece ベースのモデルを選ぶこと。** `japanese-reranker-v2` シリーズは条件を満たす（リポジトリに `tokenizer.model` と `tokenizer.json` があり、`.py` ファイルは無い）。

### 3.4 リランクエンジンの選択肢は 2 つしかない

管理画面のドロップダウン（`Documents.svelte:1214-1215`）:

```svelte
<option value="">{$i18n.t('Default (SentenceTransformers)')}</option>
<option value="external">{$i18n.t('External')}</option>
```

`''` は `get_rf` の local 分岐（`routers/retrieval.py:204-222`）に入り、**Open WebUI のプロセス内で `sentence_transformers.CrossEncoder` を動かす**。

```python
import sentence_transformers, torch
rf = sentence_transformers.CrossEncoder(
    get_model_path(reranking_model, auto_update),
    device=DEVICE_TYPE,
    trust_remote_code=RAG_RERANKING_MODEL_TRUST_REMOTE_CODE,
    backend=SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND,
    model_kwargs=SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS,
    activation_fn=(torch.nn.Sigmoid() if SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION else None),
)
```

したがって **リランカーを別コンテナに置くことは D1 と両立しない。** 別コンテナに置いた時点でエンジンは `external` になる。compose に追加するサービス（D7）はモデルの事前取得と検証を担うものであって、リランカー本体ではない。

`get_model_path`（`retrieval/utils.py:1662-1698`）の要点:

- `local_files_only = not update_model`。`update_model` は `RAG_RERANKING_MODEL_AUTO_UPDATE` から来る
- `cache_dir = os.getenv('SENTENCE_TRANSFORMERS_HOME')`
- **`if os.path.exists(model)` があるので、絶対パスを渡せばそれをそのまま返す**（イメージにモデルを焼き込む構成を採る場合に使えるが、本設計では採らない）
- 取得に失敗しても `OFFLINE_MODE` でなければ例外を投げず、モデル ID をそのまま返す。その先で sentence-transformers が自分でダウンロードを試みる

関連する環境変数。**いずれも PersistentConfig ではない**ため、起動済みの環境でも `.env` の変更が再起動で反映される。

| 変数                                                              | 既定                                    | 位置                                           |
| ----------------------------------------------------------------- | --------------------------------------- | ---------------------------------------------- |
| `SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND`                     | `torch`（空文字なら torch）             | `env.py:1065-1067`                             |
| `SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS`                | なし（JSON）                            | `env.py:1070-1077`                             |
| `SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION` | `True`                                  | `env.py:1082-1083`                             |
| `RAG_RERANKING_MODEL_AUTO_UPDATE`                                 | `True`（`OFFLINE_MODE` が真なら false） | `config.py:1022-1023`                          |
| `RAG_RERANKING_MODEL_TRUST_REMOTE_CODE`                           | `True`                                  | `config.py:1026`                               |
| `DEVICE_TYPE`                                                     | `cpu`（CUDA があれば `cuda`）           | `env.py:45-65`。検出結果であり環境変数ではない |

`RAG_RERANKING_BATCH_SIZE`（`config.py:1028`、既定 32）は **PersistentConfig であり、local 分岐でのみ使われる**（`retrieval/utils.py:1243-1245`）。

**シグモイドは順位を変えない。** 既定で `activation_fn=torch.nn.Sigmoid()` が適用されるが、シグモイドは単調増加なので**並び順は一切変わらない**。変わるのはスコアの絶対値の尺度だけ（実数 → (0,1)）である。本設計は `RAG_RELEVANCE_THRESHOLD=0.0` なので影響しないが、将来しきい値を導入する場合はこの尺度で考える必要がある。

**`trust_remote_code` は false にできる。** `hotchpotch/japanese-reranker-small-v2` のリポジトリファイルは `config.json` / `model.safetensors` / `tokenizer.*` / `special_tokens_map.json` / `training_args.bin` / `README.md` のみで、**カスタムコード（`.py`）を含まない**（HuggingFace API で確認済み）。ModernBERT は transformers 4.48 以降がネイティブに対応しており、本イメージは 5.5.4 である。

### 3.5 ⚠️ ONNX / OpenVINO バックエンドは選べない

`SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND=onnx` と書くことはできるが、sentence-transformers がそれを解決するには `optimum[onnxruntime]` が必要である。`backend/requirements.txt` にあるのは `onnxruntime==1.26.0` だけで `optimum` は無い（確認済み）。**公式イメージを維持する方針である以上、torch 固定になる。**

参考: 仮にカスタムイメージで `optimum` を足せば、`export_dynamic_quantized_onnx_model(model, quantization_config="avx2", ...)` で int8 量子化でき、重みは 280MB → 約 70MB／CPU 推論は 2〜4 倍速になる。ホストが AVX2 までなので `avx512_vnni` ではなく `avx2` を選ぶ必要がある。メモリが逼迫した場合の退避先として §10 に記録する。

### 3.6 ⚠️ 見落とすと沈黙して壊れる 5 点

1. **モデルのロードに失敗すると、ハイブリッド検索が勝手に off に戻る。**
   `routers/retrieval.py:1185-1187`:

   ```python
   except Exception as e:
       log.error(f'Error loading reranking model: {e}')
       config.ENABLE_RAG_HYBRID_SEARCH = False
   ```

   HTTP エラーは返らない。管理画面では「保存したはずなのに Hybrid Search が off に戻っている」という症状になり、原因は `log.error` の 1 行にしか出ない。**D7 の init サービスは、この失敗を `docker compose up` 時点の明示的なエラーへ前倒しするために存在する。**

2. **`RAG_RERANKING_MODEL` が空だと、エラーではなく「別のもの」が動く。**
   `get_rf` は `if reranking_model:` で早期に `None` を返す（`routers/retrieval.py:173 / 176 / 240`）。`RerankCompressor` は `reranking_function` が `None` のとき **埋め込みベースの再スコアへフォールバックする**（`retrieval/utils.py:1743-1758`）。結果として全候補に対して毎クエリ OpenAI の embeddings が追加で叩かれ、**精度は上がらないのに課金だけ増える**。ログにも異常は出ない。

3. **モデルは遅延ロードではなく、ワーカー起動時に読まれる。**
   `main.py:624` の `get_rf` は lifespan の中で走る。`UVICORN_WORKERS=4` なら **4 プロセスがそれぞれモデルを保持する**。「使わなければメモリを食わない」ではない。

4. **`SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS` の JSON が壊れていると黙って無視される。**
   `env.py:1074-1077` に try/except があり、失敗時は `None` にフォールバックして警告も出ない。よくある壊し方は大文字の `True` / `False` を書くこと。

5. **`RAG_TOP_K` はコレクションごとの取得数である。**
   ナレッジベースを 3 つ束ねたモデルでは候補は 3×40 になり、再スコアも 3 回発生する。

なお呼び出しは `asyncio.to_thread` 経由なので（`retrieval/utils.py:1747`）、推論中もワーカーのイベントループは止まらない。止まらないのは**イベントループだけ**で、CPU は奪う（§4.2）。

### 3.7 モデル選定の根拠

| モデル                            | パラメータ（実測） | F32 サイズ   | 層 / hidden  | JQaRA          | JaCWIR     | CPU 速度                       |
| --------------------------------- | ------------------ | ------------ | ------------ | -------------- | ---------- | ------------------------------ |
| `japanese-reranker-tiny-v2`       | —                  | —            | 3 / 256      | 0.6455         | 0.9287     | 214 ペア/秒（実測）            |
| `japanese-reranker-xsmall-v2`     | 36,771,585         | 147 MB       | 10 / 256     | 0.7403         | 0.9409     | 65 ペア/秒（実測）             |
| **`japanese-reranker-small-v2`**  | **70,151,041**     | **280.6 MB** | **13 / 384** | **0.7633**     | **0.9586** | **約 30 ペア/秒（推定）**      |
| `japanese-reranker-base-v2`       | —                  | —            | 19 / 512     | 0.7845         | 0.9603     | 約 15 ペア/秒（推定）          |
| `cl-nagoya/ruri-v3-reranker-310m` | —                  | —            | —            | 日本語 SOTA 級 | —          | 約 5 ペア/秒（推定・GPU 前提） |

パラメータ数は HuggingFace API の `safetensors.parameters.F32` の実値。速度の実測値は tiny / xsmall のみ（150,000 ペアの処理時間から換算）で、small / base は層数と hidden size からの推定である。**実環境で計測して見直すこと。**

`japanese-reranker-base-v2` の `config.json` は `model_type: modernbert` / `max_position_embeddings: 8192` だが、モデルカードの利用例は `max_length=512` である。**アーキテクチャ上の上限と、学習・評価された範囲は別物。** 本設計は後者に合わせる。

出典:

- [とても小さく速く実用的な日本語リランカー japanese-reranker-tiny,xsmall,small,base の v2 を公開 — A Day in the Life](https://secon.dev/entry/2025/05/08/100000-japanese-reranker-v2/)
- [hotchpotch/japanese-reranker-small-v2 — Hugging Face](https://huggingface.co/hotchpotch/japanese-reranker-small-v2)
- [hotchpotch/japanese-reranker-tiny-v2 — Hugging Face](https://huggingface.co/hotchpotch/japanese-reranker-tiny-v2)
- [Speeding up Inference — Sentence Transformers documentation](https://sbert.net/docs/cross_encoder/usage/efficiency.html)

---

## 4. アーキテクチャ

### 4.1 データフロー

```
起動時（一回きり）
  ⓪ reranker-model-init
       └ HuggingFace から japanese-reranker-small-v2 を open-webui-data の
         HF キャッシュへ取得し、CrossEncoder としてロードできることを検証して終了
       └ open-webui は service_completed_successfully で待つ

文書取り込み
  ① 抽出      Open WebUI ──▶ docling-serve            （svc-docling-net, internal）
  ② 分割      RAG_TEXT_SPLITTER=token_transformers
              └ RAG_TOKENIZER_MODEL = japanese-reranker-small-v2 のトークナイザで長さを数える
              └ CHUNK_SIZE=440 / CHUNK_OVERLAP=64      （= クエリぶんを残してリランカーの 512 窓に収まる）
  ③ 埋め込み  Open WebUI ──▶ OpenAI text-embedding-3-large
  ④ 格納      Open WebUI ──▶ PGVector (postgres)       （svc-net）

検索
  ⑤ 候補取得  native hybrid search（PGVector）/ 上位 40 件・コレクションごと
              └ RAG_HYBRID_BM25_WEIGHT=0 のため FTS の SQL は発行されず、実体はベクトル検索
  ⑥ 再スコア  Open WebUI プロセス内の CrossEncoder（asyncio.to_thread）
              └ japanese-reranker-small-v2 / torch / CPU / 4 ワーカーそれぞれが保持
  ⑦ 上位 20 件を LLM のコンテキストへ
```

イメージのビルドが要るサービスは Computer と Docling の **2 つのまま**である（init サービスは公式イメージをそのまま使う）。

### 4.2 ⚠️ リソースの見積もり

ホストの実測値:

```
CPU: 4 コア / AVX2 まで（AVX-512・VNNI なし）
RAM: 23GB（実行中に確認した時点で利用可能は約 5GB）
```

compose が既に確保している上限は Docling 4CPU/4G + Computer 3×(2CPU/3G) + Playwright 3×(1CPU/1G) = **13 CPU / 16G**。limits はピーク保護であって常時消費ではないが、余裕は大きくない。

**メモリ（増分）**

| 項目                                        | 1 ワーカーあたり      | ×4 ワーカー |
| ------------------------------------------- | --------------------- | ----------- |
| モデル重み（F32・実測パラメータ数から算出） | 280.6 MB              | 1,122 MB    |
| torch + transformers のランタイム           | 300〜600 MB（要実測） | 1.2〜2.4 GB |
| トークナイザ（`RAG_TOKENIZER_MODEL`）       | 数 MB                 | 数十 MB     |

合計の増分は **2〜3 GB 程度**と見込まれるが、ランタイム側は推定であり実測が必要である。`docker-compose.yml` の `open-webui` サービスには現在 `deploy.resources.limits` が無い。**上限を当てずっぽうで設定するとサービス全体が OOM kill されるため、まず実測してから決める。**

**CPU**

再スコアは `asyncio.to_thread` 経由なのでイベントループは止まらないが、CPU は API ワーカーと共有する。候補 40 件で推定 1.3 秒ぶんの CPU を占有する。さらに **torch は既定でホストの全コアを使おうとする。** Open WebUI のイメージにも backend のソースにも `OMP_NUM_THREADS` / `torch.set_num_threads` の設定は存在しない（確認済み）ため、上限を明示する。

**ホストが 4 コアなので `OMP_NUM_THREADS=1` にする。** 4 ワーカー × 1 スレッド = 4 で、ちょうどコア数に一致する。2 にすると 8 スレッドとなり、Docling や Computer が動いている間はオーバーサブスクリプションでかえって遅くなる。

**縮小の順序**

1. `RAG_TOP_K` を 40 → 24 に下げる（レイテンシと CPU が直線的に減る）
2. モデルを `japanese-reranker-xsmall-v2` に下げる（重み 147MB → ×4 で 588MB。実測 65 ペア/秒。JQaRA は 0.7633 → 0.7403）
3. `UVICORN_WORKERS` を 4 → 2 に下げる（メモリは半減するが API の同時実行性も半減する）
4. カスタムイメージで `optimum` を足し ONNX int8（avx2）にする（§10）

---

## 5. 実装

### 5.1 `docker-compose.yml`

#### 追加するサービス: `reranker-model-init`

`minio-init` と同じ「一回だけ走って終了する」サービス。担う役割は 2 つある。

1. **事前取得** — モデルを `open-webui-data` ボリューム上の HF キャッシュへ入れる。これが無いと 4 ワーカーが起動時に同時ダウンロードを始める（§3.6-3）
2. **ロード検証** — 実際に `CrossEncoder` としてロードしてみる。失敗を `docker compose up` の明示的なエラーに変換し、§3.6-1 の「Hybrid Search が黙って off に戻る」を防ぐ

```yaml
reranker-model-init:
  # ⚠️ open-webui と同じイメージを使うこと。huggingface_hub / sentence-transformers /
  #    transformers のバージョンが一致し、キャッシュのレイアウトも一致する。
  #    別イメージにすると「init は成功したのにワーカーが見つけられない」が起きる。
  #    追加の pull も発生しない。
  image: ghcr.io/open-webui/open-webui:main
  container_name: svc-reranker-model-init
  restart: "no"
  environment:
    # ⚠️ 公式イメージの Dockerfile:95 と同じ値。ここがずれるとワーカーが
    #    キャッシュを見つけられない。
    SENTENCE_TRANSFORMERS_HOME: /app/backend/data/cache/embedding/models
    HF_HOME: /app/backend/data/cache/embedding/models
    RERANKER_MODEL_ID: ${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}
  # ⚠️ 公式イメージには ENTRYPOINT（bash start.sh）が焼かれているため、
  #    command ではなく entrypoint を上書きすること。
  entrypoint: ["python", "/scripts/init_reranker_model.py"]
  volumes:
    - open-webui-data:/app/backend/data
    - ./scripts/init_reranker_model.py:/scripts/init_reranker_model.py:ro
  # ⚠️ 一回きりで終了するため専用ネットワークは切っていない。必要な通信は
  #    HuggingFace への egress のみで、svc-net の他サービスは使わない。
  networks: [svc-net]
```

#### 追加するスクリプト: `scripts/init_reranker_model.py`

要件は 4 つ。

1. **キャッシュを先に見る。** `local_files_only=True` で試し、あればネットワークに触れずに成功する。初回だけダウンロードし、2 回目以降は HuggingFace が落ちていても成功する
2. **ロードを検証する。** `CrossEncoder` を実際に構築し、1 ペアだけ `predict` して数値が返ることまで確認する
3. **`trust_remote_code` / `backend` は Open WebUI と同じ値を使う。** 検証と本番で条件が違うと意味が無い
4. **失敗時は非ゼロで終了する。** compose がそこで止まる

#### `open-webui` サービスへの追記

```yaml
depends_on:
  # モデルの取得と検証が終わるまで起動しない。
  # ⚠️ 初回は HuggingFace への到達性が必須になる。オフラインで起動したい場合は
  #    この 2 行を消して RAG_RERANKING_MODEL_AUTO_UPDATE=true に戻すこと
  #    （ワーカーが各自で取得するようになる）。
  reranker-model-init:
    condition: service_completed_successfully
environment:
  # リランクとトークナイザで同じモデルを使う。ここが食い違うと、チャンクが
  # リランカーの 512 窓に収まらなくなる（無症状のまま精度が落ちる）。
  RAG_RERANKING_MODEL: ${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}
  RAG_TOKENIZER_MODEL: ${RERANKER_MODEL_ID:-hotchpotch/japanese-reranker-small-v2}
  #
  # ⚠️ torch は既定でホストの全コアを使う。Open WebUI 側に上限を設ける仕組みは
  #    無い（イメージにも backend にも OMP_NUM_THREADS の設定が無い）。
  #    ホストが 4 コアなので、4 ワーカー × 1 = 4 に揃える。
  OMP_NUM_THREADS: ${OPEN_WEBUI_OMP_NUM_THREADS:-1}
  MKL_NUM_THREADS: ${OPEN_WEBUI_OMP_NUM_THREADS:-1}
```

`RAG_RERANKING_MODEL` と `RAG_TOKENIZER_MODEL` は PersistentConfig なので、この記述が効くのは初回起動時だけである。それでも compose 側に置くのは、**2 つが同じ値であるという制約を 1 つの変数（`RERANKER_MODEL_ID`）で表現するため**である。init サービスも同じ変数を読む。`OMP_NUM_THREADS` は素の環境変数なので常に効く。

### 5.2 `.env.example` §13 の差分

| 変数                                                              | 現在                 | 変更後                                  | PC  | 備考                                                                     |
| ----------------------------------------------------------------- | -------------------- | --------------------------------------- | --- | ------------------------------------------------------------------------ |
| `RAG_TEXT_SPLITTER`                                               | `token`              | `token_transformers`                    | ✅  | 管理画面の「トークン（Transformers）」                                   |
| `TIKTOKEN_ENCODING_NAME`                                          | `cl100k_base`        | そのまま残す                            | ✅  | `token` を選んだときだけ参照される                                       |
| `RAG_TOKENIZER_MODEL`                                             | —                    | `hotchpotch/japanese-reranker-small-v2` | ✅  | compose が上書き。単体起動時のみ有効                                     |
| `CHUNK_SIZE`                                                      | `2000`               | `440`                                   | ✅  | 512 − 特殊トークン(3) − クエリのトークン数。オーバーラップは加算されない |
| `CHUNK_OVERLAP`                                                   | `200`                | `64`                                    | ✅  | チャンクの約 13%                                                         |
| `ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER`                            | `true`               | 変更なし                                | ✅  | 見出しで先に切ってからトークン数で分割される                             |
| `ENABLE_RAG_HYBRID_SEARCH`                                        | 未設定（既定 false） | `true`                                  | ✅  | **これが無いとリランカーは一度も呼ばれない**                             |
| `RAG_HYBRID_BM25_WEIGHT`                                          | 未設定（既定 0.5）   | `0`                                     | ✅  | §3.2                                                                     |
| `RAG_RERANKING_ENGINE`                                            | —                    | 空（`RAG_RERANKING_ENGINE=`）           | ✅  | 空 = SentenceTransformers。`external` にしない                           |
| `RAG_RERANKING_MODEL`                                             | —                    | `hotchpotch/japanese-reranker-small-v2` | ✅  | compose が上書き。空にすると §3.6-2                                      |
| `RAG_RERANKING_BATCH_SIZE`                                        | 未設定（既定 32）    | `16`                                    | ✅  | local 分岐でのみ使われる。CPU 実行なので控えめに                         |
| `RAG_TOP_K`                                                       | `15`                 | `40`                                    | ✅  | 再スコアへ渡す候補プール。コレクションごと                               |
| `RAG_TOP_K_RERANKER`                                              | 未設定（既定 3）     | `20`                                    | ✅  | LLM へ渡す最終件数                                                       |
| `RAG_RELEVANCE_THRESHOLD`                                         | 未設定（既定 0.0）   | `0.0` を明示                            | ✅  | シグモイド適用後の尺度で効く（§3.4）                                     |
| `RAG_RERANKING_MODEL_AUTO_UPDATE`                                 | 未設定（既定 true）  | **`false`**                             | —   | init がキャッシュを保証するので、ワーカーは HF に触らない                |
| `RAG_EMBEDDING_MODEL_AUTO_UPDATE`                                 | 未設定（既定 true）  | **`false`**                             | —   | トークナイザ側の `local_files_only` を決める（`retrieval.py:1583`）      |
| `RAG_RERANKING_MODEL_TRUST_REMOTE_CODE`                           | 未設定（既定 true）  | `false`                                 | —   | 対象リポジトリはカスタムコードを含まない（§3.4）                         |
| `RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE`                           | 未設定（既定 true）  | `false`                                 | —   | トークナイザ読み込みにも同じ理由が当てはまる                             |
| `SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND`                     | 未設定（既定 torch） | `torch` を明示                          | —   | `onnx` は `optimum` 未同梱のため不可（§3.5）                             |
| `SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION` | 未設定（既定 true）  | `true`                                  |     | シグモイドは単調増加なので順位は変わらない。変わるのはスコアの尺度だけ   |

PC = PersistentConfig（初回起動時にしか取り込まれない）。**チェックが無い 6 つは素の環境変数**なので、起動済みの環境でも `.env` の変更が再起動で反映される。

⚠️ **`RAG_RERANKING_MODEL_AUTO_UPDATE=false` は init サービスと対になっている。** init を外すなら `true` に戻すこと。false のままキャッシュが空だと、`get_model_path` が `snapshot_download(local_files_only=True)` で失敗し、モデル ID をそのまま返した先で sentence-transformers が自力ダウンロードを試みる（`OFFLINE_MODE` でなければ例外にはならないが、ワーカー 4 つが同時に取りに行く元の問題に戻る）。

§19（docker-compose 用）に追加:

```
RERANKER_MODEL_ID=hotchpotch/japanese-reranker-small-v2
OPEN_WEBUI_OMP_NUM_THREADS=1
```

**`RAG_TOP_K=40` / `RAG_TOP_K_RERANKER=20` の根拠**

最終件数 20 に対し候補 40 で選別比 2:1。チャンクが約 700 文字になるため、LLM へ届く文脈量は 20 × 700 ≒ 14,000 文字で、変更前（15 × 約 1,800 ≒ 27,000 文字）の約半分だが、リランカーが選んだ 20 件なので密度は上がる。プロセス内実行で CPU が API ワーカーと競合するため、候補プールは 40 に留める。実測して余裕があれば 60 まで上げてよい（精度は上がる方向）。

### 5.3 構成図（`docs/RequirementDefinition/TA/system_architecture.mmd`）

現在の図には Docling も載っていない（前回の変更時の積み残し）。今回の対象である RAG 層をまとめて反映する。

- 新設する `ContentExtraction` サブグラフに `docling-serve` を置き、エッジを 1 本追加
  - `open-webui <--> |文書のコンテンツ抽出（PDF/Office → Markdown）| docling-serve`
- **リランカーは独立したコンテナではないため、デプロイ単位のノードとしては置かない。** `Vercel Compute` の中に「Reranker（Open WebUI プロセス内 / CrossEncoder）」を破線エッジ（`-.->`）で表現し、他のサービスと区別する
- `reranker-model-init` は起動時のみ存在するため図には含めない（常時稼働の構成を表す図であるため）
- 既存の `open-webui <--> |ベクトル検索| pgvector` はそのまま

`.mmd` は `mcp-mermaid` で描画を検証してからコミットする。

**Playwright MCP も未記載だが、今回のスコープ外とする**（RAG とは無関係な面であり、混ぜると図の変更理由が追えなくなる）。

### 5.4 `.claude/CLAUDE.md`

「Basic RAG（Embedding = OpenAI / 抽出 = Docling / ベクトル DB = PGVector）」の節を更新する。

- 見出しと冒頭を、リランキングとトークナイザを含む記述へ
- 箇条書きに次の 5 点を追加
  - リランカーはハイブリッド検索が有効なときしか呼ばれず、日本語では語彙側が機能しないため BM25 重みを 0 にしていること
  - モデルのロード失敗が `ENABLE_RAG_HYBRID_SEARCH=False` の巻き戻しとして現れること、それを防ぐために init サービスがあること（§3.6-1）
  - `RAG_RERANKING_MODEL` を空にすると埋め込みベースの再スコアへ静かにフォールバックし、課金だけ増えること（§3.6-2）
  - モデルが 4 ワーカーぶん多重に載ること、`OMP_NUM_THREADS=1` を設けていること（§4.2）
  - `reranker-model-init` は open-webui と**同じイメージでなければならない**こと（キャッシュのレイアウト一致のため）
- ディレクトリ図に `scripts/init_reranker_model.py` を追加
- **ビルド対象は 2 つのまま**（この変更では増えない）

---

## 6. 運用手順

### 6.1 ⚠️ PersistentConfig と素の環境変数が混在する

§5.2 の表の PC 列を参照。PersistentConfig のものは `Config.seed_defaults()` が「DB に無いキーだけ INSERT」する（`models/config.py:256`）ため、**既に一度起動した環境では `.env` を書き換えてもエラーも警告も出ずに無視される。** 管理者が Admin Settings → Documents で手入力する。

素の環境変数（`*_AUTO_UPDATE` / `*_TRUST_REMOTE_CODE` / `SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND` / `OMP_NUM_THREADS`）は `.env` に書いて再起動すれば効く。

### 6.2 有効化の順序

1. `.env` を更新し、`docker compose up -d` を実行する
   → `reranker-model-init` が走ってモデルを取得・検証し、成功したら open-webui が起動する
   → **ここで失敗したら先へ進まない。** `docker compose logs reranker-model-init` を読む
2. Admin Settings → Documents で §5.2 の PersistentConfig 項目を入力して保存する
   - Reranking Engine: Default (SentenceTransformers)
   - Reranking Model: `hotchpotch/japanese-reranker-small-v2`
   - Hybrid Search: on / BM25 Weight: 0
   - Text Splitter: トークン（Transformers）/ Tokenizer Model: 同じモデル ID
   - Chunk Size: 440 / Chunk Overlap: 64 / Top K: 40 / Top K Reranker: 20
   - **保存後に Hybrid Search が off に戻っていないことを確認する**（§3.6-1）
3. `docker compose restart open-webui` で全ワーカーをキャッシュから起動させる
4. §6.3 の再インデックスを行う

### 6.3 ⚠️ 既存ナレッジベースの再インデックスが必須

チャンク分割の設定を変えても、既存のベクトルは作り直されない。新旧のチャンクが同じコレクションに混在すると、**リランカーが 512 トークンで切り詰める長いチャンクと、収まる短いチャンクが同じ土俵で比較される**。スコアが歪むが、エラーは出ない。

各ナレッジベースについて、既存ファイルを削除してから再アップロードする。管理者がチームへ配った read grant は、ナレッジベース自体を消さない限り維持される。`RAG_EMBEDDING_MODEL` は変更しないため、ベクトルの次元は変わらずコレクションを作り直す必要はない。

---

## 7. 失敗モードと切り分け

| 症状                                                    | 原因                                                                                  | 確認方法                                                                 |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `docker compose up` が init で止まる                    | モデル ID の誤り、HuggingFace への到達不可、ディスク不足                              | `docker compose logs reranker-model-init`                                |
| init は成功するがワーカーがモデルを見つけられない       | init と open-webui で `SENTENCE_TRANSFORMERS_HOME` かイメージが違う                   | 両サービスの環境変数とイメージタグを突き合わせる                         |
| 保存したのに Hybrid Search が off に戻る                | モデルのロードに失敗（§3.6-1）                                                        | `docker compose logs open-webui \| grep 'Error loading reranking model'` |
| 検索結果が変わらない                                    | `ENABLE_RAG_HYBRID_SEARCH` が false                                                   | Admin Settings → Documents の Hybrid Search                              |
| 検索結果が変わらず、OpenAI の課金だけ増える             | `RAG_RERANKING_MODEL` が空 → 埋め込みベースの再スコアへフォールバック（§3.6-2）       | 同画面の Reranking Model 欄                                              |
| 文書アップロードが `Tokenizer model required...` で失敗 | `token_transformers` なのに `RAG_TOKENIZER_MODEL` が空（`routers/retrieval.py:1600`） | 同画面の Tokenizer Model 欄                                              |
| コンテナが OOM kill される                              | モデル × 4 ワーカーぶんのメモリ（§4.2）                                               | `docker stats`。§4.2 の縮小順序                                          |
| 回答は返るがホスト全体が重い                            | torch がコアを取り合っている                                                          | `OMP_NUM_THREADS` が効いているか                                         |
| 検索が遅い                                              | 候補が多い（`RAG_TOP_K` × コレクション数）                                            | §3.6-5                                                                   |

---

## 8. 変更ファイル一覧

| ファイル                                                | 種別 | 内容                                                                             |
| ------------------------------------------------------- | ---- | -------------------------------------------------------------------------------- |
| `scripts/init_reranker_model.py`                        | 新規 | モデルの事前取得とロード検証                                                     |
| `docker-compose.yml`                                    | 変更 | `reranker-model-init` サービス、`open-webui` の depends_on と environment 4 変数 |
| `.env.example`                                          | 変更 | §13 に 20 変数、§19 に 2 変数                                                    |
| `docs/Setup/multi_user_setup.md`                        | 変更 | 有効化の順序（§6.2）、Admin Settings 手順、再インデックス手順                    |
| `docs/RequirementDefinition/TA/system_architecture.mmd` | 変更 | `ContentExtraction` サブグラフ（docling-serve）とリランカーの破線表現            |
| `.claude/CLAUDE.md`                                     | 変更 | Basic RAG 節、ディレクトリ図                                                     |

`functions/` は変更しないため、`scripts/check_functions.py` の再実行は不要。`docker compose build` も不要（ビルド対象は Computer と Docling の 2 つのまま）。

---

## 9. 検証項目

⚠️ 本リポジトリのルールにより、ビルドと起動はユーザが明示的に行う。以下は手順書に載せる確認項目である。

1. `docker compose up -d` で `reranker-model-init` が終了コード 0 で完了すること
2. 2 回目の `docker compose up -d` では init がネットワークに触れずに即座に完了すること（キャッシュ経路の確認）
3. `docker compose exec open-webui ls /app/backend/data/cache/embedding/models` にモデルのスナップショットが存在すること
4. §6.2 の手順 2 の直後、Hybrid Search が off に戻っていないこと
5. `docker stats` で `open-webui` の RSS 増分を実測し、§4.2 の見積もり（2〜3 GB）と照合すること
6. 日本語 PDF をアップロードし、チャンク数が増えていること（目安 2〜3 倍。2000 cl100k トークン ≒ 1,800 文字 に対し 440 SentencePiece トークン ≒ 650 文字）
7. 日本語で質問し、引用されるチャンクが変更前と変わること
8. 1 クエリあたりの再スコア所要時間を計測し、§3.7 の推定（約 30 ペア/秒）と照合すること。乖離が大きければ §4.2 の縮小順序に従う
9. `RAG_RERANKING_MODEL_TRUST_REMOTE_CODE=false` でモデルが読み込めること（読み込めない場合は true に戻し、理由を記録する）

---

## 10. スコープ外

| 項目                                                         | 理由                                                                                                                                 |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| 日本語の語彙検索（PGroonga / pg_bigm）                       | SQL が `'simple'` を固定しており、submodule を patch しないと使われない（§3.2）                                                      |
| カスタム Open WebUI イメージ + `optimum` + ONNX int8（avx2） | 公式イメージを維持する方針を優先。メモリが逼迫した場合の退避先として §4.2 の縮小順序 4 に記録（重み 280MB → 約 70MB／CPU 2〜4 倍速） |
| リランカーの別コンテナ化                                     | `RAG_RERANKING_ENGINE=external` になり D1 と両立しない（§3.4）                                                                       |
| GPU 対応 / `ruri-v3-reranker-310m`                           | 現構成は CPU のみ。GPU を導入する判断とセットで再検討する                                                                            |
| `system_architecture.mmd` への Playwright MCP 追記           | RAG と無関係な面。図の変更理由を追えなくするため別件とする                                                                           |
| Docling の日本語 OCR 設定                                    | 既存のまま（`DOCLING_PARAMS`）                                                                                                       |
