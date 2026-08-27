# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## このリポジトリの性質

「ショート動画作成ツール」の**設計ドキュメント + アプリ基盤（Open WebUI フォーク）を束ねるルートリポジトリ**。
アプリ本体のソースはこのリポジトリ直下には存在せず、`full-stack/open-webui` が git submodule
（fork: `https://github.com/A-clear/open-webui.git`）として配置されている。

```
docs/                       設計ドキュメント（成果物の中心）
  RequirementDefinition/    要件定義: BA(業務) / AA(情報システムフロー) / TA(システム構成) / DA(ER + 外部API仕様)
  DetailedDesign/           詳細設計: authorization_flow.mmd / movie_flow.mmd
                            functions_contract.md ← 3 つの Functions が共有する契約の正本
  Setup/                    セットアップ手順書: descript_functions_setup.md / codex_agent_setup.md
                            multi_user_setup.md / playwright_mcp_setup.md
  superpowers/specs/        設計ドキュメント（マルチユーザ対応の設計と根拠）
  superpowers/plans/        実装計画（タスク分割と検証手順）
functions/                  Open WebUI Functions のソース（ここで開発し、GitHub URL 経由で Open WebUI に取り込む）
  descript_studio.py        Action（マルチ 4 サブ: upload / edit / export / probe）
  descript_pipe.py          Pipe（MCP オーケストレーション。単一モデル）
  descript_guard.py         Filter（RAG 迂回 / 署名 URL 恒久化 / 事前診断）
scripts/                    運用スクリプト
  check_functions.py        Functions の静的検証
  init_reranker_model.py    リランクモデルの事前取得とロード検証
                            （compose の reranker-model-init から実行）
  computer-urls.sh          Computer 3 台のアクセス URL を出す
full-stack/open-webui/      Open WebUI フォーク（submodule）
.env.example                スケーリング構成の環境変数テンプレート
docker/docling/             Docling の日本語 OCR イメージ
  Dockerfile                公式イメージに tesseract-langpack-jpn / -jpn_vert を追加
docker/computer/            Open WebUI Computer の開発イメージ
  Dockerfile                Node/pnpm/TS/codex/mcp-remote を追加 + cptr のパッチ 2 件
  browser-root-relative-shim.js
                            Browser タブ（proxy）に注入するシム。実行時 root-relative
                            URL をプロキシのフレームパスへ書き換える
docker-compose.yml          PostgreSQL(pgvector) / Redis / MinIO / Open WebUI
                            + Docling（Basic RAG のコンテンツ抽出）
                            + Open Terminal（組み込みマルチユーザ）
                            + Open WebUI Computer × 3（チーム別）
                            + Playwright MCP × 3（チーム別）
```

**`docs/DetailedDesign/functions_contract.md` が Functions 実装の正本。** 1 Function = 自己完結 1 ファイルの制約上、共通ヘルパは 3 ファイルに重複展開されている。コピー元は常にこの契約書であり、ヘルパを直す場合は契約書を先に直してから 3 ファイルへ反映する。

ドキュメント・コミットメッセージ・ブランチ名は日本語。コミットは `add:` / `mod:` / `feature:` の接頭辞を使う。

## submodule の扱い

```bash
git submodule update --init --recursive     # 初回チェックアウト時に必須
git submodule status                        # 現在ピン留めされているコミットの確認
```

`full-stack/open-webui` 配下を変更した場合は、**submodule 内でコミット → 親リポジトリで submodule ポインタをコミット**の 2 段階が必要。

フォーク独自の差分は現時点で 1 コミットのみ（upstream との乖離を最小に保つ方針）:

- `.open-webui/agents.txt`, `llms.txt`, `llms-full.txt` — Open WebUI 公式ドキュメントのエージェント向けインデックス。Open WebUI の仕様を調べるときはまずここ（`https://docs.openwebui.com/api/search?q=...`、任意ページに `.md` を付けると Markdown 取得）を参照する

Open WebUI 本体（`backend/open_webui/`, `src/`）への改変は原則行わない。upstream 追従が壊れるため、機能追加は後述の Functions として実装する。

**submodule を書き換えたくなったら、まず親リポジトリ側で解決できないか検討する。** `kamegin4-aws` は `A-clear/open-webui` に push 権限を持たない（`pull` のみ）ため、submodule にコミットすると親が push できない submodule コミットを pin してしまい、`git push` が `must name a ref` で落ちる。実例として `WEBUI_SECRET_KEY` の自動生成は `backend/dev.sh` を書き換えず、親側の `scripts/dev-backend.sh`（ラッパー）に逃がしてある。

## 開発コマンド

フロントエンド（SvelteKit :5173）とバックエンド（FastAPI :8080）を別プロセスで起動する構成。

```bash
# バックエンド（Python 3.11 / venv は backend/venv に既存）。ルートで実行
./scripts/dev-backend.sh      # 鍵を用意 → venv 有効化 → submodule の backend/dev.sh に exec

# フロントエンド
cd full-stack/open-webui
npm run dev                   # pyodide 取得 → vite dev --host（:5173）

# ビルドが要るのは Computer（公式イメージに開発ツールが無い）と
# Docling（公式イメージに日本語の tesseract 言語パックが無い）の 2 つ。
# Computer 3 台は同じ image: タグを指すため、これで足りる
docker compose build

# Computer 3 台のアクセス URL を出す（初回セットアップ URL / ログイン URL を出し分ける）
./scripts/computer-urls.sh

# Docker 一括起動
make install                  # docker compose up -d
make startAndBuild            # 再ビルドして起動
```

