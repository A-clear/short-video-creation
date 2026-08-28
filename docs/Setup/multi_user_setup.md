# マルチユーザ対応 セットアップ手順書

ショート動画作成ツールを少人数の社内チーム（〜10 名、相互に信頼関係あり）で同時利用できるようにするための手順。設計の背景・調査で確定した事実・決定理由は `docs/superpowers/specs/2026-08-27-multi-user-design.md` を参照。

**所要時間**: 60〜90 分（1 ユーザ運用からの移行を含む場合はさらに 20〜30 分）

**前提**:

- 管理者（admin）ロールのアカウントで Open WebUI にログインできること
- `docker-compose.yml` / `.env.example` が本手順書と同じコミットのものであること（識別子は実装と厳密に対応している）
- Descript Functions 自体のセットアップは別手順。`docs/Setup/descript_functions_setup.md` を先に済ませておくこと
- Codex を使う場合は `docs/Setup/codex_agent_setup.md` を併読すること（本手順書はチーム別 3 台化に伴う差分のみ扱う）

---

> ⚠️ **この構成が守るのは「事故」であって「悪意」ではない。** 少人数の信頼できるチーム内での、意図しない越境（他人のファイルを踏む、他人のチャット状態が漏れる）を防ぐのが目的であり、悪意あるユーザからの防御ではない。到達できる限界は末尾の「既知の限界」に列挙する。運用者はこれを理解した上でチームを設計すること。

> ⚠️ **順序を誤ると設定が黙って反映されない。** `TERMINAL_SERVER_CONNECTIONS` / `OPENAI_API_CONFIGS` / `DEFAULT_USER_ROLE` / `USER_PERMISSIONS_*` はいずれも PersistentConfig で、`Config.seed_defaults()` は「DB に無いキーだけ INSERT」する（`full-stack/open-webui/backend/open_webui/models/config.py:256`）。つまり `.env` の値が取り込まれるのは **Open WebUI の初回起動時だけ**。起動済みの環境でこれらを変更したい場合は、`.env` を書き換えてもエラーにも警告にもならず単に無視されるので、対応する Admin Settings 画面で直接変更すること（各節に画面パスを記載）。

---

## 全体像

構成は 3 層に分かれる。層ごとに分離の単位と強さが異なる点が本設計の核心。

| 層                               | コンポーネント                                                                                      | 分離の単位                                               | 分離の強さ                                                                                                                                                                                        |
| -------------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ① 認証・認可                     | Open WebUI（グループ / access_grants / USER_PERMISSIONS_*）                                         | ユーザ・グループ                                         | アプリ UI 経路の ACL。DB・インフラへの直接アクセスは別問題（`ENABLE_ADMIN_CHAT_ACCESS` 等は UI 経路のみを塞ぐ）                                                                                   |
| ② 実行環境 — Open Terminal       | 共有 1 コンテナ（`open-terminal` サービス、`OPEN_TERMINAL_MULTI_USER=true` の組み込みマルチユーザ） | **ファイルのみ**（ユーザごとの Linux アカウントと home） | 弱い。PTY セッション・プロセス出力・ポート・CPU/メモリは全ユーザ共有                                                                                                                              |
| ② 実行環境 — Open WebUI Computer | チーム別 3 コンテナ（`open-webui-computer-a` / `-b` / `-c`）                                        | **コンテナ境界**（チーム単位）                           | チーム間は強い（ネットワーク分割済み）。**チーム内は分離なし**（ファイル・端末セッション・gateway キーすべて共有）                                                                                |
| ② 実行環境 — Playwright MCP      | チーム別 3 コンテナ（`playwright-mcp-a` / `-b` / `-c`）                                             | **コンテナ境界**（チーム単位）                           | チーム間は強い（`svc-playwright-net-*` で Computer と 1:1。`open-webui` 本体にも到達できない）。**チーム内はブラウザセッションを共有**（既知の限界 10）。任意なので使わない場合は起動しなくてよい |
| ③ アプリ — Descript Functions    | `functions/descript_pipe.py` 等の `chat_id` / `file_id` スコープ検証                                | ユーザ単位（`user_id`）                                  | Descript 自体の OAuth トークンは元々ユーザ単位で分離済み。Functions 側の state 読み書きにも所有者検証を追加（本設計のスコープ）                                                                   |

3 層は独立して機能する。①だけでも Open WebUI 単体の可視性は制御できるが、②・③を設定しないとチャット経由で他人の実行環境やファイルに触れてしまう。

---

## 移行手順（既存環境から）

