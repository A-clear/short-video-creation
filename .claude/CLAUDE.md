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
  Setup/                    セットアップ手順書
functions/                  Open WebUI Functions のソース（ここで開発し、GitHub URL 経由で Open WebUI に取り込む）
  descript_studio.py        Action（マルチ 4 サブ: upload / edit / export / probe）
  descript_pipe.py          Pipe（MCP オーケストレーション。単一モデル）
  descript_guard.py         Filter（RAG 迂回 / 署名 URL 恒久化 / 事前診断）
full-stack/open-webui/      Open WebUI フォーク（submodule）
.env.example                スケーリング構成の環境変数テンプレート
docker-compose.yml          PostgreSQL(pgvector) / Redis / MinIO / Open WebUI
```

**`docs/DetailedDesign/functions_contract.md` が Functions 実装の正本。** 1 Function = 自己完結 1 ファイルの制約上、共通ヘルパは 3 ファイルに重複展開されている。コピー元は常にこの契約書であり、ヘルパを直す場合は契約書を先に直してから 3 ファイルへ反映する。

ドキュメント・コミットメッセージ・ブランチ名は日本語。コミットは `add:` / `mod:` / `feature:` の接頭辞を使う。

## submodule の扱い

```bash
git submodule update --init --recursive     # 初回チェックアウト時に必須
git submodule status                        # 現在ピン留めされているコミットの確認
```

`full-stack/open-webui` 配下を変更した場合は、**submodule 内でコミット → 親リポジトリで submodule ポインタをコミット**の 2 段階が必要。

フォーク独自の差分は現時点で 2 コミットのみ（upstream との乖離を最小に保つ方針）:

- `.open-webui/agents.txt`, `llms.txt`, `llms-full.txt` — Open WebUI 公式ドキュメントのエージェント向けインデックス。Open WebUI の仕様を調べるときはまずここ（`https://docs.openwebui.com/api/search?q=...`、任意ページに `.md` を付けると Markdown 取得）を参照する
- `backend/dev.sh` — 開発起動時に `WEBUI_SECRET_KEY` を自動生成（v0.11.0 以降は必須。未設定だと backend が起動しない）

Open WebUI 本体（`backend/open_webui/`, `src/`）への改変は原則行わない。upstream 追従が壊れるため、機能追加は後述の Functions として実装する。

## 開発コマンド

すべて `full-stack/open-webui/` 配下で実行する。フロントエンド（SvelteKit :5173）とバックエンド（FastAPI :8080）を別プロセスで起動する構成。

```bash
# バックエンド（Python 3.11 / venv は backend/venv に既存）
cd full-stack/open-webui/backend
source venv/bin/activate
./dev.sh                      # uvicorn --reload、:8080、CORS を :5173 に許可、シークレット鍵を自動生成

# フロントエンド
cd full-stack/open-webui
npm run dev                   # pyodide 取得 → vite dev --host（:5173）

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
- ホスティング想定: Vercel（`docs/RequirementDefinition/TA/system_architecture.mmd`）

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
- `.env.example` は 2 つある。**ルート直下のもの**が本プロジェクトの正（PostgreSQL / Redis / PGVector / MinIO / プロバイダ / Tavily / MCP を含むスケーリング構成）。`full-stack/open-webui/.env.example` は upstream 由来で、submodule 単体を開発起動するとき用
- `WEBUI_SECRET_KEY` は**全レプリカで同一の値**にすること。値が変わると OAuth 連携ツール（Descript MCP など）の保存済みトークンを復号できなくなり `Error decrypting tokens` になる
- submodule 先 `A-clear/open-webui` は別リポジトリ。親を public にしても連動しないため、private のままだと第三者は `git submodule update` に失敗する
- `full-stack/open-webui/backend/venv/` はコミット対象外のローカル環境。作り直す場合は `pip install -r backend/requirements.txt`