Functions の静的検証（ルートで実行。ファイルを生成せず、ネットワークも使わない）:

```bash
python3 scripts/check_functions.py    # 0 = 問題なし / 1 = 問題あり
```

frontmatter、クラス属性の位置、`stream` の引数名、`replace_imports` が壊す import、**共通ヘルパの 3 ファイル間ドリフト**、`descript_op` とエラーコードのファイル間整合、秘匿情報の直書きを一括で検査する。**Functions を編集したら必ず実行すること。**

品質チェック（submodule 側）:

```bash
npm run lint                  # frontend(eslint) + types(svelte-check) + backend(pylint) を順に実行
npm run lint:frontend         # eslint . --fix
npm run check                 # svelte-kit sync && svelte-check
npm run format                # prettier
npm run format:backend        # ruff format
npm run test:frontend         # vitest（--passWithNoTests。テストは事実上未整備）
npx vitest run path/to/x.test.ts   # 単一テスト実行
npm run cy:open               # Cypress（E2E、upstream 由来）
```

バージョン制約: Node は `>=18.13.0 <=22.x.x`、Python は `>=3.11, <3.13`。Node 22 系でも 22.13 未満だと一部 devDependency が解決できないため、22.13〜22.x を使う。

## アーキテクチャ（設計の要点）

実際の動画編集は自前実装せず **Descript に委譲**する。Open WebUI はチャット UI とオーケストレーションの器として使う。

- 認証: Google OAuth Client による OIDC（`docs/DetailedDesign/authorization_flow.mmd`）
- LLM: OpenAI（編集指示 → Descript 向け編集プロンプトの生成）
- 動画編集: Descript MCP → Descript App
- 永続化: PostgreSQL / PGVector / Redis / MinIO
- 実行環境: Open Terminal / Open WebUI Computer（下記）
- 文書 RAG: OpenAI Embedding / Docling / PGVector（下記）
- ホスティング想定: Vercel（`docs/RequirementDefinition/TA/system_architecture.mmd`）

### Basic RAG（Embedding = OpenAI / 抽出 = Docling / ベクトル DB = PGVector / リランク = SentenceTransformers）

抽出・埋め込み・ベクトル DB の 3 点は**マルチユーザだから必要になる**選択で、独立した好みではない。既定の SentenceTransformers（埋め込み）は**ワーカーあたり約 500MB**、既定の pypdf は継続的な取り込みでメモリリーク、既定の ChromaDB は SQLite ベースで fork-safe ではない。ここに日本語特化のリランクとトークナイザ（`hotchpotch/japanese-reranker-small-v2`）が加わる。設定は `.env.example` §13 に集約し、判断の根拠は `docs/superpowers/specs/2026-08-27-multi-user-design.md` §4.7 と `docs/superpowers/specs/2026-08-28-japanese-rag-reranker-design.md` が正本。