**新規構築の場合はこの節を読み飛ばして「セットアップの順序」から始めてよい。** 既存の 1 コンテナ構成（Open Terminal 単体 / Open WebUI Computer 単体）から移行する場合のみ、以下を順に行う。

> ⚠️ **この節の操作は破壊的。** 既存ボリュームを削除する。実行前に必ず退避すべきものが無いか確認すること。

### 1. Computer の `/workspace` を退避する（必要な場合のみ）

ボリューム名がチーム別（`computer-a-workspace` 等）に変わるため、旧ボリューム `open-webui-computer-workspace` の中身は自動では引き継がれない。作業ファイルが残っている場合は、コンテナを止める前に `docker cp` で退避する。

```bash
docker cp svc-open-webui-computer:/workspace ./computer-workspace-backup
```

> ⚠️ **`codex-home` を新しい名前（`codex-home-a` / `-b` / `-c`）に変えると Codex の再ログインが必要になる。** 認証情報（`auth.json`）は旧ボリュームに残ったままになるため、必要ならこちらも退避しておく。ただし `auth.json` はログイン情報そのものなので、退避先の取り扱いに注意すること（このリポジトリにコミットしない）。

```bash
docker cp svc-open-webui-computer:/home/cptr/.codex/auth.json ./codex-auth-backup.json
```

### 2. 旧 Open WebUI Computer（単体）を削除する

```bash
# ⚠️ `open-webui-computer` というサービスは新しい compose に存在しない
#    （3 台化で `-a` / `-b` / `-c` に分割された）。コンテナ名を直指定して削除する。
docker rm -f svc-open-webui-computer
docker volume rm short-video-creation_open-webui-computer-data
docker volume rm short-video-creation_open-webui-computer-workspace
docker volume rm short-video-creation_codex-home
```

パッケージキャッシュ用の `-cache` / `-local` ボリュームが存在する場合も同様に削除してよい（消えても再取得されるだけ）。実際のボリューム名は環境によって接頭辞が異なる場合があるため、削除前に `docker volume ls | grep open-webui-computer` で確認すること。

### 3. 旧 Open Terminal のコンテナとボリュームを削除する

```bash
# ⚠️ 既存ボリュームを「マウント先だけ /home に変える」のは壊れる。
#    中身は /etc/skel 由来の dotfile 24KB のみで、移行価値はない。
docker compose rm -sf open-terminal
docker volume rm short-video-creation_open-terminal-data
```

削除後、新しいボリューム名 `open-terminal-home`（マウント先 `/home`）で作り直す。詳細は次節「セットアップの順序」を参照。

---

## セットアップの順序

順序を誤ると反映されない設定があるため、この順で行う（`docs/superpowers/specs/2026-08-27-multi-user-design.md` §9 の 13 ステップに、既存環境（移行）向けの手順・Basic RAG・Playwright MCP（任意）を追加した 16 ステップ）。

1. **既存の Open Terminal コンテナとボリュームを削除する**（移行の場合のみ。前節「移行手順」参照。新規構築では不要）

2. **`.env` を新しい `.env.example` に合わせて更新する**

   ```bash
   diff .env.example .env   # 差分を確認しながら手で反映する（自動マージしない）
   ```

   最低限、次の節を埋める: 10 節（`OAUTH_ALLOWED_DOMAINS` / `DEFAULT_USER_ROLE`）、13 節（`DOCLING_API_KEY`。`openssl rand -hex 32`。加えてリランクとチャンク分割の設定一式）、14 節（`OPEN_TERMINAL_API_KEY` / `OPEN_TERMINAL_MAX_SESSIONS`）、15 節（`OPEN_WEBUI_COMPUTER_PORT_A/B/C` 等。ゲートウェイキーはステップ 6 まで空欄でよい）、18 節（`USER_PERMISSIONS_*` / `AUDIT_LOG_LEVEL` 等）、19 節（`RERANKER_MODEL_ID` / `OPEN_WEBUI_OMP_NUM_THREADS`）。

   > ⚠️ 6 節の `UVICORN_WORKERS` は 1 → 4 に変わった。**Open WebUI のバージョンを上げた直後の初回起動だけは 1 に戻すこと。** マイグレーションは `config.py:78` のモジュール読み込み時に走るため、`--workers 4` では 4 プロセスが同時に `alembic upgrade` を実行する。しかも `config.py:73-75` が例外を握り潰す（`log.exception` のみで再送出しない）ので、**スキーマが壊れても起動は成功してしまう**。

3. **ビルドする**（対象は 2 サービス。Computer は 3 台とも同じ `image: svc/open-webui-computer:local` タグを指すため実質 1 回、加えて Docling の日本語 OCR イメージ `svc/docling-serve:local`）

   ```bash
   docker compose build
   ```

   > ⚠️ **ビルド対象は 2 つのままである。** リランクモデルはイメージに焼き込まず、
   > `reranker-model-init` サービスが実行時に取得する。このサービスは
   > `ghcr.io/open-webui/open-webui:main` をそのまま使うため追加のビルドは要らない。

4. **Computer を 3 台起動し、初回セットアップ URL を拾って管理者アカウントを作る**

   ```bash
   docker compose up -d open-webui-computer-a open-webui-computer-b open-webui-computer-c
   ./scripts/computer-urls.sh
   ```

   ユーザが 1 人も居ない間だけ有効な使い捨て URL が 3 台分表示される。ここで作ったアカウントが admin になる。

   > ⚠️ **`docker compose logs` に出る URL をそのまま開いてはいけない。** 3 台とも
   > `http://localhost:8000/?token=...` と表示され、**どれがどの Computer か区別できない**。
   > コンテナでは `--host 0.0.0.0` が必須で、cptr はそれを無条件に `localhost` へ潰し
   > （`cptr/cli.py`: `display_host = "localhost" if host == "0.0.0.0" else host`）、
   > ポートもコンテナ内部の 8000 のまま出力する。ホスト側の公開ポートを cptr に
   > 伝える環境変数は存在しないため、設定では直せない。違うのはトークンだけである。
   >
   > 混同しても **GET は 200 を返してセットアップ画面が開いてしまう**（トークンは
   > URL ではなく `POST /api/auth/setup` のボディで `compare_digest` 検証されるため。
   > `cptr/routers/auth.py`）。フォーム送信の瞬間に `403 invalid startup token` で
   > 初めて失敗が分かる。`scripts/computer-urls.sh` はポートを compose の実マッピングから
   > 引き、トークンを各コンテナの最新起動行から取るのでこの取り違えが起きない。
   >
   > ⚠️ **トークンは restart のたびに変わる**（`secrets.token_hex(32)` が `run()` 内で
   > 毎回生成される）。控えておく運用は必ず壊れるので、そのつどスクリプトを実行すること。

5. **各 Computer で LLM プロバイダを登録する**

   Settings → Admin → Connections（OpenAI / Anthropic の 2 種類のみ。環境変数では設定できない）。

6. **各 Computer で gateway API キーを発行し、`.env` に貼る**

   Settings → Admin → Gateway。`OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A` / `_B` / `_C` にそれぞれ貼る。

   > ⚠️ **平文（`sk-cptr-...`）は発行時に 1 回しか表示されない。** 取りこぼすと再発行が必要になる。

   併せて `OPEN_WEBUI_COMPUTER_ENABLED_A` / `_B` / `_C` を `true` にする。

7. **各 Computer で Codex にログインする**（使う場合のみ）

   ```bash
   docker compose exec open-webui-computer-a codex login --device-auth   # -b / -c も同様
   ```

   同じ ChatGPT アカウントで 3 回ログインする運用になる（レート制限は 1 アカウント共有）。

8. **Open WebUI の起動は Computer 3 台のキーを `.env` に入れた後に行う**

   ```bash
   docker compose up -d open-webui
   ```

   Open WebUI は起動時に有効な OpenAI 互換接続の `/v1/models` を引きに行くため、ステップ 6 のキー投入前に起動するとログが汚れる（致命的ではないが、キー未投入で先に起動した場合は `docker compose restart open-webui` で読み直させる）。

   > ⚠️ **初回は `reranker-model-init` が先に走り、モデルの取得（約 280MB）が終わるまで Open WebUI は起動しない。** 数分かかる。取得に失敗すると `dependency failed to start` になり Open WebUI は上がらない（`docker-compose.yml` の `open-webui` の `depends_on` が `condition: service_completed_successfully` のため）。ログは `docker compose logs reranker-model-init` で確認する。
   >
   > オフラインで起動したい場合は、`docker-compose.yml` の `open-webui` から `reranker-model-init` の 2 行（`depends_on` の項目）を消し、`.env` の `RAG_RERANKING_MODEL_AUTO_UPDATE` を `true` に戻す。この 2 つは対になっており、片方だけ変えると 4 つのワーカーが起動時に同時ダウンロードを始める。
   >
   > 2 回目以降はキャッシュを見るだけなので待たされない（`scripts/init_reranker_model.py` が `local_files_only=True` を先に試すため）。

9. **Admin Settings → Users → Groups で `team-a` / `team-b` / `team-c` を作る**