- **ナレッジベースは管理者が作り、チームに read grant を配る。** `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS` は upstream 既定の `false` のまま。一般ユーザの作成は 401 になる（`routers/knowledge.py:286-292`）が、**利用は塞がらない**（`GET /api/v1/knowledge/` は `get_verified_user` のみ。同 `:130`）。管理者は `filter_allowed_access_grants()` の対象外（`utils/access_control/__init__.py:249` で早期 return）なので、`..._ALLOW_SHARING=false` があっても管理者が付けたグループ grant は削られない
- **モデルに紐付けたナレッジは、モデルの grant とは別にナレッジ自身の read grant が要る**（`retrieval/utils.py:1514-1521`）。付け忘れると「モデルは選べるのに検索結果だけ 0 件」になり、エラーが出ないので原因が分からない
- **`BYPASS_RETRIEVAL_ACCESS_CONTROL` と `ENABLE_RETRIEVAL_UNSCOPED_COLLECTIONS` を true にしてはいけない。** 前者は `retrieval/utils.py:1489 / 1565 / 1590 / 1598` の 4 箇所で所有者検査を飛ばし、クライアントが送った `collection_name` をそのままベクトル DB へ投げる。どちらも PersistentConfig ではない素の環境変数（`env.py:787` / `:793`）なので、`.env` に書けば再起動のたびに効く。`.env.example` §18 で明示的に `false` を固定している
- **Docling はビルドが要る。** 公開イメージの tesseract は英語の言語パックしか持たない（docling-serve の `os-packages.txt` に `tesseract-langpack-eng` のみ）ため、`docker/docling/Dockerfile` で `tesseract-langpack-jpn` / `-jpn_vert` を足している（CentOS Stream 9 の **AppStream** にあり EPEL は不要。ベースが `quay.io/sclorg/python-312-c9s:c9s` のため）。ビルドせずに公式イメージのまま `do_ocr: true` にすると、日本語のスキャン PDF が文字化けした本文としてベクトル DB に入り検索結果を汚染する。Dockerfile 末尾の `test -f` が言語パックの実在を検証しており、パスやパッケージ名が変わったら**ビルドがその場で失敗**する
- **縦書きは `ocr_lang` に `jpn_vert` を明示的に足す必要がある。** tesseract は自動で切り替えない。言語パックはイメージに入れてあるので設定だけで済む（横組みの精度が落ちるため既定は `["jpn","eng"]`）
- **docling-serve の `/opt/app-root/src/.cache/docling/models` にボリュームを張ってはいけない。** 変換モデルはイメージに焼き込み済みで、空のボリュームで覆うと自動ダウンロードせず実行時エラーになる。同じ理由で `svc-docling-net` は `internal: true` にできている
- **docling-serve 側の `UVICORN_WORKERS` は 1 固定**（既定の LocalOrchestrator がタスクをプロセス内メモリに持つため、2 以上だと Task Not Found (404)）。Open WebUI 側の `UVICORN_WORKERS`（`.env` §6、現在 4）とは名前が同じだけの別物
- **`RAG_EMBEDDING_MODEL` を後から変えない。** 次元が変わると既存コレクションと合わず、再インデックスするまで検索が黙って空を返す
- **リランカーはハイブリッド検索が有効なときしか呼ばれない。** `RerankCompressor` が現れるのは `query_doc_with_native_hybrid_search`（`retrieval/utils.py:434`）と `query_doc_with_hybrid_search` の 2 経路だけで、通常の `query_doc` には存在しない。そのうえで **`RAG_HYBRID_BM25_WEIGHT=0`** にしている — PGVector の native hybrid search は `to_tsvector('simple', ...)` を使い（`retrieval/vector/dbs/pgvector.py:566-578`）、`simple` は空白区切りなので**日本語の文は丸ごと 1 トークンになる**。フォールバックの `BM25Retriever` も `preprocess_func` 未指定で同じ。設定で差し替える口は無い。ハイブリッドは reranker を起動するためのスイッチとして使っている。0 なら `if bm25_weight > 0` のガード（`pgvector.py:562`）で FTS の SQL 自体が発行されない
- **モデルのロード失敗は「Hybrid Search が勝手に off に戻る」として現れる。** `routers/retrieval.py:1185-1187` が例外を握って `ENABLE_RAG_HYBRID_SEARCH = False` にするだけで、HTTP エラーを返さない。原因は `log.error` の 1 行にしか出ない。compose の **`reranker-model-init`** サービスは、この失敗を `docker compose up` 時点の明示的なエラーへ前倒しするために存在する。**このサービスの image は `open-webui` 本体と同一でなければならない**（`huggingface_hub` のバージョンとキャッシュのレイアウトを一致させるため）
- **`RAG_RERANKING_MODEL` を空にすると、エラーではなく「別のもの」が動く。** `get_rf` が `None` を返し（`routers/retrieval.py:173 / 176 / 240`）、`RerankCompressor` が**埋め込みベースの再スコアへ静かにフォールバックする**（`retrieval/utils.py:1743-1758`）。全候補に対して毎クエリ OpenAI の embeddings が追加で叩かれ、精度は上がらないのに課金だけ増える
- **`RAG_RERANKING_MODEL` と `RAG_TOKENIZER_MODEL` は同じ値でなければならない。** 前者はチャンクを読む側、後者は `RAG_TEXT_SPLITTER=token_transformers` のときチャンクを切る側（`routers/retrieval.py:1608-1615`）。食い違うとチャンクがリランカーの 512 トークン窓に収まらず、先頭しか読まれない。エラーは出ない。`docker-compose.yml` では **`RERANKER_MODEL_ID` 1 つから両方を導いて**構造的に防いでいる
- **モデルはワーカー起動時に、ワーカーごとに読まれる。** `main.py:624` の `get_rf` は lifespan の中で走るため遅延ロードではなく、`UVICORN_WORKERS=4` なら 4 プロセスが各自モデルを保持する（重みだけで 280.6MB × 4）。torch は既定でホストの全コアを使おうとし Open WebUI 側に上限は無いので、compose で **`OMP_NUM_THREADS=1`**（ホスト 4 コア ÷ ワーカー 4）を渡している。`onnx` バックエンドは `optimum` 未同梱のため選べない

### 実行環境の 3 コンポーネント（compose のみ。Functions とは別レイヤ）

動画編集そのものを Descript に委譲する方針は変わらない。ここは FCPXML の検査や ffprobe での確認など「手元でファイルを触る」ための面。

- **Open Terminal** — Open WebUI の**部品**。バックエンドがプロキシし、チャットのサイドバーに出る実行環境。ホストにポートを公開していない（API キーをブラウザに渡さないため）。`OPEN_TERMINAL_MULTI_USER=true` でユーザごとに Linux アカウントと home（`/home/<ユーザ ID の先頭 8 文字>`）が作られる。**分離されるのはファイルだけで、PTY セッション・プロセス出力・ポートは全ユーザ共有**（他人の稼働中シェルにアタッチできる）
- **Open WebUI Computer（cptr）** — **独立した 1 つのアプリ**。自前のログイン・PWA・エージェントランタイムを持ち、Open WebUI からは gateway 経由で `cptr/<workspace>` というモデルに見える。ポート公開が必須（ブラウザが直接 Socket.IO でつなぐ）。**compose でビルドする 2 サービスのうちの 1 つ**（`docker/computer/Dockerfile`。もう 1 つは `docker/docling/Dockerfile`）。**チーム別に 3 台**（`open-webui-computer-a` / `-b` / `-c`、ホストポート 8001 / 8002 / 8003）。cptr の `routers/workspace.py` と `routers/terminal.py` に所有者検査が無いため、コンテナ境界が唯一の分離手段。

  **初回セットアップ URL は `docker compose logs` から読んではいけない。`./scripts/computer-urls.sh` を使う。** 3 台とも `http://localhost:8000/?token=...` と出力され区別できないため（`cptr/cli.py` が `--host 0.0.0.0` を無条件に `localhost` へ潰し、ポートもコンテナ内部の 8000 のまま出す。差し替える環境変数は無い）。違うのはトークンだけ。取り違えても **GET は 200 でセットアップ画面が開いてしまい**、送信時に初めて `403 invalid startup token` になる（トークンは URL ではなく `POST /api/auth/setup` のボディで `compare_digest` 検証される）。**トークンは restart のたびに変わる**ので控えても無駄