10. **既存環境では、層①の PersistentConfig 値を Admin Settings で手入力する**（新規構築では `.env` がそのまま初回起動時に取り込まれるため、このステップは不要）

    `.env` の値が反映されるかどうかは設定ごとに異なる。まず区別する。

    | 設定                                                                                                                                                                                                      | 反映方法                                                                                                                        |
    | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
    | `USER_PERMISSIONS_*`（22 件）/ `DEFAULT_USER_ROLE` / `AUDIT_LOG_LEVEL` / `TERMINAL_SERVER_CONNECTIONS` / `OPENAI_API_CONFIGS` / `OPENAI_API_BASE_URLS`                                                    | **PersistentConfig。既存 DB では `.env` は無視される。** 新規構築時のみ `.env` が効く。既存環境では Admin Settings で手入力する |
    | Basic RAG（`.env` 13 節）: `CONTENT_EXTRACTION_ENGINE` / `DOCLING_SERVER_URL` / `DOCLING_API_KEY` / `DOCLING_PARAMS` / `RAG_TEXT_SPLITTER` / `CHUNK_SIZE` / `CHUNK_OVERLAP` / `RAG_TOP_K`                 | **PersistentConfig**（`config.py:2828-2894`）。既存環境では Admin Settings → Tools → Documents で手入力する（ステップ 14）      |
    | `ENABLE_KB_EXEC` / `BYPASS_RETRIEVAL_ACCESS_CONTROL` / `ENABLE_RETRIEVAL_UNSCOPED_COLLECTIONS`                                                                                                            | 非 PersistentConfig（`env.py:787-906`）。`.env` の更新 + 再起動で効く                                                           |
    | `ENABLE_ADMIN_CHAT_ACCESS` / `ENABLE_ADMIN_EXPORT` / `BYPASS_ADMIN_ACCESS_CONTROL` / `BYPASS_MODEL_ACCESS_CONTROL` / `WEBUI_AUTH_COOKIE_SECURE` / `WEBUI_AUTH_COOKIE_SAME_SITE` / `OAUTH_ALLOWED_DOMAINS` | 非 PersistentConfig。`.env` の更新 + 再起動で効く                                                                               |

    既存環境（前節「移行手順」を経た環境）では、Admin Settings → Users → Groups → Default Permissions で `.env.example` §18 の `USER_PERMISSIONS_*` の値を手で入れる。`DEFAULT_USER_ROLE` は Admin Settings → Users → Default User Role、`AUDIT_LOG_LEVEL` は Admin Settings → Audit Log で同様に設定する。`TERMINAL_SERVER_CONNECTIONS`（ステップ 11）と `OPENAI_API_CONFIGS` / `OPENAI_API_BASE_URLS`（ステップ 12）も同じ理由で `.env` だけでは反映されない点は、各ステップ側にも記載する。

11. **Admin Settings → Integrations で Open Terminal 接続に 3 グループの read grant を付ける**

    `TERMINAL_SERVER_CONNECTIONS` の `access_grants` は、**新規構築の場合のみ** `[]` で seed される（意図的な初期値。空 / 未指定は「管理者のみ」に倒れる）。グループ ID はステップ 9 で作るまで存在しないため、この手順を先に行うことはできない。

    > ⚠️ **既存環境（移行の場合）では `[]` は適用されない。** 旧 compose は端末接続に `principal_type: "user"` / `principal_id: "*"`（全ユーザ read）を seed していたため、放置すると移行後も Open Terminal がログイン済み全員に見えたままになる。Admin Settings → Integrations で現在の `access_grants` を確認し、`principal_type: "user"` / `principal_id: "*"` の grant があれば **削除してから** 下記の 3 チーム分を追加すること。

    Open Terminal 接続の編集画面で、次を 3 チーム分追加する。

    ```json
    {
      "principal_type": "group",
      "principal_id": "<team-a の group_id>",
      "permission": "read"
    }
    ```

12. **Admin Settings → Connections で Computer 3 接続を確認・登録する**

    - **新規構築の場合**: `OPENAI_API_CONFIGS` / `OPENAI_API_BASE_URLS` は初回起動時に取り込まれているため、`http://open-webui-computer-a:8000/v1`（`-b` / `-c` も同様）の **API Type** が **Chat Completions** になっていることを確認するだけでよい。`Responses` になっていると `POST /v1/responses` が呼ばれ `Method Not Allowed`（405）になる。
    - **既存環境の場合**: `OPENAI_API_CONFIGS` / `OPENAI_API_BASE_URLS` も PersistentConfig なので、この 3 接続はそもそも作られない。Admin Settings → Connections で 3 本を手で追加する。**旧構成の `http://open-webui-computer:8000/v1` の接続は削除する**（サービスが存在しないため）。各接続の **API Type** は **Chat Completions** にする（gateway が実装しているのは `GET /v1/models` と `POST /v1/chat/completions` の 2 つだけで、`Responses` にすると 405 になる）。