- **Codex（coding agent subscription）** — ChatGPT サブスクリプションを API キー無しで使う。**Open WebUI ではなく Computer 側の機能**で、設定は Computer の Settings → Admin → Agents。手順は `docs/Setup/codex_agent_setup.md`

### Computer 内で立てた開発サーバをホストで見る

**Computer の Browser タブ / Port Preview を使う。** HTTP プロキシ型で、`http://localhost:5173` をそのまま開ける。実装は `cptr/routers/browser.py` と `cptr/utils/browser/proxy.py`。ループバック宛てのときだけ ES module の root-relative import を書き換える分岐があり（`rewrite_javascript()`）、dev サーバを見るための機能として作られている。フロントの `browser-runtime.js` が `fetch` / `XMLHttpRequest` / `WebSocket` / `EventSource` / `pushState` を差し替え、WebSocket は `/api/browser/sessions/{id}/ws` でトンネルされるので **HMR も通る**。ホストからはチーム別ポート（`:8001` / `:8002` / `:8003`）経由で見えるため**ポート公開は不要**。

**ポート公開は廃止した。** 固定ポートの公開は複数人と構造的に両立しない。Computer は 1 コンテナ = 1 つの Linux ユーザ空間で、2 人目の `vite` は 5173 を取れず（EADDRINUSE）既定の `strictPort: false` のまま 5174 へずれる。5174 は公開していないのでホストから見えない（`uvicorn` は自動でずれず起動失敗する）。Browser タブ / Port Preview は任意のポートで動きマッピングが要らないため、こちらに一本化した。

**Browser タブ側の落とし穴:**

- **モードは `proxy` のままにする。** Settings → Admin → **Browser** → **Browser tab default**。もう一方の `chrome` は CDP でスクリーンキャストする方式で、**このイメージに Chrome は入っていない**（実測: `command -v chromium google-chrome` → 127）。既定のまま選ぶと `409` で失敗する。同じ画面の **Agent browser tools**（`browser.enabled`）はエージェント用のブラウザ _ツール_（`browser_navigate` / `_snapshot` / `_click` / `_type` / `_screenshot` / `_evaluate` の 6 つ）を生やす別物。**どちらもリモート CDP を指せば Chrome 無しで動く** — 下の「ブラウザ操作をエージェントに持たせる」を参照。ただし `chrome` モードでリモート CDP を使うには **`browser.tab_chrome_source` を `personal` にする必要があり**（そうでないと `browser.cdp_url` は完全に無視され、Chrome を自前起動する経路に落ちて 409 になる）、さらに **管理者ロール限定**（`routers/browser.py:253` `"Personal Chrome is available to administrators only"`）。マルチユーザ前提の本構成では**非管理者が Browser タブを使えなくなる**ため採用していない
- **実行時に JS が組み立てた root-relative URL は書き換えられない**（→ `docker/computer/Dockerfile` の「4b. cptr のパッチ」で対処済み）。`utils/browser/proxy.py` が書き換えるのは **初期 HTML の属性 / CSS の `url()` / JS の import 文だけ**。Vite の dev サーバはアセットを必ず root-relative で返すため（実測: `GET /src/assets/hero.png?import` → `export default "/src/assets/hero.png"` という**裸の文字列**）、React が `<img src>` に入れた時点で iframe のオリジン（= cptr 自身）に解決され、cptr は SPA フォールバックで **200 / `text/html`** を返す。**404 にならない**ので原因が分かりにくい。`<use href="/icons.svg#id">` も同じ。**アプリ側では直せない** — Vite 8 の dev は `base: './'` を無視し、`new URL('./x.png', import.meta.url)` も root-relative に変換する（どちらも実測）
- **Files パネルの Ports 一覧に出るのは、Computer の Terminal から起動したサーバだけ。** `routers/events.py` が 3 秒ごとに `/proc/net/tcp` を走査するが、`_find_session_for_pid()` が `cptr.utils.terminal` のセッションの子孫に絞る。**エージェントの `run_command` で起動したサーバは `command_sessions`（`utils/tools.py` の別レジストリ）配下なので一覧に出ない** — Browser タブに URL を直打ちすれば普通に開ける
- 走査は `/proc/net/tcp` のみで **IPv6 専用の listen は検出されない**。ポート 22 / 53 / 80 / 443 / 631 / 5353 も除外される
- Ports 一覧は `session_id` を付けるが**所有者でフィルタしていない**。ユーザ間分離が無い設計と整合しているだけで、他人のポートも見える

### ブラウザ操作をエージェントに持たせる（Playwright MCP / リモート CDP）

「エージェントに画面を触らせて dev サーバを検証させたい」場合の選択肢。**Chrome をイメージに入れる必要は無い。**
実測: このイメージには Chromium が要求する共有ライブラリが 1 つも無い（`libnss3` / `libatk-1.0` / `libgbm` / `libxkbcommon` / `libasound` / `libcups` / `libpango` / `libdrm` / `libxcomposite` すべて `ldconfig -p` で 0 件）。`sudo` も apt も使えないので、**コンテナ内で `npx playwright install` しても起動時に落ちる**。ブラウザ本体は必ず外に置く。