13. **Workspace → Models で Model エントリを作り、`filterIds` / `actionIds` を紐づけて grant を付ける**

    詳細は次節「Functions の配布」を参照。

14. **Basic RAG を設定し、ナレッジベースを作ってチームに read grant を付ける**

    Basic RAG の構成は Embedding = OpenAI（`text-embedding-3-large`）/ 抽出 = Docling / ベクトル DB = PGVector。**ナレッジベースは管理者が作り、チームに read grant を配る**運用とする。

    **(a) 抽出エンジンと分割・検索の設定**

    新規構築では `.env` 13 節が初回起動時に取り込まれるため、`Admin Settings → Tools → Documents` で次を確認するだけでよい。既存環境では PersistentConfig のため `.env` が無視される。同じ画面で手入力する。

    | 項目                      | 値                                                                                                                     |
    | ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
    | Content Extraction Engine | Docling / `http://docling-serve:5001` / API キーは `.env` の `DOCLING_API_KEY`                                         |
    | Docling Parameters        | `{"do_ocr":true,"ocr_engine":"tesseract","ocr_lang":["jpn","eng"],"pdf_backend":"dlparse_v4","table_mode":"accurate"}` |
    | Text Splitter             | **トークン（Transformers）**                                                                                           |
    | Tokenizer Model           | **`hotchpotch/japanese-reranker-small-v2`**                                                                            |
    | Markdown Header Splitting | On                                                                                                                     |
    | Chunk Size / Overlap      | **440 / 64**                                                                                                           |
    | Hybrid Search             | **On**                                                                                                                 |
    | BM25 Weight               | **0**                                                                                                                  |
    | Reranking Engine          | **Default (SentenceTransformers)**                                                                                     |
    | Reranking Model           | **`hotchpotch/japanese-reranker-small-v2`**（Tokenizer Model と同じ値）                                                |
    | Reranking Batch Size      | **16**                                                                                                                 |
    | Top K                     | **40**                                                                                                                 |
    | Top K Reranker            | **20**                                                                                                                 |
    | Relevance Threshold       | **0.0**                                                                                                                |
    | Embedding Model           | `text-embedding-3-large`（OpenAI）                                                                                     |

    疎通は文書を 1 件アップロードして確認する。`docker compose logs docling-serve` に `POST /v1/convert/file` が出れば通っている。

    > ⚠️ **`do_ocr: true` は独自イメージが前提。** 公開イメージの tesseract は英語の言語パックしか持たない（docling-serve の `os-packages.txt` に `tesseract-langpack-eng` のみ）。`docker/docling/Dockerfile` で `tesseract-langpack-jpn` / `-jpn_vert` を足しているため成立する。**ステップ 3 の `docker compose build` を飛ばすと、日本語のスキャン PDF が文字化けした本文としてベクトル DB に入り検索結果を汚染する。** 疎通確認は `docker compose exec docling-serve tesseract --list-langs` に `jpn` が出るかで行う。

    > ⚠️ **縦書きは `ocr_lang` に `jpn_vert` を明示的に足す必要がある。** tesseract は自動で切り替えない。言語パックはイメージに入っているので、縦組みの文書を扱うときだけ `["jpn","jpn_vert","eng"]` にする（横組みの精度が落ちるため既定では入れていない）。`force_ocr` は true にしないこと（デジタル PDF にも強制的に OCR がかかり、遅くなるうえ精度も落ちる）。

    > ⚠️ **`RAG_EMBEDDING_MODEL` を後から変えてはいけない。** 次元が変わると既存コレクションと合わなくなり、再インデックスするまで検索が黙って空を返す。

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

    **(b) ナレッジベースの作成と grant**

    `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS` は `false`（既定のまま）。一般ユーザは `POST /api/v1/knowledge/create` が 401 になる（`routers/knowledge.py:286-292`）ため、**作成できるのは管理者だけ**である。管理者が Workspace → Knowledge で作成し、ステップ 9 で作ったグループに read grant を付ける。

    ```json
    {
      "principal_type": "group",
      "principal_id": "<team-a の group_id>",
      "permission": "read"
    }
    ```

    管理者は `filter_allowed_access_grants()` の対象外なので（`utils/access_control/__init__.py:249` で早期 return）、`USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ALLOW_SHARING=false` があっても管理者が付けたグループ grant は削られない。この運用が成立するのはこの分岐があるため。

    > ⚠️ **モデルにナレッジを紐づける場合、モデルの grant とは別にナレッジ自身の read grant が要る。** `retrieval/utils.py:1514-1521` の collection 分岐が `admin` / 所有者 / `AccessGrants.has_access(resource_type='knowledge')` / フォルダのいずれかを要求する。付け忘れると **モデルは選べるのに検索結果だけ 0 件**という、エラーの出ない壊れ方をする（ステップ 12 の「モデルピッカーには並ぶのに 400」と同じ形の食い違い）。

    > ⚠️ 一般ユーザの**利用**は塞がっていない。`GET /api/v1/knowledge/` は `get_verified_user` のみ（`routers/knowledge.py:130`）なので、read grant を貰ったナレッジベースはチャット入力欄の `#` から使える。チャットへの一回限りのファイル添付（`USER_PERMISSIONS_CHAT_FILE_UPLOAD`、既定 `true`）も全ユーザが使える。

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

15. **ユーザを招待し、承認してグループへ追加する**

    詳細は「ユーザの追加手順」を参照。

16. **（任意）Playwright MCP を起動して各 Computer に登録する**

    エージェントにブラウザで dev サーバを操作させたい場合のみ。人間が目で見るだけなら
    Computer の Browser タブで足りるため不要。手順は `docs/Setup/playwright_mcp_setup.md`。
    チーム間の分離は `svc-playwright-net-a` / `-b` / `-c` で保たれるが、
    **チーム内ではブラウザセッションを共有する**（既知の限界 10）。

---

## Functions の配布

Functions（Actions / Pipes / Filters）そのものには **アクセス制御が存在しない**。`function` テーブルに `access_control` 列も `access_grants` 列も無く、`access_grant` テーブルの `resource_type` 10 種（`calendar` / `channel` / `folder` / `knowledge` / `model` / `note` / `prompt` / `shared_chat` / `skill` / `tool`）にも `function` は含まれない。制御軸は次の 2 つだけ。

- `is_active` — 有効 / 無効
- `is_global` — 全モデル・全ユーザへの強制適用。**本プロジェクトでは 3 つの Function すべてでオフにする。** オンにするとモデル単位の出し分けが意味を失う

Function を有効化するとモデルが 1 つ生えるが、`model` テーブルに対応する行が無いため一般ユーザには見えない（`utils/models.py:517-523` の `elif user.role == 'admin'` に落ちる）。可視性を出すには **Workspace → Models で Model エントリを作る** 必要がある。

> ⚠️ **生えるモデルの ID に `pipe.` という接頭辞は付かない。** `functions.py:129-133` は単一 Pipe（`pipes` 属性を持たない Function）に対して **Function の ID をそのままモデル ID にする**。`descript_pipe.py` から生えるモデルは `descript_pipe` であって `pipe.descript_pipe` ではない。`<function_id>.<sub_id>` 形式になるのは `pipes` を持つマニフォールドの場合だけ（`functions.py:104`）。

| Model エントリ     | base            | 紐づけ                                                        | grant                                                 |
| ------------------ | --------------- | ------------------------------------------------------------- | ----------------------------------------------------- |
| Descript 編集      | `descript_pipe` | `filterIds: [descript_guard]`, `actionIds: [descript_studio]` | `group: team-a` / `team-b` / `team-c` の read を 3 つ |
| Computer（team-a） | `cptr/codex-a`  | —                                                             | `group: team-a` の read                               |
| Computer（team-b） | `cptr/codex-b`  | —                                                             | `group: team-b` の read                               |
| Computer（team-c） | `cptr/codex-c`  | —                                                             | `group: team-c` の read                               |

Descript の Pipe は 3 チームで同一機能を使うため、Model エントリは 1 つに 3 グループの read grant を付ける形でよい。Computer は接続 URL がチームごとに異なる（`open-webui-computer-a` / `-b` / `-c` の 3 接続）ため、gateway モデルは最初から 3 エントリに分かれる。gateway モデルの実 ID は各 Computer のワークスペース名で決まるので、`GET /api/models` かモデルピッカーで実際の値を確認してから登録すること。

未登録のモデルは管理者にしか見えない。Model エントリを作るまで、一般ユーザからは Descript の Pipe も Computer のワークスペースも不可視のままになる。

### ⚠️ `BYPASS_ADMIN_ACCESS_CONTROL=false` にすると管理者も未登録モデルを使えない