- **Playwright MCP（構築済み）** — `docker-compose.yml` の `playwright-mcp-a` / `-b` / `-c`。手順と落とし穴は `docs/Setup/playwright_mcp_setup.md` が正本。cptr 0.9.21 は本物の MCP クライアントを持ち（`cptr/utils/mcp/client.py`。`mcp` 1.29.0 同梱済みで `pip install 'cptr[mcp]'` は不要）、Settings → Admin → **Tools** に `openapi` / `mcp`（Streamable HTTP）/ `mcp_stdio`（プロセス起動）の 3 種を登録できる（`routers/admin.py:585`）。**ただし `type: mcp` で登録してはいけない。** cptr は `type: mcp` のときツール呼び出しのたびに接続して切断するため（`utils/tools.py:2931-2940`）、MCP セッションが毎回作り直され `browser_navigate` の次の `browser_snapshot` が **`about:blank` になる**（`--shared-browser-context` でも直らず、前のタブは `browser_tabs` の一覧からも消えることを実測）。`mcp_stdio` は `stdio_manager` がプロセスを呼び出しをまたいで生かすためセッションが保たれる。そこで **`command: mcp-remote` / `args: http://playwright-mcp-a:8931/mcp --transport http-only --allow-http`** としてstdio ↔ HTTP のブリッジを噛ませる（`mcp-remote` は `docker/computer/Dockerfile` で焼き込み済み。`npx` にすると初回ツール呼び出しでネットワーク取得が走る）。**`--allow-http` を忘れると cptr 側には `Connection closed` としか出ない。****`mcp_stdio` で `npx @playwright/mcp` をコンテナ内に生やす形は上記の理由で不可**
- **リモート CDP** — cptr 内蔵の Agent browser tools と Browser タブの `chrome` モードは、どちらも `browser.cdp_url` を見る。**`browser.auto_launch` を off にすると `ensure_browser()`（localhost 決め打ちで Chrome を探す）を通らず、リモートの CDP に直接つなぐ**（`utils/tools.py:1822-1836`、`routers/browser.py:208`）。`browserless/chrome` などをサイドカーに置いて `http://chrome:9222` を指す
- **クラウド API** — `browser.provider` は `local` / `firecrawl` / `browser_use` の 3 択（Admin → Browser の `<select>`）。ブラウザを自前で持たない代わりに API キーと課金が要る

**Computer 側で「Computer use」モデル（gpt-5.4 以降）は使えない。** cptr のソース全体に `computer_use` / `computer-use` の文字列が 1 つも無く、アクションループが実装されていない。`browser_screenshot` は PNG を `<workspace>/.cptr/screenshots/` に保存して**ファイルパスの文字列を返すだけ**で、画像がモデルへ戻らない（`utils/tools.py:1901-1931`）。ブラウザ操作は Playwright MCP の既定の **Snapshot モード**（アクセシビリティツリー）で行う — ツール呼び出しができるモデルなら何でも動き、座標クリックより安定して安い。座標が要るときだけ `--caps=vision` を足す。

**dev サーバの bind に注意。** ブラウザが別コンテナに居るので、`http://localhost:5173` では届かない。dev サーバを `--host 0.0.0.0` で立て、`http://open-webui-computer-a:5173` のようにチーム別サービス名で指す。

### Computer の開発イメージ（`docker/computer/Dockerfile`）

公式イメージ `ghcr.io/open-webui/computer:latest` は意図的に最小構成で、入っているのは git / openssl / tar / Python 3.12（+ pip, uv）**だけ**。node も npm も curl も gcc も無く、非 root ユーザ `cptr`（uid 1000）で動き `sudo` も無い。**apt パッケージは実行中のコンテナには足せない**ため、起動時インストールではなくビルドになる。

足しているもの: apt（build-essential / curl / wget / jq / ripgrep / git-lfs / openssh-client / unzip / xz-utils / less / vim / procps）、Node 22（node / npm / npx / corepack）、npm -g（pnpm / typescript / tsx）、codex CLI。

**見落とすと沈黙して壊れる点**（すべて実イメージ・実バイナリで確認済み）:

- **`docker compose up -d` だけでは反映されない。** `docker compose build` を明示的に流す必要がある（3 台とも同じ `image:` タグを指すため、これで足りる）
- **Node は 22 で固定。** submodule の `engines` が `>=18.13.0 <=22.x.x` なので、24 に上げると submodule のフロントエンドがインストールできなくなる
- Node は `node:22-bookworm-slim` からバイナリを `COPY` している。**両者のベースが同じ Debian 12 (bookworm) だから成立する**。upstream がベースを変えたらここが壊れる
- pnpm は corepack ではなく `npm install -g` で入れる。corepack のシムは初回実行時にネットワークから pnpm を取りに行くため、「ビルドは通ったのに実行時に落ちる」が起きる
- codex の配布物は `codex-package-*`（単体バイナリではない）。`rg` / `bwrap` / `zsh` を実行ファイルからの相対パスで解決するため、レイアウトを崩すと機能が黙って落ちる。展開直後の中身は uid 1001 所有なので `chown` / `chmod a+rX` が要る
- **イメージに無いパスへ名前付きボリュームを張ると Docker が root 所有でディレクトリを作る。** ベースイメージの `VOLUME` 宣言は `/data` だけで、`/workspace` すら存在しない。`/workspace` と `/home/cptr/{.codex,.cache,.local}` は Dockerfile 側で cptr 所有として作ってある。これを消すと `codex login` も `mkdir /workspace/<name>` も Permission denied になり、Computer のワークスペースピッカーからフォルダも作れなくなる（UI の操作もコンテナ内では cptr として走るため）。ボリュームが空なら再ビルド + `--force-recreate` で所有者が入り直る
- ログインは `codex login --device-auth` を使う。素の `codex login` は 1455 番へのコールバック待ちでコンテナでは完了しない
- モデル ID は codex 側のスラッグで API の ID とは別体系。`agent:codex/gpt-5.4` は有効だが `agent:codex/gpt-5.4-2026-03-05` は存在しない
- **Agents プロファイルの登録と `<workspace>/.cptr/model` は環境変数で設定できない。** 前者は Computer の管理画面、後者はワークスペース内のファイル。ここを書かない限り Open WebUI から Codex は使われない
- **codex のサンドボックス（bwrap）はコンテナ内では動かない。** 非特権ユーザ名前空間を作れず `bwrap: No permissions to create a new namespace` になる。`seccomp=unconfined` / `apparmor=unconfined` / `--privileged` のいずれでも通らないことを実測済みなので、Docker 側を緩める価値は無い。Agents プロファイルの **Sandbox** を `danger-full-access` にする。cptr 自身の `run_command` は元からサンドボックスを使っておらず、境界は最初からコンテナそのものなので実効的な安全性は下がらない
- **その `sandbox_mode` は upstream のバグで無視される。** `utils/agents/models.py` が検証し UI にも出るのに、`utils/agents/codex.py` が読んでいない。`docker/computer/Dockerfile` の「4. cptr のパッチ」で 1 行だけ直している（該当行が変わったらビルドが失敗する）
- **cptr のパッチは 2 件ある。** 上記の `sandbox_mode`（節 4）と、Browser タブの root-relative シム（節 **4b**、`docker/computer/browser-root-relative-shim.js` を `utils/browser/proxy.py` の `rewrite_html` から注入）。どちらもアンカー行の出現回数を 1 で検証しており、upstream が変えたら**ビルドがその場で失敗**する。シムは**冪等でなければならない** — 書き換え不要な入力に対して 1 文字でも違う値を返すと `setAttribute` → `MutationObserver` → `setAttribute` の無限ループでページが描画を終えない（実測でハングした）
- **Open WebUI 側の接続は「API Type: Chat Completions」でなければならない。** gateway が実装しているのは `GET /v1/models` と `POST /v1/chat/completions` の 2 つだけ。`Responses` にすると Open WebUI が `POST /v1/responses` を叩き `Method Not Allowed`（405）になる（分岐は `routers/openai.py:1284`）。接続ごとの設定なので、OpenAI 本体の接続は Responses のままでよい。※PC なので起動済み環境では Admin Settings → Connections で直す

### 機能の実装先は Open WebUI Functions

`docs/DetailedDesign/movie_flow.mmd` が実装の一次仕様。責務分割は以下のとおり:

- **Actions Functions** — フロントに入力フォームを出す層（プロジェクト名選択、動画添付、編集指示、確定ボタン）。ユーザとの往復はすべてここ
- **Pipes Functions** — オーケストレーション層。Descript MCP 呼び出し、Prompts/Models の取得、LLM への編集プロンプト生成、「確定するまで」の編集ループを回す
- **Filters Functions** — 入出力の前後処理

主要ユースケースは 3 つ: 動画のアップロード / 動画の編集（編集 → publish → プレビュー → 再指示のループ）/ タイムラインのエクスポート（Descript App へ遷移して Final Cut Pro 形式で書き出し）。

### Functions のソース管理と取り込み方

Open WebUI の Functions は**実行時は DB 管理**（Workspace UI に登録された内容が動く）でリポジトリ内のファイルではない。
そのため本プロジェクトでは **ソースを `functions/<function-name>.py` として git 管理し、Open WebUI の「Import From Link」に GitHub URL を渡して取り込む**運用を取る。
この取り込みを成立させるため、**本リポジトリは public**（raw.githubusercontent.com をトークンなしで取得するため、private だと 404 になる）。
Open WebUI 本体（submodule）にはコードを置かない。

取り込み処理は `backend/open_webui/routers/functions.py` の `POST /api/v1/functions/load/url`（管理者のみ）。URL は raw URL に自動変換される:

| 指定する GitHub URL              | 実際に取得されるファイル | 登録名 |
| -------------------------------- | ------------------------ | ------ |
| `.../blob/main/functions/foo.py` | そのファイル             | `foo`  |
| `.../tree/main/functions/foo`    | `functions/foo/main.py`  | `foo`  |

`main.py` / `index.py` / `__init__.py` はそれ自体が名前にならず、**親ディレクトリ名**が登録名になる。本プロジェクトは前者のフラット構成（`functions/<name>.py` を `blob` URL で指定）を採る。

- リポジトリ側が正（source of truth）。Workspace UI 上で直接編集して済ませない。UI で急ぎ直した場合は必ずリポジトリへ書き戻す
- 取り込みは**その時点のコードを取得してエディタに載せるだけで、自動同期ではない**。`load/url` は `{name, content}` を返すのみで、保存操作で DB に入る。ソース修正のたびに再インポート＋保存が必要
- URL はブランチを固定する（`refs/heads/<branch>` に変換される）。`main` を指すか、検証中は作業ブランチを指すかを明示的に選ぶ
- **public リポジトリである以上、Functions のコードに API キー・エンドポイント・プロジェクト名などを直書きしない。** 環境依存値は Valves（`Valves` / `UserValves`）に逃がし、値は Open WebUI 側で設定する