「未登録モデルは管理者にしか**見えない**」は可視性の話で、**使える**こととは別である。`BYPASS_ADMIN_ACCESS_CONTROL=false` にすると `main.py:1077` の条件が管理者でも真になり、管理者も `check_model_access` を通る。同関数は `model` テーブルに行が無いモデルを問答無用で拒否する（`utils/models.py:447`）。

結果として **「モデルピッカーには並ぶのに、選んで送信すると 400 `Model not found`」** という状態になる。可視性を決める `get_filtered_models` は未登録モデルを管理者に見せ（`utils/models.py:521-523`）、実行可否を決める `check_model_access` は同じモデルを拒否するため、両者が食い違う。

実測（2026-08-28、この構成）:

| モデル                                           | `main.py:1070-1115` の結果 |
| ------------------------------------------------ | -------------------------- |
| Model エントリのあるモデル                       | OK                         |
| `descript_pipe`（素の Pipe モデル）              | `Model not found`          |
| `gpt-5.4` / `gpt-4o-mini` など素の OpenAI モデル | `Model not found`          |
| `cptr/codex-a`（Model エントリ未作成）           | `Model not found`          |

MODELS プール 122 件に対し Model エントリが 2 件しか無かったため、`POST /api/chat/completions` は全件 400 になっていた。**そのため本プロジェクトでは `BYPASS_ADMIN_ACCESS_CONTROL=true`（upstream 既定）を採る。** 一般ユーザ側の制限は `BYPASS_MODEL_ACCESS_CONTROL=false` が担うため、管理者側を true にしても一般ユーザが未登録モデルを使えるようにはならない。

なお **Action → Pipe の RPC はこの影響を受けない。** `descript_studio.py` が `generate_chat_completion(..., bypass_filter=True)` で呼ぶため `chat.py:198` の検査を飛ばし、`descript_pipe` に Model エントリが無くても到達できる。壊れるのは「チャット UI でモデルを選ぶ」経路だけである。

---

## ユーザの追加手順

新規ユーザは `DEFAULT_USER_ROLE=pending` で作られ、承認されるまでログインできない。アカウント作成の完了条件は次の 2 手。

1. **Admin Settings → Users で承認する**（`pending` → `user`）
2. **同じ画面で所属グループ（`team-a` / `team-b` / `team-c` のいずれか）へ追加する**

   > ⚠️ **承認だけでは何も見えない。** グループ未所属のユーザには、Open Terminal も Descript の Pipe も Computer のワークスペースも表示されない（すべてグループ grant で可視性を付けているため）。承認とグループ追加は必ずセットで行う。

グループ追加後、各ユーザは次を自分で行う必要がある（自動化不可）。

3. **チャット入力欄の ＋ → Integrations → Tools から Descript を手動で有効化し、OAuth 同意を完了する**

   Descript の認証はユーザ単位で分離されている（`utils/tools.py:158-167` が MCP の OAuth トークンを `user.id` で引く）が、この同意フローはチャット補完リクエストの最中にブラウザリダイレクトを起こせないため、事前の手動有効化が必須。詳細は `docs/Setup/descript_functions_setup.md` の「4. OAuth 同意を完了する」を参照。

---

## 既知の限界

本設計が守るのは事故であって悪意ではない。以下は Enterprise License（Terminals オーケストレータ）無しでは解決できない。