**制約 — 1 Function = 自己完結した 1 ファイル**:

- Open WebUI に登録できるのは 1 ファイル分のコードだけ。リポジトリ内の相対 import は解決されない。共通処理は各ファイルにインライン展開するか、`requirements` 経由で pip パッケージ化する
- `replace_imports()` により `from utils` → `from open_webui.utils`、`from apps` / `from main` / `from config` も同様に書き換えられる。Open WebUI 内部を参照するときは最初から `from open_webui.utils...` と書くのが安全
- ファイル**1 行目**が `"""` で始まる必要がある（frontmatter 判定条件）。以降 `key: value` 行を並べて `"""` で閉じる

```python
"""
title: Video Upload Action
author: A-clear
version: 0.1.0
required_open_webui_version: 0.11.0
requirements:
"""
```

`requirements` はカンマ区切り。実際に pip install されるのは `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` が有効かつ非オフライン時のみ。
**本プロジェクトでは 3 ファイルとも `requirements` を空にしている。** 複数ワーカー / 複数レプリカで同時 pip install が走るとワーカーがクラッシュするため本番では無効化する方針であり、Open WebUI 同梱の `mcp` / `httpx` / `pydantic` と標準ライブラリだけで実装している。

Function の種別は定義したクラス名で決まる（`Pipe` / `Filter` / `Action` / `Event`。この優先順で先に見つかった 1 つが採用される）。実行には `ENABLE_PLUGINS` が有効である必要がある。

**制約 — 見落とすと沈黙して壊れる 4 点**（すべて v0.11.0 の実ソースで確認済み）:

1. **`actions` / `file_handler` / `toggle` は `class` 直下のクラス属性にする。**
   `load_function_module_by_id` は `module.Filter()` という**インスタンス**を返す（`utils/plugin.py:296-306`）ため、以降の検出はすべて `getattr(instance, ...)` で行われる。モジュールトップレベルに書くと**エラーも警告も出ないまま無視される**（検出箇所: `utils/models.py:246` / `utils/filter.py:171` / `utils/models.py:409`）。
   なお Open WebUI 公式ドキュメントには `file_handler` を「module attribute, not `self.file_handler`」と書いた箇所があるが、v0.11.0 のコードとは一致しない。

2. **`__init__` で `self.valves = self.Valves()` を必ず初期化する。** これが無いと Valves が注入されない。

3. **`Filter.stream()` の第 1 引数名は `event`**（`body` ではない）。`utils/filter.py:124` が `{'event': form_data}` を渡すため、`body` にすると `TypeError`。
   `outlet()` は `__chat_id__` / `__message_id__` を要求してはいけない（`utils/middleware.py:3505-3512` の extra_params に無い）。

4. **状態は `chat.chat` JSON blob の独自キーに保存する。**
   `Chat.meta` 列に汎用 writer は存在しない（`models/chats.py` で meta を書くのは tags 専用の `:729-742` のみ）。`Chat.variables` は通常チャット完了時に列ごと置換される。
   `Chats.update_chat_by_id(id, blob, touch=False)` を使い、**blob に `title` を必ず含めること**。含めないと `chat_item.title` が `'New Chat'` にリセットされる（`models/chats.py:608`）。

**Action と Pipe の能力差**（責務分割はこれに従っている）:

- Action には `__tools__` / `__metadata__` / `__files__` が**渡らない**（`utils/actions.py:98-104`）。MCP を扱えるのは Pipe だけ
- Action はチャットの**添付ファイルを受け取れない**。フロントが送る `body.messages` に `files` が無い（`src/lib/components/chat/Chat.svelte:2192-2199`）
- Action から Pipe を起動するには `from open_webui.utils.chat import generate_chat_completion` を `stream=False` で呼ぶ。`metadata` 以外の任意キーは Pipe の `body` に素通しされる（`functions.py:206`）ので RPC 引数に使える
- そのとき **`metadata.message_id` には Action の `body['id']` を再利用する**。フロントは `history.messages[event.message_id]` が無いイベントを黙って捨てるため（`Chat.svelte:962`）、新規 UUID を振ると `status`/`embeds` が描画されず `__event_call__` は 300 秒ハングする

### 外部 API 仕様

`docs/RequirementDefinition/DA/` に OpenAPI JSON を配置済み。ネットワークを叩く前にこちらを参照する。

- `descript_api.json` — Descript API。主要エンドポイント: `POST /jobs/import/project_media`（メディア取込）、`POST /jobs/agent`（Agent edit）、`POST /jobs/publish`（書き出し）、`GET /projects` / `GET /projects/{project_id}`、`GET /jobs/{job_id}`（非同期ジョブのポーリング）
- `open_web_ui.json` — Open WebUI 自身の REST API（476 エンドポイント）

### 業務要件（`docs/RequirementDefinition/BA/`）

ショート動画化のために実装対象となる編集機能: ジェットカッティング（サイレンスカット / フィラーカット / タイムライン自動圧縮）、オートキャプショニングと音声処理（自動テロップ、コンプレッサー、ノイズリダクション）、ダイナミックエフェクト（オートパン＆ズーム、インサート配置）、FCPXML 双方向連携。

## 注意点

- **このリポジトリは public**（Functions を GitHub URL で取り込むため）。コミットする内容はすべて公開されると考えること
- `full-stack/open-webui/.env`（OpenAI API キー等を含む）と `backend/.webui_secret_key` はいずれも gitignore 済みのローカル専用ファイル。中身をログ・コミット・出力に露出させない
- `.env.example` は 2 つある。**ルート直下のもの**が本プロジェクトの正（PostgreSQL / Redis / PGVector / MinIO / プロバイダ / Tavily / MCP / Basic RAG（Docling）/ Open Terminal / Computer / Playwright MCP / Codex を含むスケーリング構成。19 節構成）。`full-stack/open-webui/.env.example` は upstream 由来で、submodule 単体を開発起動するとき用
- `WEBUI_SECRET_KEY` は**全レプリカで同一の値**にすること。値が変わると OAuth 連携ツール（Descript MCP など）の保存済みトークンを復号できなくなり `Error decrypting tokens` になる
- submodule 先 `A-clear/open-webui` は別リポジトリ（public）。読み取りは誰でもできるが `kamegin4-aws` に push 権限は無い。submodule にコミットを積むと親が push 不能になる点に注意
- `full-stack/open-webui/backend/venv/` はコミット対象外のローカル環境。作り直す場合は `pip install -r backend/requirements.txt`
- `codex-home-a` / `-b` / `-c` ボリューム（チーム別）には Codex のログイン情報（`auth.json`）が入る。**消すと再ログインが必要**になり、バックアップ対象としても機微。`computer-{a,b,c}-cache` / `computer-{a,b,c}-local` はパッケージキャッシュなので消しても再取得されるだけ
- **マルチユーザ前提で運用している。** 設計は `docs/superpowers/specs/2026-08-27-multi-user-design.md`、手順は `docs/Setup/multi_user_setup.md`。分離は「事故防止」レベルであって「悪意」には不十分。既知の限界は手順書の最終節を参照
- **Functions にアクセス制御は存在しない。** `function` テーブルに `access_control` 列が無く、`access_grant` の `resource_type` 10 種にも `function` が無い。制御軸は `is_active` と `is_global` の 2 つだけ。特定グループに見せたい場合は **Workspace → Models に Model エントリを作り、そこに access_grants を付ける**。「Function の権限」ではなく「Model の権限」として設計すること
- **Pipe から生えるモデルの ID に `pipe.` 接頭辞は付かない。** 単一 Pipe（`pipes` 属性を持たない Function）は **Function の ID がそのままモデル ID**（`functions.py:129-133`）。`descript_pipe.py` → `descript_pipe`。`<function_id>.<sub_id>` になるのは `pipes` を持つマニフォールドだけ（`functions.py:104`）
- **`BYPASS_ADMIN_ACCESS_CONTROL=false` にしてはいけない**（upstream 既定は `true`。`config.py:2062`）。`false` にすると `main.py:1077` の条件が管理者でも真になり、管理者も `check_model_access` を通る。同関数は `model` テーブルに行が無いモデルを問答無用で拒否する（`utils/models.py:447`）ため、**Workspace → Models 未登録のモデルが全員にとって使用不能**になる。しかも `get_filtered_models` は未登録モデルを管理者には見せる（`utils/models.py:521-523`）ので、**モデルピッカーには並ぶのに選ぶと 400 `Model not found`** という食い違いになり原因が分かりにくい。なお Action → Pipe の RPC は `bypass_filter=True` で `chat.py:198` を飛ばすため影響を受けない
- **権限はグループを OR 合成する**（`utils/access_control/__init__.py:54`）。グループは権限を足すことしかできず奪えないため、「既定を `false` に絞ってグループで `true` を足す」以外の順序は成立しない
- **`chat_id` と `file_id` はクライアント制御である。** Functions で DB を引くときは必ずスコープ付き API（`Chats.get_chat_by_id_and_user_id` / `Files.get_file_by_id_and_user_id`）を使う。スコープ無し版を使うと他ユーザの状態とファイルに到達できる
- **PersistentConfig は初回起動時にしか取り込まれない。** `Config.seed_defaults()` は「DB に無いキーだけ INSERT」する（`models/config.py:256`）。`TERMINAL_SERVER_CONNECTIONS` / `OPENAI_API_CONFIGS` / `DEFAULT_USER_ROLE` / `USER_PERMISSIONS_*` / Basic RAG の抽出・分割・検索設定（`CONTENT_EXTRACTION_ENGINE` / `DOCLING_*` / `RAG_TEXT_SPLITTER` / `CHUNK_*` / `RAG_TOP_K`／リランク設定一式（`RAG_TOKENIZER_MODEL` / `ENABLE_RAG_HYBRID_SEARCH` / `RAG_HYBRID_BM25_WEIGHT` / `RAG_RERANKING_ENGINE` / `RAG_RERANKING_MODEL` / `RAG_RERANKING_BATCH_SIZE` / `RAG_TOP_K_RERANKER` / `RAG_RELEVANCE_THRESHOLD`）。`config.py:2828-2894`）はすべてこれに該当する。起動済みの環境で `.env` を変えても効かないので、Admin Settings で直すこと
- **`UVICORN_WORKERS` は 4。ただし Open WebUI のバージョンを上げた直後の初回起動だけは 1 に戻すこと。** マイグレーションは `config.py:78` のモジュール読み込み時に走るため、`--workers 4` では 4 プロセスが同時に `alembic upgrade` を実行する。しかも `config.py:73-75` が例外を握り潰す（`log.exception` のみで再送出しない）ので、**スキーマが壊れても起動は成功してしまう**。DB 接続数も `(POOL_SIZE + MAX_OVERFLOW) × ワーカー数 × レプリカ数` で掛け算になる（既定で 140 / `max_connections=200`）