| #   | 限界                                                                                                                               | 影響                                                                                                                                                                                                                                                                                                              |
| --- | ---------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Open Terminal は他人の PTY セッションにアタッチできる。** ファイル分離は本物だが、セッション・プロセス出力・ポートは全ユーザ共有 | 同一 Terminal を使う全ユーザが、他人の PTY セッションを一覧・アタッチ・削除でき、実行中コマンドの文字列と出力を読める                                                                                                                                                                                             |
| 2   | Open Terminal のポート共有                                                                                                         | ユーザがバインドしたポートに他ユーザの proxy URL から到達できる                                                                                                                                                                                                                                                   |
| 3   | Open Terminal のリソース共有                                                                                                       | CPU / メモリ / `OPEN_TERMINAL_MAX_SESSIONS` は全ユーザで分け合う（コンテナ全体の合計上限で、ユーザ単位ではない）                                                                                                                                                                                                  |
| 4   | Open Terminal のユーザ名衝突                                                                                                       | ユーザ ID の先頭 8 文字が一致する 2 人は同じ OS アカウントに合流する（10 名規模では実質無視できる）                                                                                                                                                                                                               |
| 5   | **同一チーム内の Computer には分離が無い。** 境界はチームのコンテナだけ                                                            | 同一チームのメンバーは互いのファイル・端末セッション・gateway キーに到達できる                                                                                                                                                                                                                                    |
| 6   | **Computer のロール降格は最大 30 日効かない。** JWT が DB を読まないため                                                           | 管理者がユーザを降格しても、既存の JWT クッキー（有効期限 30 日）はフルアクセスのまま生き続ける。即時失効には `config.toml` の `[server] secret` ローテートが必要で、全員ログアウトとプロバイダキー復号不能を伴う                                                                                                 |
| 7   | Computer の gateway キー一覧・削除が無フィルタ                                                                                     | `GET /v1/keys` / `DELETE /v1/keys/{id}` に所有者検査が無い（コンテナ境界（チーム別 3 台化）で緩和されるが、チーム内では有効ではない）                                                                                                                                                                             |
| 8   | Functions の read-modify-write レース                                                                                              | `Chats.update_chat_by_id` がチャット JSON を丸ごと置換するため、同一チャットの同時操作で state が後勝ちで失われうる。所有者検証（本手順書の対象）とは別の課題で、今回のスコープには含めない                                                                                                                       |
| 9   | 監査の粒度                                                                                                                         | Computer の `OPEN_WEBUI_COMPUTER_AUDIT_LOG_LEVEL` は API レベルの記録であり、端末操作の全トランスクリプトではない                                                                                                                                                                                                 |
| 10  | **Playwright MCP のブラウザセッションはチーム内で 1 本。**                                                                         | `stdio_manager` が `server_id` だけでクライアントを使い回すため（`self._instances: dict[str, MCPClient]`）、Computer コンテナにつきブラウザは 1 つ。同一チームの別ユーザ・別チャットとタブを共有し、同時に触ると互いの画面が飛ぶ。ログイン済みの状態も共有される（限界 5 と同じ境界であり、新しい越境先ではない）。**Chrome + noVNC を有効にすると、この共有は再起動をまたいで永続化する** → 限界 14。なお Playwright MCP 自体は接続クライアントごとにセッションを分ける能力を持つ（実測）。現状それを使えていないのは cptr 側の事情だが、**noVNC 構成（CDP + isolated=false）はその能力自体を無効化する**ため、将来ユーザ単位の分離を入れる際の選択肢が狭まる |
| 11  | **ブラウザ操作は監査に残らない。**                                                                                                 | cptr の監査は ASGI ミドルウェア（`utils/audit.py`）で**受信 HTTP のみ**を記録する。MCP ツール呼び出しは cptr からの送信なので対象外。Playwright MCP 側も既定でリクエストログを出さない。限界 9 の延長で、「どのページを開いたか」は残らない                                                                       |

| 12 | **管理者は RAG 経由で全ユーザの添付ファイルとナレッジを読める。** | `filter_accessible_collections()` は `user.role == 'admin'` で無条件に全コレクションを通す（`retrieval/utils.py:1289`）。`BYPASS_ADMIN_ACCESS_CONTROL` とは別系統の管理者バイパスで、設定では閉じられない。層①の他の管理者権限（`ENABLE_ADMIN_CHAT_ACCESS` 等）と同じ位置づけ |

| 13 | **noVNC の認証は共有パスワードであって、ユーザ認証ではない。**（Chrome + noVNC を使う場合のみ） | VNC のパスワードは Open WebUI / Computer のアカウントと無関係で、**誰が見たかは記録されない**。`CHROME_NOVNC_VNC_PASSWORD_A/B/C` をチームごとに分けることでチーム間の越境は防げるが、チーム内に閲覧者の区別は無い。画面にはエージェントが開いたページがそのまま映るため、ログイン済みセッションやフォームの入力内容も見える。compose の `ports:` は既定で 0.0.0.0 に bind するため同一 LAN から届く（Computer の 8001-8003 と同じ露出）。限界 5 と同じ境界だが、**認証層が 1 つ増える**点が新しい |
| 14 | **Chrome のプロファイルがチーム内で永続共有される。**（Chrome + noVNC を使う場合のみ） | `chrome-*-profile` ボリュームに Cookie とログイン状態が残る。従来の `PLAYWRIGHT_MCP_ISOLATED=true` は「プロファイルをディスクに残さない」設定だったため、**この構成を入れると挙動が変わる**。あるユーザがエージェントに行わせたログインは、同じチームの別ユーザのエージェントからも、コンテナ再起動後もそのまま使える。消すには `docker compose down` 後に `docker volume rm <project>_chrome-a-profile` |

1〜3 を解決するには Terminals オーケストレータ（Enterprise License）が必要である。5 を解決するにはユーザごとの Computer コンテナが必要で、10 名なら 10 コンテナになる（10 / 11 / 13 / 14 も 5 と同じ根で、Computer を分ければ同時に解消する）。
