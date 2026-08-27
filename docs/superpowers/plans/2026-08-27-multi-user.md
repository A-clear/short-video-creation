# マルチユーザ対応 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open WebUI / Open Terminal / Open WebUI Computer / Descript Functions を、少人数の社内チーム（〜10 名）が同時利用できる状態にする。Enterprise License は使用しない。

**Architecture:** 3 層に分けて解く。①認証・認可は Open WebUI の Groups と access_grants、②実行環境は Open Terminal の組み込みマルチユーザ（ファイル分離）と Computer のチーム別コンテナ（3 台）、③アプリは Descript Functions の `user_id` スコープ化。既定を閉じてグループで開く順序を守る。

**Tech Stack:** Docker Compose / Open WebUI v0.11.0 (FastAPI) / Open Terminal / Open WebUI Computer (cptr) / Python 3.11

**Spec:** `docs/superpowers/specs/2026-08-27-multi-user-design.md`

## Global Constraints

このリポジトリのルール（`.claude/rules/`）が本計画のすべてのタスクに適用される。**スキルの既定手順よりリポジトリのルールが優先する。**

- **git のミューテート系コマンドを実行しない。** `git commit` / `git push` / `git merge` / `git rebase` / `git reset` / `git checkout -b` は使用禁止。参照系（`git status` / `git log` / `git diff` / `git show`）のみ可。**そのため本計画に「コミット」ステップは存在しない。** 各タスクの最後は検証ステップで終わる
- **テストコードを作成しない。実行しない。** 単体テスト・結合テストは明確な指示がない限り書かない。検証は既存の静的検査 `scripts/check_functions.py` と `docker compose config -q` で行う
- **ビルド・デプロイコマンドを実行しない。** `docker compose build` / `docker compose up` / `pnpm build` は実行せず、手順書に記載して利用者に委ねる
- **`npm` ではなく `pnpm` を使う**（本計画では該当なし）
- **`python` / `pip` の実行前に venv を有効化する。** リポジトリ直下に `.venv` がある: `source .venv/bin/activate`
- **このリポジトリは public。** API キー・エンドポイント・プロジェクト名を平文でコミットしない
- ドキュメント・コメントは日本語
- **Functions の共通ヘルパはバイト単位で同一でなければならない。** `scripts/check_functions.py` の `SHARED` / `SHARED_CONSTS` に載っているものが 2 ファイル以上に存在する場合、`norm()` 後の文字列が完全一致することを検査する（`scripts/check_functions.py:318-334`）。正本は `docs/DetailedDesign/functions_contract.md` §5

### 確定している値

- チーム名: `team-a` / `team-b` / `team-c`（後で差し替える雛形）
- 新規ユーザ: `DEFAULT_USER_ROLE=pending`（管理者承認を挟む）
- Computer の開発サーバポート公開: 全廃
- Computer のリソース: `memory` limits `3G` / `cpus` `2.0` / `reservations.memory` `1G`
- Computer のホストポート: 8001 / 8002 / 8003
- Open Terminal: 1 コンテナ、`OPEN_TERMINAL_MULTI_USER=true`、`OPEN_TERMINAL_MAX_SESSIONS=32`
- Open Terminal のボリューム: `open-terminal-home:/home`（`open-terminal-data:/home/user` から変更）

---

## File Structure

| ファイル | 責務 | Task |
|---|---|---|
| `.env.example` | 環境変数の正本。§10 に入口の設定、新設 §17 にマルチユーザの権限・監査、§14 に Terminal、§15/16 に Computer 3 台分 | 1, 2, 3 |
| `docker-compose.yml` | サービス定義。Terminal のマルチユーザ化、Computer の 3 台化、ネットワーク分割、Open WebUI の接続 4 本化 | 2, 3 |
| `docs/DetailedDesign/functions_contract.md` | 共通ヘルパの正本。§5.3 を改訂し、`_user_id` を §5.3 に追加 | 4, 5 |
| `scripts/check_functions.py` | 静的検査。`SHARED` に `_user_id` を追加 | 4 |
| `functions/descript_pipe.py` | Pipe。`_user_id` / `_load_state` / `_save_state` / `_append_history` / `_resolve_stored_file` と全呼び出し元 | 4, 5 |
| `functions/descript_studio.py` | Action。`_user_id` / `_load_state` と呼び出し元 1 箇所 | 4 |
| `functions/descript_guard.py` | Filter。`_user_id` / `_load_state` / `_save_state` / `_append_history` と呼び出し元 3 箇所 | 4 |
| `docs/Setup/multi_user_setup.md` | 新規。セットアップの順序と既知の限界 | 6 |
| `.claude/CLAUDE.md` | マルチユーザ前提の追記 | 7 |

---

## Task 1: Open WebUI の認証・権限・監査

**Files:**
- Modify: `.env.example`（§10 認証、および新設 §17）

**Interfaces:**
- Consumes: なし（最初のタスク）
- Produces: `.env.example` に新設される §17「マルチユーザ（権限・監査）」。Task 6 の手順書がこの節を参照する

**背景:** Open WebUI の権限はグループを OR 合成する実装で（`utils/access_control/__init__.py:54`「最も寛容な値が勝つ」）、グループは権限を足すことしかできず奪えない。したがって既定を閉じてからグループで開く順序でしか設計できない。

- [ ] **Step 1: §10「認証」に入口の設定を追加する**

`.env.example` の `ENABLE_OAUTH_SIGNUP=true` の直前に次を挿入する。

```
# 社内ドメイン以外のアカウントを入口で弾く。カンマ区切り。既定 * は全許可。
# （config.py:2575）
OAUTH_ALLOWED_DOMAINS=example.com

# 新規ユーザのロール。pending にすると管理者が承認するまでログインできない。
# ⚠️ PersistentConfig。初回起動時にしか取り込まれない（config.py:1699 →
#    ui.default_user_role）。起動済みの環境では Admin Settings → Users で変更する。
#
# ⚠️ Google OAuth 単体ではロール / グループの自動同期ができない。
#    GOOGLE_OAUTH_SCOPE の既定が openid email profile（config.py:2465）で
#    roles / groups クレームが返らず、ENABLE_OAUTH_ROLE_MANAGEMENT の実装
#    （utils/oauth.py:1488）は 3 つの条件が揃わないとロール判定に入らないため。
#    グループは Admin Settings → Users → Groups で手動運用する。
DEFAULT_USER_ROLE=pending
```

- [ ] **Step 2: 既存の §17 を §18 に繰り下げる**

`.env.example:585` の見出しを次のように書き換える。

```
# 18. docker-compose 用（Open WebUI 本体は参照しません）
```

見出しの直前直後の区切り線（`# ---...---`）はそのままにする。

- [ ] **Step 3: 新しい §17「マルチユーザ（権限・監査）」を挿入する**

`.env.example` の §16 の末尾（新しい §18 の区切り線の直前）に次のブロックを挿入する。

```
# ------------------------------------------------------------------------------
# 17. マルチユーザ（権限・監査）
# ------------------------------------------------------------------------------
#
# ⚠️ この節の USER_PERMISSIONS_* はすべて PersistentConfig です。
#    DEFAULT_CONFIG 経由で Config.seed_defaults() に渡されますが、この関数は
#    「DB に無いキーだけ INSERT」します（models/config.py:256）。
#    つまり取り込まれるのは初回起動時だけで、2 回目以降は
#    Admin Settings → Users → Groups の値が正になります。
#
# ⚠️ 権限はグループを OR で合成します（utils/access_control/__init__.py:54）。
#    グループは権限を「足す」ことしかできず、奪えません。
#    したがって「既定を false に絞り、許可グループに true を足す」以外の
#    順序は成立しません。ここを逆にすると後から締められなくなります。

# --- 共有の禁止 -----------------------------------------------------------
# 一般ユーザがワークスペースのリソースやチャットを他人・公開に開く経路を塞ぐ。
# （config.py:1765-1818）
USER_PERMISSIONS_WORKSPACE_MODELS_ALLOW_SHARING=false
USER_PERMISSIONS_WORKSPACE_MODELS_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ALLOW_SHARING=false
USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_WORKSPACE_PROMPTS_ALLOW_SHARING=false
USER_PERMISSIONS_WORKSPACE_PROMPTS_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_WORKSPACE_TOOLS_ALLOW_SHARING=false
USER_PERMISSIONS_WORKSPACE_TOOLS_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_WORKSPACE_SKILLS_ALLOW_SHARING=false
USER_PERMISSIONS_WORKSPACE_SKILLS_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_NOTES_ALLOW_SHARING=false
USER_PERMISSIONS_NOTES_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_FOLDERS_ALLOW_SHARING=false
USER_PERMISSIONS_CHAT_ALLOW_PUBLIC_SHARING=false
USER_PERMISSIONS_CHAT_ALLOW_OPEN_SHARING=false
USER_PERMISSIONS_CALENDAR_ALLOW_PUBLIC_SHARING=false

# --- grant 操作の禁止 -----------------------------------------------------
# filter_allowed_access_grants()（utils/access_control/__init__.py:220-298）は
# 権限の無いユーザが付けた grant をエラーにせず保存時に削ります。
# 一般ユーザが自分のリソースを他人へ開く経路が閉じます。
USER_PERMISSIONS_ACCESS_GRANTS_ALLOW_USERS=false
USER_PERMISSIONS_ACCESS_GRANTS_ALLOW_GROUPS=false

# --- 迂回路の遮断（既定 false。明示して維持する）--------------------------
#
# ⚠️ direct_tool_servers は特に重要です。ユーザ個人設定からの direct 接続には
#    access_grants が付かず（AddTerminalServerModal.svelte:359 が !direct の
#    ときだけ付与）、ブラウザが Open Terminal を直叩きするため X-User-Id が
#    付きません。その結果 uid 1000（パスワード無しで root 昇格可）になります。
#    本プロジェクトは Open Terminal のポートを公開していないため実際には
#    到達できませんが、この 2 つは併せて維持してください。
USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS=false
USER_PERMISSIONS_FEATURES_API_KEYS=false
USER_PERMISSIONS_FEATURES_AUTOMATIONS=false

# --- 管理者の越境 ---------------------------------------------------------
# ⚠️ これらは製品 UI 経路を塞ぐだけです。DB やインフラへの直接アクセスは別問題。
ENABLE_ADMIN_CHAT_ACCESS=false
ENABLE_ADMIN_EXPORT=false
BYPASS_ADMIN_ACCESS_CONTROL=false
BYPASS_MODEL_ACCESS_CONTROL=false

# --- 監査 -----------------------------------------------------------------
# ⚠️ AUDIT_LOG_LEVEL の既定は NONE です（env.py:1160）。
#    明示的に上げない限り何も記録されません。
AUDIT_LOG_LEVEL=REQUEST
ENABLE_AUDIT_LOGS_FILE=true

# --- セッション -----------------------------------------------------------
# HTTPS 運用では true にしてください（env.py:726-732、既定 false）。
WEBUI_AUTH_COOKIE_SECURE=true
WEBUI_AUTH_COOKIE_SAME_SITE=lax
```

- [ ] **Step 4: 検証**

節番号の重複と欠落が無いことを確認する。

Run:
```bash
grep -n '^# [0-9]\+\.' .env.example
```
Expected: 1 から 18 までが 1 つずつ、重複なしで並ぶ。特に `17. マルチユーザ（権限・監査）` と `18. docker-compose 用` が順に出ること。

Run:
```bash
grep -c '^USER_PERMISSIONS_' .env.example
```
Expected: `21`

---

## Task 2: Open Terminal のマルチユーザ化

**Files:**
- Modify: `docker-compose.yml`（`open-terminal` サービス、`open-webui` の環境変数、`volumes:` 宣言）
- Modify: `.env.example`（§14）

**Interfaces:**
- Consumes: なし
- Produces: `open-terminal-home` ボリューム名。Task 6 の手順書が既存ボリュームの削除手順で参照する

**背景:** `OPEN_TERMINAL_MULTI_USER=true` は `useradd` で実 Linux アカウントを作り、`sudo -u` でコマンドを回す。Open WebUI は `X-User-Id` を無条件に送る（`routers/terminals.py:123`）ため追加の連携は不要。分離されるのはファイルのみで、PTY セッション・プロセス出力・ポートは全ユーザ共有のまま。

- [ ] **Step 1: open-terminal の環境変数を追加する**

`docker-compose.yml` の `open-terminal` サービスの `environment:` に次を追加する。`OPEN_TERMINAL_MAX_SESSIONS` は既存行を書き換える。

```yaml
      # 各ユーザに専用の Linux アカウントと home（/home/<先頭8文字>）を作る。
      # Open WebUI は X-User-Id を無条件に送るため（routers/terminals.py:123）、
      # 追加の連携設定は要らない。
      #
      # ⚠️ 分離されるのはファイルだけです。PTY セッション一覧・接続・削除、
      #    プロセス一覧・コマンド文字列・出力、ポート、CPU/メモリは全ユーザ共有。
      #    他人の稼働中シェルに WebSocket でアタッチできます。
      #    小規模で信頼できるチーム向けの設定です。
      #
      # ⚠️ home 名は Open WebUI のユーザ ID を英数小文字の先頭 8 文字に
      #    切り詰めたものです（公式ドキュメントの /home/{user-id} は不正確）。
      #    先頭 8 文字が一致する 2 人は同じ OS アカウントに合流します。
      OPEN_TERMINAL_MULTI_USER: "true"

      # ⚠️ コンテナ全体の合計上限であり、ユーザ単位ではありません。
      #    既定 16 を人数で分け合う形になるため引き上げます。
      OPEN_TERMINAL_MAX_SESSIONS: ${OPEN_TERMINAL_MAX_SESSIONS:-32}
```

- [ ] **Step 2: ボリュームのマウント先と名前を変える**

`docker-compose.yml` の `open-terminal` の `volumes:` を次に置き換える。

```yaml
    volumes:
      # ⚠️ マルチユーザ時は各ユーザの home が /home/<name> に作られるため、
      #    /home ごと載せる必要があります。/home/user だけをマウントしても
      #    新しい home はコンテナのレイヤ上に置かれ、作り直すと消えます。
      #
      # ⚠️ 既存の open-terminal-data を「マウント先だけ /home に変える」のは
      #    壊れます。既存ボリュームは空ではない（/etc/skel 由来の dotfile が
      #    24KB）ため、Docker がイメージの /home で seed せずボリュームの中身で
      #    上書きし、/home/user が root 所有の空ディレクトリになります。
      #    uid 1000 の user は自分の HOME に書けずログ出力が失敗します。
      #    そのためボリューム名ごと変えています（移行価値のあるデータは無し）。
      - open-terminal-home:/home
```

- [ ] **Step 3: `volumes:` 宣言を書き換える**

`docker-compose.yml` の末尾の `volumes:` ブロックで `open-terminal-data:` を次に置き換える。

```yaml
  open-terminal-home:
```

- [ ] **Step 4: FILE_BROWSER_ROOT のコメントを直す**

`OPEN_TERMINAL_FILE_BROWSER_ROOT: home` の上のコメント「home = /home/user（既定）」はマルチユーザ化後は不正確になる。該当コメントを次に置き換える。

```yaml
      # ファイルブラウザのルート。home を指定すると、マルチユーザ時は
      # 各ユーザ自身の home に解決される（main.py:153 の get_file_browser_root
      # が fs.home を返す）。明示パス（/workspace 等）や filesystem も
      # 指定できるが、filesystem にすると分離の意味が消えるため使わない。
```

- [ ] **Step 5: TERMINAL_SERVER_CONNECTIONS の access_grants を管理者のみにする**

`docker-compose.yml` の `open-webui` サービスの `TERMINAL_SERVER_CONNECTIONS` を次に置き換える。

```yaml
      # ⚠️ access_grants を空にしています。空 / 未指定は「管理者のみ」に倒れます
      #    （utils/access_control/__init__.py:171）。これは意図的な初期値です。
      #
      #    グループ ID はグループを作るまで存在せず、この変数は初回起動時にしか
      #    取り込まれない（Config.seed_defaults() は DB に無いキーだけ INSERT。
      #    models/config.py:256）ため、ここにグループ grant を書けません。
      #    起動後に Admin Settings → Integrations で 3 チームの read grant を
      #    付けてください。手順は docs/Setup/multi_user_setup.md。
      #
      # ⚠️ config.py:386 の json.loads には try/except がありません。
      #    JSON を壊すと Open WebUI が起動しません。
      TERMINAL_SERVER_CONNECTIONS: >-
        [{"id": "open-terminal", "name": "作業用ターミナル",
        "url": "http://open-terminal:8000", "key": "${OPEN_TERMINAL_API_KEY}",
        "auth_type": "bearer", "enabled": true,
        "config": {"access_grants": []}}]
```

- [ ] **Step 6: TERMINAL_PROXY_HEADERS を追加する**

`docker-compose.yml` の `open-webui` サービスの `environment:` で、`TERMINAL_SERVER_CONNECTIONS` の直後に次を追加する。

```yaml
      # ターミナルのプロキシ応答に sandbox CSP を被せる。
      # プロキシは上流応答からセキュリティ関連ヘッダを剥がすが、ここで指定した
      # ものは剥がした後にマージされ、衝突時はこちらが勝つ
      # （routers/terminals.py:178-179）。
      #
      # ⚠️ セッション横取りには効きません。あれは WebSocket であって
      #    プロキシ応答ではないためです。効くのは「プロキシ経由で返る
      #    コンテンツがブラウザで何をできるか」の制限だけです。
      #
      # TERMINAL_SERVER_CONNECTIONS と違い config.py:391-393 に try/except が
      # あるため、JSON が壊れていても {} にフォールバックして起動します。
      TERMINAL_PROXY_HEADERS: >-
        {"Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY"}
```

- [ ] **Step 7: .env.example の §14 を更新する**

`.env.example` の §14「Open Terminal」で、`OPEN_TERMINAL_MAX_SESSIONS` の既定値を 32 に変え、次の注記を追加する。既存の `OPEN_TERMINAL_MAX_SESSIONS` 行がある場合は書き換える。

```
# 同時ターミナルセッション数の上限。
# ⚠️ コンテナ全体の合計上限であり、ユーザ単位ではありません（main.py:1899 が
#    len(_terminal_sessions) をグローバルに数える）。人数分引き上げてください。
OPEN_TERMINAL_MAX_SESSIONS=32

# ⚠️ マルチユーザモードは docker-compose.yml 側で "true" に固定しています。
#    ユーザごとに Linux アカウントと home が作られ、ファイルが分離されます。
#    分離されないもの（セッション・プロセス出力・ポート・リソース）については
#    docs/Setup/multi_user_setup.md の「既知の限界」を参照してください。
```

- [ ] **Step 8: 検証**

Run:
```bash
docker compose config -q && echo "compose OK"
```
Expected: `compose OK`（YAML と変数展開が壊れていない）

Run:
```bash
grep -nE "^\s*-?\s*open-terminal-data:" docker-compose.yml || echo "旧ボリューム名の残存なし"
```
Expected: `旧ボリューム名の残存なし`

（Step 2 が逐語指定するコメント文の中に「既存の open-terminal-data を…」という説明が
含まれるため、素の `grep -n "open-terminal-data"` は必ずヒットする。ボリューム宣言と
マウント指定だけを見るために行頭アンカーを付けている。）

Run:
```bash
source .venv/bin/activate && python3 -c "
import json, subprocess
out = subprocess.run(['docker','compose','config','--format','json'],capture_output=True,text=True).stdout
cfg = json.loads(out)
env = cfg['services']['open-webui']['environment']
json.loads(env['TERMINAL_SERVER_CONNECTIONS'])
json.loads(env['TERMINAL_PROXY_HEADERS'])
print('JSON OK')
"
```
Expected: `JSON OK`（両方の環境変数が有効な JSON として展開されている）

---

## Task 3: Computer のチーム別 3 コンテナ化

**Files:**
- Modify: `docker-compose.yml`（`open-webui-computer` サービスを 3 つに分割、`volumes:`、`networks:`、`open-webui` の `networks:` と `OPENAI_API_*`）
- Modify: `.env.example`（§15、§16）

**Interfaces:**
- Consumes: Task 2 で変更済みの `docker-compose.yml`
- Produces: サービス名 `open-webui-computer-a` / `-b` / `-c`、環境変数名 `OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A` / `_B` / `_C`、`OPEN_WEBUI_COMPUTER_PORT_A` / `_B` / `_C`。Task 6 の手順書がこれらを参照する

**背景:** Computer の `routers/workspace.py` と `routers/terminal.py` は絶対パスを受け取るだけで所有者検査が無く、設定でも `config.toml` でも緩和できない。コンテナ境界が唯一の分離手段。一方 Gateway API はキー所有者のワークスペースだけをモデルとして返す（`gateway.py:104-105` が `Workspace.get_by_user(user_id)`）ため、接続を 3 本に分ければモデルピッカーの出し分けが成立する。

- [ ] **Step 1: YAML アンカーで共通部分を括る**

`docker-compose.yml` の `services:` の直前に次を挿入する。

```yaml
# ==============================================================================
# Open WebUI Computer の共通定義（チーム別 3 台で共有する）
# ==============================================================================
#
# ⚠️ 3 台とも同じ image: タグを指すため、docker compose build は実質 1 回の
#    ビルドになります（BuildKit のキャッシュが効く）。
x-computer-env: &computer-env
  # イメージ側で既に /data ですが、下の volume と対応させるため明示します。
  CPTR_DATA_DIR: /data
  CPTR_LOG_LEVEL: ${OPEN_WEBUI_COMPUTER_LOG_LEVEL:-INFO}
  CPTR_LOG_FORMAT: json
  # 既定は NONE（監査ログ無し）。SSH 相当の権限を持つ面なので METADATA 以上を推奨。
  # ⚠️ API レベルの記録であり、端末操作の全トランスクリプトではありません。
  CPTR_AUDIT_LOG_LEVEL: ${OPEN_WEBUI_COMPUTER_AUDIT_LOG_LEVEL:-METADATA}
  CPTR_EXECUTE_TIMEOUT: ${OPEN_WEBUI_COMPUTER_EXECUTE_TIMEOUT:-600}
  TAVILY_API_KEY: ${TAVILY_API_KEY:-}
  #
  # ⚠️ LLM プロバイダ（OpenAI / Anthropic）と認証モードは環境変数で
  #    設定できません。前者は各 Computer の Settings → Admin → Connections、
  #    後者は <data-dir>/config.toml の [auth] セクションです。
  #
  # ⚠️ 認証モードは password のままにしてください。trusted_header モードの
  #    AuthResult は user_id が None になり（utils/config.py:387-397）、
  #    gateway キー発行が 401 で弾かれます（gateway.py:780-781）。
  #    本構成は gateway キーが必須です。

x-computer-base: &computer-base
  build:
    context: ./docker/computer
    args:
      COMPUTER_IMAGE: ${OPEN_WEBUI_COMPUTER_IMAGE:-ghcr.io/open-webui/computer:latest}
      NODE_VERSION: ${NODE_VERSION:-22}
      PNPM_VERSION: ${PNPM_VERSION:-11.24.0}
      TYPESCRIPT_VERSION: ${TYPESCRIPT_VERSION:-7.0.2}
      TSX_VERSION: ${TSX_VERSION:-4.23.12}
      CODEX_CLI_VERSION: ${CODEX_CLI_VERSION:-rust-v0.149.1}
      CODEX_CLI_TARGET: ${CODEX_CLI_TARGET:-x86_64-unknown-linux-musl}
  image: svc/open-webui-computer:local
  restart: unless-stopped
  deploy:
    resources:
      limits:
        # ⚠️ 3 台に増えたため 4G/4.0 から下げています。limits はピーク保護で
        #    あって常時消費ではありませんが、3 台合計 9G / 6 CPU がホストに
        #    要求されます。
        memory: ${OPEN_WEBUI_COMPUTER_MEMORY_LIMIT:-3G}
        cpus: "${OPEN_WEBUI_COMPUTER_CPU_LIMIT:-2.0}"
      reservations:
        memory: ${OPEN_WEBUI_COMPUTER_MEMORY_RESERVATION:-1G}
  healthcheck:
    # curl の同梱は保証されていないため、確実に存在する python で叩きます。
    test:
      - CMD
      - python
      - -c
      - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).status == 200 else 1)"
    interval: 30s
    timeout: 10s
    retries: 5
    start_period: 60s
```

- [ ] **Step 2: 既存の open-webui-computer サービスを 3 つに置き換える**

`docker-compose.yml` の `open-webui-computer:` サービス定義全体（`build:` から `networks: [svc-computer-net]` まで）を次に置き換える。既存の解説コメント（Open Terminal との役割の違い、セキュリティモデル）は先頭の 1 ブロックに集約する。

```yaml
  # ----------------------------------------------------------------------------
  # Open WebUI Computer（cptr）— チーム別 3 コンテナ
  # ----------------------------------------------------------------------------
  # 「実マシンをブラウザに出す」コンポーネント。ファイル・エディタ・ターミナル・
  # git に加えて、独自のエージェントランタイムを持ちます。
  #
  # ⚠️ Open Terminal とは別物です:
  #    - Open Terminal … Open WebUI の中の「手」。バックエンドがプロキシする部品。
  #    - Computer      … 独立した 1 つのアプリ。自前のログインと PWA を持つ。
  #                      Open WebUI からは gateway 経由で cptr/<workspace> という
  #                      モデルとして見える。
  #
  # ⚠️ なぜチームごとにコンテナを分けるのか:
  #    cptr の routers/workspace.py と routers/terminal.py は絶対パスや
  #    セッション ID を受け取るだけで所有者検査がありません。サインインさえ
  #    していれば任意のユーザが他人のファイルを読み書きでき、他人の稼働中
  #    シェルにアタッチできます（GET /api/workspace/read?path=... が通る）。
  #    これは設定でも config.toml でも緩和できないため、コンテナ境界が
  #    唯一の分離手段です。公式の表現は "Accounts are not isolation."
  #
  # ⚠️ 同一チーム内には分離がありません。境界はチームのコンテナだけです。
  #
  # ⚠️ ポート公開は必須です。ブラウザが Open WebUI のバックエンドを経由せず
  #    直接 Socket.IO でつなぎに行くためです。生の公開インターネットには
  #    出さないでください。
  open-webui-computer-a:
    <<: *computer-base
    container_name: svc-open-webui-computer-a
    environment:
      <<: *computer-env
      CPTR_CORS_ALLOWED_ORIGINS: ${OPEN_WEBUI_COMPUTER_URL_A:-http://localhost:8001},${WEBUI_URL:-http://localhost:8080}
    volumes:
      # ⚠️ ホストディレクトリを /data に bind mount すると SQLite が app.db を
      #    作れずに起動失敗します。名前付きボリュームのままにしてください。
      - computer-a-data:/data
      - computer-a-workspace:/workspace
      # ⚠️ Codex のログイン情報。チームごとに分けているため、同じ ChatGPT
      #    アカウントで 3 回 codex login --device-auth する運用になります
      #    （レート制限は 1 アカウントで共有）。消すと再ログインが必要です。
      - codex-home-a:/home/cptr/.codex
      - computer-a-cache:/home/cptr/.cache
      - computer-a-local:/home/cptr/.local
    ports:
      - "${OPEN_WEBUI_COMPUTER_PORT_A:-8001}:8000"
    networks: [svc-computer-net-a]

  open-webui-computer-b:
    <<: *computer-base
    container_name: svc-open-webui-computer-b
    environment:
      <<: *computer-env
      CPTR_CORS_ALLOWED_ORIGINS: ${OPEN_WEBUI_COMPUTER_URL_B:-http://localhost:8002},${WEBUI_URL:-http://localhost:8080}
    volumes:
      - computer-b-data:/data
      - computer-b-workspace:/workspace
      - codex-home-b:/home/cptr/.codex
      - computer-b-cache:/home/cptr/.cache
      - computer-b-local:/home/cptr/.local
    ports:
      - "${OPEN_WEBUI_COMPUTER_PORT_B:-8002}:8000"
    networks: [svc-computer-net-b]

  open-webui-computer-c:
    <<: *computer-base
    container_name: svc-open-webui-computer-c
    environment:
      <<: *computer-env
      CPTR_CORS_ALLOWED_ORIGINS: ${OPEN_WEBUI_COMPUTER_URL_C:-http://localhost:8003},${WEBUI_URL:-http://localhost:8080}
    volumes:
      - computer-c-data:/data
      - computer-c-workspace:/workspace
      - codex-home-c:/home/cptr/.codex
      - computer-c-cache:/home/cptr/.cache
      - computer-c-local:/home/cptr/.local
    ports:
      - "${OPEN_WEBUI_COMPUTER_PORT_C:-8003}:8000"
    networks: [svc-computer-net-c]
```

開発サーバのポート公開（`COMPUTER_DEV_PORT_VITE` / `_NODE` / `_HTTP` の 3 行と、それに付随する長大なコメントブロック）は**削除する**。Browser タブ / Port Preview が任意ポートを扱えるため不要であり、複数人だとポート衝突で機能しない。

- [ ] **Step 3: open-webui の depends_on を書き換える**

`docker-compose.yml` の `open-webui` の `depends_on:` で `open-webui-computer:` を次の 3 つに置き換える。

```yaml
      open-webui-computer-a:
        condition: service_started
      open-webui-computer-b:
        condition: service_started
      open-webui-computer-c:
        condition: service_started
```

- [ ] **Step 4: open-webui の OpenAI 接続を 4 本にする**

`docker-compose.yml` の `open-webui` の `OPENAI_API_BASE_URLS` / `OPENAI_API_KEYS` / `OPENAI_API_CONFIGS` を次に置き換える。

```yaml
      # 添字 0 = 本物の OpenAI、1〜3 = 各チームの Computer gateway。
      # OPENAI_API_CONFIGS のキーは「この配列の添字を文字列にしたもの」です
      #（routers/openai.py:544 の api_configs.get(str(idx), ...)）。
      #
      # ⚠️ 接続そのものは管理者グローバルで、ユーザ別に出し分ける口はありません。
      #    出し分けは Workspace → Models 側のグループ ACL で行います。
      #    未登録のモデルは管理者にしか見えない（utils/models.py:517-523）ため、
      #    Model エントリを作るまで一般ユーザからは不可視です。
      #
      # ⚠️ ※PersistentConfig です。既に一度起動した環境ではここを変えても
      #    無視されるので、Admin Settings → Connections で直してください。
      OPENAI_API_BASE_URLS: ${OPENAI_API_BASE_URL:-https://api.openai.com/v1};http://open-webui-computer-a:8000/v1;http://open-webui-computer-b:8000/v1;http://open-webui-computer-c:8000/v1
      OPENAI_API_KEYS: ${OPENAI_API_KEY};${OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A:-};${OPEN_WEBUI_COMPUTER_GATEWAY_KEY_B:-};${OPEN_WEBUI_COMPUTER_GATEWAY_KEY_C:-}
      #
      # headers は接続ごとのカスタムヘッダで、値は utils/headers.py:114-131 の
      # プレースホルダに置換されます。この 5 つを渡すことで
      #   - 会話の継続 / 分岐の同期 / 裏方タスクの除外
      # が効きます。1 つでも欠けると素のチャットしか成立しません。
      #
      # ⚠️ api_type は Chat Completions（= "responses" 以外）にすること。
      #    gateway が実装しているのは GET /v1/models と
      #    POST /v1/chat/completions の 2 つだけです。
      #
      # ⚠️ gateway 経由の実行に個別承認はありません。gateway キーは SSH 認証情報
      #    として扱ってください。
      OPENAI_API_CONFIGS: >-
        {"0": {"enable": true},
        "1": {"enable": ${OPEN_WEBUI_COMPUTER_ENABLED_A:-false},
        "connection_type": "external", "api_type": "",
        "headers": {"X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
        "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
        "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
        "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
        "X-OpenWebUI-Task": "{{TASK}}"}},
        "2": {"enable": ${OPEN_WEBUI_COMPUTER_ENABLED_B:-false},
        "connection_type": "external", "api_type": "",
        "headers": {"X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
        "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
        "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
        "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
        "X-OpenWebUI-Task": "{{TASK}}"}},
        "3": {"enable": ${OPEN_WEBUI_COMPUTER_ENABLED_C:-false},
        "connection_type": "external", "api_type": "",
        "headers": {"X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
        "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
        "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
        "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
        "X-OpenWebUI-Task": "{{TASK}}"}}}
```

- [ ] **Step 5: open-webui の networks を 3 本に広げる**

`docker-compose.yml` の `open-webui` の `networks:` を次に置き換える。

```yaml
    # svc-terminal-net と 3 つの svc-computer-net-* にも参加します。
    # 各 Computer へ到達できるのはこのサービスだけで、Computer 同士は
    # ネットワークが分かれているため相互に到達できません。
    networks:
      [
        svc-net,
        svc-terminal-net,
        svc-computer-net-a,
        svc-computer-net-b,
        svc-computer-net-c,
      ]
```

- [ ] **Step 6: volumes 宣言を書き換える**

`docker-compose.yml` 末尾の `volumes:` ブロックで、`open-webui-computer-data` / `open-webui-computer-workspace` / `codex-home` / `open-webui-computer-cache` / `open-webui-computer-local` の 5 行を次に置き換える。

```yaml
  # Computer のチーム別ボリューム。
  # ⚠️ codex-home-* には Codex のログイン情報（auth.json）が入ります。
  #    消すと再ログインが必要になり、バックアップ対象としても機微です。
  # ⚠️ *-cache / *-local はパッケージキャッシュなので消しても再取得されるだけ。
  #    ただしチームごとに独立するため、合計サイズは 3 倍に膨らみます。
  computer-a-data:
  computer-a-workspace:
  computer-a-cache:
  computer-a-local:
  codex-home-a:
  computer-b-data:
  computer-b-workspace:
  computer-b-cache:
  computer-b-local:
  codex-home-b:
  computer-c-data:
  computer-c-workspace:
  computer-c-cache:
  computer-c-local:
  codex-home-c:
```

- [ ] **Step 7: networks 宣言を書き換える**

`docker-compose.yml` 末尾の `networks:` ブロックで `svc-computer-net:` を次に置き換える。

```yaml
  # Open WebUI ↔ 各チームの Computer 専用。チームごとに分けることで
  # Computer コンテナ同士を相互に到達不能にしています。
  #
  # ⚠️ internal: true にはできません。Computer は OpenAI / Anthropic API と
  #    Tavily に出る必要があり、また ports 公開でホストからも入ってきます。
  svc-computer-net-a:
    driver: bridge
  svc-computer-net-b:
    driver: bridge
  svc-computer-net-c:
    driver: bridge
```

- [ ] **Step 8: .env.example の §15 / §16 を更新する**

§15「Open WebUI Computer」で、次を行う。

1. `OPEN_WEBUI_COMPUTER_PORT` / `OPEN_WEBUI_COMPUTER_URL` / `OPEN_WEBUI_COMPUTER_GATEWAY_KEY` / `OPEN_WEBUI_COMPUTER_ENABLED` の 4 変数を、それぞれ `_A` / `_B` / `_C` の 3 つ組に展開する
2. `COMPUTER_DEV_PORT_VITE` / `COMPUTER_DEV_PORT_NODE` / `COMPUTER_DEV_PORT_HTTP` / `COMPUTER_DEV_BIND` の 4 変数と、それに付随するコメントを**削除する**
3. `OPEN_WEBUI_COMPUTER_MEMORY_LIMIT` を `3G`、`OPEN_WEBUI_COMPUTER_CPU_LIMIT` を `2.0` に変え、`OPEN_WEBUI_COMPUTER_MEMORY_RESERVATION=1G` を追加する

展開後の 3 つ組は次の形にする。

```
# --- team-a ---------------------------------------------------------------
OPEN_WEBUI_COMPUTER_PORT_A=8001
OPEN_WEBUI_COMPUTER_URL_A=http://localhost:8001
# ⚠️ 各 Computer の Settings → Admin → Gateway で発行します。
#    sk-cptr-... は 1 回しか表示されず、以降はハッシュ保存です。
OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A=
OPEN_WEBUI_COMPUTER_ENABLED_A=false

# --- team-b ---------------------------------------------------------------
OPEN_WEBUI_COMPUTER_PORT_B=8002
OPEN_WEBUI_COMPUTER_URL_B=http://localhost:8002
OPEN_WEBUI_COMPUTER_GATEWAY_KEY_B=
OPEN_WEBUI_COMPUTER_ENABLED_B=false

# --- team-c ---------------------------------------------------------------
OPEN_WEBUI_COMPUTER_PORT_C=8003
OPEN_WEBUI_COMPUTER_URL_C=http://localhost:8003
OPEN_WEBUI_COMPUTER_GATEWAY_KEY_C=
OPEN_WEBUI_COMPUTER_ENABLED_C=false
```

削除した開発サーバポートの代替として、§15 に次の注記を追加する。

```
# ⚠️ コンテナ内で立てた開発サーバをホストで見る方法:
#    Computer の Browser タブ / Port Preview を使ってください
#    （HTTP プロキシ型。http://localhost:5173 をそのまま開けます）。
#    ホストからは各 Computer の公開ポート経由で見えるため、追加のポート公開は
#    不要です。WebSocket もトンネルされるので HMR も通ります。
#
# ⚠️ 開発サーバのポート公開は廃止しました。複数人だと 2 人目の vite が
#    5173 を取れず（EADDRINUSE）5174 へずれ、公開していないので見えません。
#    固定ポートの公開は構造的に複数人と両立しません。
#
# ⚠️ Browser タブの「モード」は proxy のままにしてください
#    （Settings → Admin → Browser）。chrome モードは CDP 方式で、
#    このイメージに Chrome は入っていないため 409 で失敗します。
```

- [ ] **Step 9: 検証**

Run:
```bash
docker compose config -q && echo "compose OK"
```
Expected: `compose OK`

Run:
```bash
docker compose config --services | sort
```
Expected: `open-webui-computer-a` / `-b` / `-c` の 3 つが含まれ、`open-webui-computer`（サフィックス無し）は存在しない

Run:
```bash
source .venv/bin/activate && python3 -c "
import json, subprocess
cfg = json.loads(subprocess.run(['docker','compose','config','--format','json'],capture_output=True,text=True).stdout)
env = cfg['services']['open-webui']['environment']
urls = env['OPENAI_API_BASE_URLS'].split(';')
keys = env['OPENAI_API_KEYS'].split(';')
cfgs = json.loads(env['OPENAI_API_CONFIGS'])
assert len(urls) == 4, urls
assert len(keys) == 4, len(keys)
assert set(cfgs) == {'0','1','2','3'}, set(cfgs)
for k in ('1','2','3'):
    assert cfgs[k]['api_type'] == '', k
    assert len(cfgs[k]['headers']) == 5, k
print('接続 4 本 / configs 添字 0-3 / ヘッダ 5 種 OK')
"
```
Expected: `接続 4 本 / configs 添字 0-3 / ヘッダ 5 種 OK`

Run:
```bash
grep -n "COMPUTER_DEV_PORT\|COMPUTER_DEV_BIND" docker-compose.yml .env.example || echo "開発ポート変数の残存なし"
```
Expected: `開発ポート変数の残存なし`

---

## Task 4: Functions — chat state の user_id スコープ化

**Files:**
- Modify: `docs/DetailedDesign/functions_contract.md:550-605`（§5.3）
- Modify: `scripts/check_functions.py:56-86`（`SHARED` リスト）
- Modify: `functions/descript_pipe.py`（ヘルパ `:205-256` と呼び出し元 9 箇所）
- Modify: `functions/descript_studio.py`（ヘルパ `:198-208` と呼び出し元 1 箇所）
- Modify: `functions/descript_guard.py`（ヘルパ `:222-273` と呼び出し元 4 箇所）

**Interfaces:**
- Consumes: なし（Task 1〜3 とは独立）
- Produces: 次の 4 つのシグネチャ。Task 5 が `_user_id` を再利用する
  - `def _user_id(user_raw: Any) -> Optional[str]`
  - `async def _load_state(chat_id: Optional[str], user_id: Optional[str]) -> dict`
  - `async def _save_state(chat_id: Optional[str], patch: dict, user_id: Optional[str]) -> dict`
  - `async def _append_history(chat_id: Optional[str], entry: dict, user_id: Optional[str]) -> None`
  - `ctx["user_id"]`（`descript_pipe.py` の ctx dict に追加されるキー）

**背景:** `chat_id` は完全にクライアント制御である。`utils/actions.py:66-68` を見ると `data['chat_id']` は POST された `form_data` そのもので、ユーザとの突き合わせ検証が無い。現在のヘルパはスコープ無しの `Chats.get_chat_by_id` を使っているため、他ユーザの state を読んで封筒 `state` として返し（`_ok()` は `descript_pipe.py:471-476`）、さらに `Chats.update_chat_by_id` がチャット JSON を丸ごと置換する（`models/chats.py:606`）ため他人のチャットを破壊できる。

**⚠️ 順序を守ること。** `_load_state` / `_save_state` / `_append_history` は `scripts/check_functions.py` の `SHARED` に載っており、2 ファイル以上に存在する場合はバイト単位で同一でなければならない。契約書を正本として先に直し、そこから 3 ファイルへ同一内容を展開する。

- [ ] **Step 1: 契約書 §5.3 を改訂する**

`docs/DetailedDesign/functions_contract.md` の `### 5.3 状態の読み書き` のコードブロック全体を次に置き換える。

```python
def _user_id(user_raw: Any) -> Optional[str]:
    """__user__ から user_id を取り出す。dict / UserModel のどちらでも動く。

    マルチユーザ環境では chat state とファイルの所有者検証にこの値を使う。
    取れなかった場合は None を返し、呼び出し側は fail-closed で扱う。
    """
    if user_raw is None:
        return None
    if isinstance(user_raw, dict):
        value = user_raw.get("id")
    else:
        value = getattr(user_raw, "id", None)
    return str(value) if value else None


async def _load_state(chat_id: Optional[str], user_id: Optional[str]) -> dict:
    """chat.chat['descript'] を読む。

    ⚠️ 所有者検証つきの get_chat_by_id_and_user_id を使うこと
    （backend/open_webui/models/chats.py:1540）。chat_id はクライアント制御
    （utils/actions.py:66-68 が form_data をそのまま使う）なので、
    スコープ無しの get_chat_by_id では他ユーザの state を読めてしまう。
    読んだ state は _ok() の封筒に載って呼び出し元へ返るため実害がある。

    user_id が取れない経路は fail-closed で {} を返す。
    """
    if not chat_id or not user_id:
        return {}
    try:
        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
    except Exception:
        return {}
    if chat is None:
        return {}
    state = (chat.chat or {}).get(_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


async def _save_state(chat_id: Optional[str], patch: dict, user_id: Optional[str]) -> dict:
    """chat.chat['descript'] にパッチをマージして保存し、マージ後の state を返す。

    update_chat_by_id は blob に 'title' が無いとチャットタイトルを
    'New Chat' にリセットする（models/chats.py:608）ため、必ず補う。

    ⚠️ 取得も所有者検証つきの get_chat_by_id_and_user_id を使うこと。
    update_chat_by_id は chat.chat を丸ごと置換する（models/chats.py:606）ので、
    スコープ無しで取得すると他ユーザのチャットを破壊できる。

    書けなかった場合もマージ後の state を返す。封筒の state は
    「このリクエストが認識している状態」であり、永続化の成否とは別だからである。
    """
    if not chat_id or not user_id:
        return dict(patch or {})
    try:
        chat = await Chats.get_chat_by_id_and_user_id(chat_id, user_id)
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


async def _append_history(chat_id: Optional[str], entry: dict, user_id: Optional[str]) -> None:
    state = await _load_state(chat_id, user_id)
    history = list(state.get("history") or [])
    history.append({"ts": int(time.time()), **entry})
    await _save_state(chat_id, {"history": history}, user_id)
```

さらに、このコードブロックの直後に次の注記を追加する。

```markdown
> ⚠️ **`chat_id` はクライアント制御である。** Action の `body['chat_id']` は
> POST された `form_data` そのもの（`utils/actions.py:66-68`）、Pipe の
> `__chat_id__` と Filter の `body['chat_id']` も同様に検証されていない。
> `user_id` を伴わない読み書きは、他ユーザのチャット状態への読み出しと
> 全置換書き込みになる。**スコープ無しの `Chats.get_chat_by_id` を
> ここで使ってはならない。**

> ⚠️ **既知の制約（未修正）**: `_save_state` は read-modify-write であり、
> 排他制御を持たない。同一チャットを 2 タブで同時操作すると後勝ちで
> `descript` state が失われる。より深刻な場合、Open WebUI 自身のメッセージ
> 保存経路と競合すると、古い `blob` の書き戻しでメッセージがロールバック
> されうる（`models/chats.py:606` が `chat_item.chat` を丸ごと差し替えるため）。
> §3.3 の「書き手は Pipe のみ」という規律が守るのは単一リクエスト内の順序
> だけである。
```

- [ ] **Step 2: check_functions.py の SHARED に `_user_id` を追加する**

`scripts/check_functions.py` の `SHARED` リストで、`"_as_user_model",` の直後に次の 1 行を挿入する。

```python
    "_user_id",
```

- [ ] **Step 3: descript_pipe.py にヘルパを展開する**

`functions/descript_pipe.py` の `_load_state` / `_save_state` / `_append_history` の 3 関数（`:205-256`）を、Step 1 のコードブロックの内容で**そっくり置き換える**。`_user_id` は `_load_state` の直前に置く（契約書と同じ並び）。

置き換える範囲は次のコメント行の直後から `_append_history` の末尾まで。

```python
# ===========================================================================
# 契約書 §5.3 状態の読み書き
# ===========================================================================
```

- [ ] **Step 4: descript_pipe.py の ctx に user_id を追加する**

`functions/descript_pipe.py:1866-1884` の ctx dict で、`"user": _as_user_model(__user__),` の直後に次の 1 行を挿入する。

```python
                # マルチユーザ環境での chat state / ファイルの所有者検証に使う。
                # 取れない経路は fail-closed（state を読まない・書かない）。
                "user_id": _user_id(__user__),
```

- [ ] **Step 5: descript_pipe.py の呼び出し元を更新する**

次の 9 箇所を書き換える。第 3 引数（`_load_state` は第 2 引数）として `ctx` から `user_id` を渡す。

| 行 | 変更前 | 変更後 |
|---|---|---|
| `:1885` | `await _load_state(ctx["chat_id"])` | `await _load_state(ctx["chat_id"], ctx.get("user_id"))` |
| `:2172` | `await _save_state(ctx.get("chat_id"), {"tool_map": resolved})` | `await _save_state(ctx.get("chat_id"), {"tool_map": resolved}, ctx.get("user_id"))` |
| `:2388` | `await _save_state(` 開始の複数行呼び出し | 最終引数に `ctx.get("user_id")` を追加 |
| `:2464` | `await _save_state(ctx.get("chat_id"), patch)` | `await _save_state(ctx.get("chat_id"), patch, ctx.get("user_id"))` |
| `:2503` | `await _save_state(` 開始の複数行呼び出し | 最終引数に `ctx.get("user_id")` を追加 |
| `:2618` | `await _save_state(` 開始の複数行呼び出し | 最終引数に `ctx.get("user_id")` を追加 |
| `:2807` | `await _save_state(ctx.get("chat_id"), patch)` | `await _save_state(ctx.get("chat_id"), patch, ctx.get("user_id"))` |
| `:3432` | `await _save_state(ctx.get("chat_id"), patch)` | `await _save_state(ctx.get("chat_id"), patch, ctx.get("user_id"))` |
| `:3523` | `return await _save_state(ctx.get("chat_id"), patch)` | `return await _save_state(ctx.get("chat_id"), patch, ctx.get("user_id"))` |

さらに `_append_history` の呼び出し 5 箇所（`:2465` / `:2622` / `:2808` / `:2872` / `:3433`）にも最終引数として `ctx.get("user_id")` を追加する。

**行番号はヘルパ差し替えでずれる。** 次のコマンドで実際の位置を確認してから編集すること。

```bash
grep -n "_load_state(\|_save_state(\|_append_history(" functions/descript_pipe.py
```

- [ ] **Step 6: descript_studio.py にヘルパを展開する**

`functions/descript_studio.py:198-208` の `_load_state` を Step 1 の内容で置き換え、その直前に `_user_id` を追加する。

**`_save_state` と `_append_history` は展開しない。** 契約書 §3.3 のとおり state の書き手は Pipe のみで、Action は読み取り専用である。既存のコメント（`:190-196`）はこの方針を説明しているので残す。

- [ ] **Step 7: descript_studio.py の呼び出し元を更新する**

`functions/descript_studio.py:1761` を書き換える。この行は `_act_export(self, *, body, user, request, event_call, emitter, started)` の中にあり、`user` が引数として渡っている（`:1727-1729`）。

変更前:
```python
        state = await _load_state(body.get("chat_id"))
```

変更後:
```python
        state = await _load_state(body.get("chat_id"), _user_id(user))
```

- [ ] **Step 8: descript_guard.py にヘルパを展開する**

`functions/descript_guard.py:222-273` の `_load_state` / `_save_state` / `_append_history` を Step 1 の内容でそっくり置き換え、その直前に `_user_id` を追加する。

- [ ] **Step 9: descript_guard.py の `_inject_hint` に user を渡す**

`_inject_hint` は現在 `user` を受け取っていない。呼び出し元の `_inlet` には `user` が引数として存在する（`:607`）。

`functions/descript_guard.py:627` を変更する。

変更前:
```python
            await self._inject_hint(body, metadata)
```

変更後:
```python
            await self._inject_hint(body, metadata, user)
```

`functions/descript_guard.py:732` のシグネチャを変更する。

変更前:
```python
    async def _inject_hint(self, body: dict, metadata) -> None:
```

変更後:
```python
    async def _inject_hint(self, body: dict, metadata, user) -> None:
```

`functions/descript_guard.py:736` を変更する。

変更前:
```python
        state = await _load_state(chat_id)
```

変更後:
```python
        state = await _load_state(chat_id, _user_id(user))
```

- [ ] **Step 10: descript_guard.py の outlet 側を更新する**

`_outlet(self, body, user)` は `user` を既に受け取っている（`:841`）。

`functions/descript_guard.py:846` を変更する。

変更前:
```python
        state = await _load_state(chat_id)
```

変更後:
```python
        state = await _load_state(chat_id, _user_id(user))
```

`functions/descript_guard.py:888` から始まる `_append_history` の複数行呼び出しで、最終引数として `_user_id(user)` を追加する。

- [ ] **Step 11: 検証**

Run:
```bash
source .venv/bin/activate && python3 scripts/check_functions.py
```
Expected: 終了コード 0。`共通ヘルパ _user_id: 3 ファイルで一致` / `共通ヘルパ _load_state: 3 ファイルで一致` / `共通ヘルパ _save_state: 2 ファイルで一致` / `共通ヘルパ _append_history: 2 ファイルで一致` が「確認できたこと」に並ぶ

Run:
```bash
grep -n "Chats.get_chat_by_id(" functions/*.py || echo "スコープ無し get_chat_by_id の残存なし"
```
Expected: `スコープ無し get_chat_by_id の残存なし`

Run:
```bash
source .venv/bin/activate && for f in functions/descript_pipe.py functions/descript_studio.py functions/descript_guard.py; do python3 -c "import ast,sys; ast.parse(open('$f').read()); print('$f 構文 OK')"; done
```
Expected: 3 ファイルとも `構文 OK`

Run:
```bash
grep -n "_load_state(\|_save_state(\|_append_history(" functions/*.py | grep -v "async def" | grep -vE ", *(ctx\.get\(\"user_id\"\)|_user_id\(user\)|user_id)\)" || echo "引数未追加の呼び出しなし"
```
Expected: `引数未追加の呼び出しなし`（複数行呼び出しは行末で閉じないため、この検査に引っかかった行は目視で確認する）

---

## Task 5: Functions — file_id の所有者検証

**Files:**
- Modify: `docs/DetailedDesign/functions_contract.md`（§5.3 の直後に注記を追加）
- Modify: `functions/descript_pipe.py`（`_resolve_stored_file` `:3155-` と呼び出し元 `:3116`）

**Interfaces:**
- Consumes: Task 4 が定義した `ctx["user_id"]`
- Produces: `async def _resolve_stored_file(entry: dict, user_id: Optional[str]) -> dict`

**背景:** `_resolve_stored_file` はスコープ無しの `Files.get_file_by_id` を使っている（`models/files.py:153`）。`metadata` 以外の任意キーは Pipe の `body` に素通しされる（`routers/functions.py:206`）ため、一般ユーザが `/api/chat/completions` へ次を投げるだけで他ユーザの動画を自分の Descript プロジェクトへ持ち出せる。

```json
{"model": "descript_pipe", "descript_op": "import_media",
 "descript_args": {"file_id": "<他ユーザのファイル id>"}}
```

`_dispatch`（`:1917-1921`）→ `_op_import_media`（`:2417-2422`）→ `:3115` → `_resolve_stored_file` → `_upload_media`（`:3210-`）で実体が PUT される。もう 1 つの入口として `body["descript_media"]`（`_as_media_list`, `:1974-1995`）もあるが、どちらも `_resolve_stored_file` を通るためここ 1 箇所で塞げる。

**⚠️ `_resolve_stored_file` は `SHARED` に載っていない**（`descript_pipe.py` にしか存在しない）ため、ドリフト検査の対象外である。ただし契約書に注記は残す。

- [ ] **Step 1: `_resolve_stored_file` のシグネチャと取得方法を変える**

`functions/descript_pipe.py` の `_resolve_stored_file` の先頭部分を変更する。

変更前:
```python
    @staticmethod
    async def _resolve_stored_file(entry: dict) -> dict:
        """Open WebUI に保存済みのファイル（file_id）の実体パス・サイズを解決する。

        Files.get_file_by_id で DB レコードを引き（backend/open_webui/models/files.py:153）、
        Storage.get_file で実体をローカルパスに解決する
        （backend/open_webui/storage/provider.py:70 / 162）。
        STORAGE_PROVIDER=s3（MinIO）でも S3StorageProvider.get_file が UPLOAD_DIR に
        ダウンロードしてそのパスを返すため同じ手順で読める（provider.py:162-170）。
        boto3 は同期なので upstream と同じく asyncio.to_thread に逃がす
        （backend/open_webui/routers/files.py:799）。
        """
        file_id = str(entry.get("file_id") or "")
        try:
            record = await Files.get_file_by_id(file_id)
        except Exception:
            record = None
```

変更後:
```python
    @staticmethod
    async def _resolve_stored_file(entry: dict, user_id: Optional[str]) -> dict:
        """Open WebUI に保存済みのファイル（file_id）の実体パス・サイズを解決する。

        Files.get_file_by_id_and_user_id で DB レコードを引き
        （backend/open_webui/models/files.py:168）、
        Storage.get_file で実体をローカルパスに解決する
        （backend/open_webui/storage/provider.py:70 / 162）。
        STORAGE_PROVIDER=s3（MinIO）でも S3StorageProvider.get_file が UPLOAD_DIR に
        ダウンロードしてそのパスを返すため同じ手順で読める（provider.py:162-170）。
        boto3 は同期なので upstream と同じく asyncio.to_thread に逃がす
        （backend/open_webui/routers/files.py:799）。

        ⚠️ 所有者検証つきの get_file_by_id_and_user_id を使うこと。
        file_id はクライアント制御である。metadata 以外の任意キーは Pipe の
        body に素通しされる（routers/functions.py:206）ため、スコープ無しの
        get_file_by_id（models/files.py:153）を使うと、他ユーザがアップロードした
        動画を攻撃者自身の Descript プロジェクトへ持ち出せてしまう。

        user_id が取れない経路は fail-closed とし、下の未検出エラーへ合流させる。
        """
        file_id = str(entry.get("file_id") or "")
        record = None
        if file_id and user_id:
            try:
                record = await Files.get_file_by_id_and_user_id(file_id, user_id)
            except Exception:
                record = None
```

以降の `if record is None or not record.path:` 以下は変更しない。所有者でない場合も「添付ファイルの実体を取得できませんでした」という既存の `TOOL_ARGS_MISSING` に合流し、他ユーザのファイルの存在自体を推測させない。

- [ ] **Step 2: 呼び出し元を更新する**

`functions/descript_pipe.py:3116` を変更する。この行は `_run_import_media`（`:3093`）の中にあり、`ctx` がスコープにある。

変更前:
```python
                prepared.append(await self._resolve_stored_file(entry))
```

変更後:
```python
                prepared.append(await self._resolve_stored_file(entry, ctx.get("user_id")))
```

- [ ] **Step 3: 契約書に注記を追加する**

`docs/DetailedDesign/functions_contract.md` の §5.3 の末尾（Task 4 Step 1 で追加した 2 つの注記の直後）に次を追加する。

```markdown
> ⚠️ **`file_id` もクライアント制御である。** `_resolve_stored_file()`
> （`descript_pipe.py` のみ）は `Files.get_file_by_id_and_user_id`
> （`models/files.py:168`）を使うこと。スコープ無しの `Files.get_file_by_id`
> （`:153`）では、他ユーザがアップロードした動画を攻撃者自身の Descript
> プロジェクトへ持ち出せる。入口は `descript_args.file_id` と
> `body['descript_media']` の 2 つあるが、どちらも `_resolve_stored_file` を
> 通るためここ 1 箇所で塞げる。
```

- [ ] **Step 4: 検証**

Run:
```bash
source .venv/bin/activate && python3 scripts/check_functions.py
```
Expected: 終了コード 0

Run:
```bash
grep -n "Files.get_file_by_id(" functions/*.py || echo "スコープ無し get_file_by_id の残存なし"
```
Expected: `スコープ無し get_file_by_id の残存なし`

Run:
```bash
grep -n "_resolve_stored_file(" functions/descript_pipe.py
```
Expected: 2 行のみ。定義側が `async def _resolve_stored_file(entry: dict, user_id: Optional[str]) -> dict:`、呼び出し側が `self._resolve_stored_file(entry, ctx.get("user_id"))`

---

## Task 6: セットアップ手順書

**Files:**
- Create: `docs/Setup/multi_user_setup.md`

**Interfaces:**
- Consumes: Task 1〜5 のすべて。環境変数名（`OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A` 等）、サービス名（`open-webui-computer-a` 等）、ボリューム名（`open-terminal-home` 等）を参照する
- Produces: なし（末端の成果物）

**背景:** 順序を誤ると黙って反映されない設定が複数ある。`TERMINAL_SERVER_CONNECTIONS` / `OPENAI_API_CONFIGS` / `DEFAULT_USER_ROLE` / `USER_PERMISSIONS_*` はいずれも PersistentConfig で、`Config.seed_defaults()` が「DB に無いキーだけ INSERT」する（`models/config.py:256`）ため、環境変数は初回起動時にしか取り込まれない。

- [ ] **Step 1: 手順書を作成する**

`docs/Setup/multi_user_setup.md` を作成する。次の 6 つを **`## ` レベルの見出し**にする（Step 2 の検査がこの数を数える）。

```markdown
## 全体像
## 移行手順（既存環境から）
## セットアップの順序
## Functions の配布
## ユーザの追加手順
## 既知の限界
```

**① 全体像** — 3 層（認証・認可 / 実行環境 / アプリ）と、層ごとの分離の強さを表で示す。設計ドキュメント `docs/superpowers/specs/2026-08-27-multi-user-design.md` へのリンクを張る。

**② 移行手順（既存環境から）** — 次のコマンドと注意を順に書く。

```bash
# 1. Open Terminal のコンテナとボリュームを削除する
#    ⚠️ 既存ボリュームを「マウント先だけ /home に変える」のは壊れます。
#       中身は /etc/skel 由来の dotfile 24KB のみで、移行価値はありません。
docker compose rm -sf open-terminal
docker volume rm short-video-creation_open-terminal-data

# 2. Computer のボリューム名がチーム別に変わります。
#    ⚠️ codex-home を新しい名前にすると Codex の再ログインが必要です。
#    ⚠️ /workspace に残したい作業ファイルがある場合は先に退避してください:
docker cp svc-open-webui-computer:/workspace ./computer-workspace-backup
docker compose rm -sf open-webui-computer
```

**③ セットアップの順序（13 ステップ）** — 設計ドキュメント §9 の 13 ステップをそのまま展開する。各ステップに実コマンドか UI の操作パスを添える。特に次を強調する。

- gateway キーの平文は 1 回しか表示されない
- Open WebUI の起動は Computer 3 台のキーを `.env` に入れた後
- `TERMINAL_SERVER_CONNECTIONS` の `access_grants` は `[]` で seed されるので、グループ作成後に Admin Settings → Integrations で 3 グループの read grant を付ける

**④ Functions の配布** — Model エントリ経由でしか制御できないことを説明する。

- Functions にはアクセス制御が存在しない（`function` テーブルに `access_control` 列が無く、`access_grant` の `resource_type` 10 種にも `function` が無い）
- 制御軸は `is_active` と `is_global` の 2 つだけ
- `is_global` は 3 つの Function すべてでオフにする
- Workspace → Models で Model エントリを 1 つ作り、`filterIds` に `descript_guard`、`actionIds` に `descript_studio` を紐づけ、`team-a` / `team-b` / `team-c` の read grant を 3 つ付ける
- Computer の `cptr/<workspace>` は接続が 3 本に分かれるため、Model エントリも 3 つ作りチームごとに grant を付ける
- 未登録のモデルは管理者にしか見えない（`utils/models.py:517-523`）ので、Model エントリを作るまで一般ユーザからは不可視

**⑤ ユーザの追加手順** — `pending` で作られたユーザを承認し、グループへ追加する 2 手。承認だけではグループ未所属のため何も見えない点を明記する。加えて、各ユーザがチャット入力欄の ＋ → Integrations → Tools から Descript を一度手動で有効化し OAuth 同意を完了する必要があること（自動化不可）を書く。

**⑥ 既知の限界** — 設計ドキュメント §7 の 9 項目をそのまま表で展開する。特に次の 3 つは太字で強調する。

- **Open Terminal は他人の PTY セッションにアタッチできる。** ファイル分離は本物だが、セッション・プロセス出力・ポートは全ユーザ共有
- **同一チーム内の Computer には分離が無い。** 境界はチームのコンテナだけ
- **Computer のロール降格は最大 30 日効かない。** JWT が DB を読まないため。即時失効には `config.toml` の `[server] secret` ローテートが必要で、全員ログアウトとプロバイダキー復号不能を伴う

- [ ] **Step 2: 検証**

Run:
```bash
test -f docs/Setup/multi_user_setup.md && wc -l docs/Setup/multi_user_setup.md
```
Expected: ファイルが存在する

手順書に登場する識別子が実際の定義と一致することを確認する。

Run:
```bash
for name in open-webui-computer-a open-webui-computer-b open-webui-computer-c open-terminal-home OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A OPEN_TERMINAL_MULTI_USER; do
  if grep -q "$name" docs/Setup/multi_user_setup.md; then echo "OK   $name"; else echo "NG   $name が手順書に無い"; fi
done
```
Expected: 6 件すべて `OK`

Run:
```bash
grep -c "^## " docs/Setup/multi_user_setup.md
```
Expected: `6`（6 つの節）

---

## Task 7: CLAUDE.md の更新

**Files:**
- Modify: `.claude/CLAUDE.md`

**Interfaces:**
- Consumes: Task 1〜6 のすべて
- Produces: なし（末端の成果物）

**背景:** `.claude/CLAUDE.md` は現在シングルユーザ前提で書かれており、Computer の開発サーバポート公開（削除済み）や `open-terminal-data:/home/user`（変更済み）など、実態と食い違う記述が残る。

- [ ] **Step 1: リポジトリ構成の説明を更新する**

冒頭のディレクトリツリーの説明で、`docker-compose.yml` の行を次に置き換える。

```
docker-compose.yml          PostgreSQL(pgvector) / Redis / MinIO / Open WebUI
                            + Open Terminal（組み込みマルチユーザ）
                            + Open WebUI Computer × 3（チーム別）
```

`docs/Setup/` の行に `multi_user_setup.md` を追加する。

- [ ] **Step 2: 「実行環境の 3 コンポーネント」節を書き換える**

Open Terminal の説明に次を追加する。

```markdown
- **Open Terminal** — Open WebUI の**部品**。バックエンドがプロキシし、チャットのサイドバーに出る実行環境。ホストにポートを公開していない（API キーをブラウザに渡さないため）。`OPEN_TERMINAL_MULTI_USER=true` でユーザごとに Linux アカウントと home（`/home/<ユーザ ID の先頭 8 文字>`）が作られる。**分離されるのはファイルだけで、PTY セッション・プロセス出力・ポートは全ユーザ共有**（他人の稼働中シェルにアタッチできる）
```

Computer の説明を次に置き換える。

```markdown
- **Open WebUI Computer（cptr）** — **独立した 1 つのアプリ**。自前のログイン・PWA・エージェントランタイムを持ち、Open WebUI からは gateway 経由で `cptr/<workspace>` というモデルに見える。ポート公開が必須（ブラウザが直接 Socket.IO でつなぐ）。**compose 中で唯一ビルドするサービス**（`docker/computer/Dockerfile`）。**チーム別に 3 台**（`open-webui-computer-a` / `-b` / `-c`、ホストポート 8001 / 8002 / 8003）。cptr の `routers/workspace.py` と `routers/terminal.py` に所有者検査が無いため、コンテナ境界が唯一の分離手段
```

- [ ] **Step 3: 「Computer 内で立てた開発サーバをホストで見る」節を書き換える**

方法 2（compose でポートを公開）の記述を削除し、Browser タブ / Port Preview のみを残す。削除の理由として次を書く。

```markdown
**ポート公開は廃止した。** 固定ポートの公開は複数人と構造的に両立しない。Computer は 1 コンテナ = 1 つの Linux ユーザ空間で、2 人目の `vite` は 5173 を取れず（EADDRINUSE）既定の `strictPort: false` のまま 5174 へずれる。5174 は公開していないのでホストから見えない（`uvicorn` は自動でずれず起動失敗する）。Browser タブ / Port Preview は任意のポートで動きマッピングが要らないため、こちらに一本化した。
```

- [ ] **Step 4: 「注意点」節にマルチユーザの項目を追加する**

次の 4 項目を追加する。

```markdown
- **マルチユーザ前提で運用している。** 設計は `docs/superpowers/specs/2026-08-27-multi-user-design.md`、手順は `docs/Setup/multi_user_setup.md`。分離は「事故防止」レベルであって「悪意」には不十分。既知の限界は手順書の最終節を参照
- **Functions にアクセス制御は存在しない。** `function` テーブルに `access_control` 列が無く、`access_grant` の `resource_type` 10 種にも `function` が無い。制御軸は `is_active` と `is_global` の 2 つだけ。特定グループに見せたい場合は **Workspace → Models に Model エントリを作り、そこに access_grants を付ける**。「Function の権限」ではなく「Model の権限」として設計すること
- **権限はグループを OR 合成する**（`utils/access_control/__init__.py:54`）。グループは権限を足すことしかできず奪えないため、「既定を `false` に絞ってグループで `true` を足す」以外の順序は成立しない
- **`chat_id` と `file_id` はクライアント制御である。** Functions で DB を引くときは必ずスコープ付き API（`Chats.get_chat_by_id_and_user_id` / `Files.get_file_by_id_and_user_id`）を使う。スコープ無し版を使うと他ユーザの状態とファイルに到達できる
```

- [ ] **Step 5: PersistentConfig の落とし穴を追記する**

「注意点」節に次を追加する。

```markdown
- **PersistentConfig は初回起動時にしか取り込まれない。** `Config.seed_defaults()` は「DB に無いキーだけ INSERT」する（`models/config.py:256`）。`TERMINAL_SERVER_CONNECTIONS` / `OPENAI_API_CONFIGS` / `DEFAULT_USER_ROLE` / `USER_PERMISSIONS_*` はすべてこれに該当する。起動済みの環境で `.env` を変えても効かないので、Admin Settings で直すこと
```

- [ ] **Step 6: 検証**

Run:
```bash
grep -n "open-terminal-data\|COMPUTER_DEV_PORT\|15173\|13000\|18080" .claude/CLAUDE.md || echo "旧記述の残存なし"
```
Expected: `旧記述の残存なし`

Run:
```bash
for name in OPEN_TERMINAL_MULTI_USER open-webui-computer-a multi_user_setup.md get_chat_by_id_and_user_id; do
  if grep -q "$name" .claude/CLAUDE.md; then echo "OK   $name"; else echo "NG   $name が CLAUDE.md に無い"; fi
done
```
Expected: 4 件すべて `OK`

---

## 全体の最終確認

すべてのタスク完了後に一度だけ実行する。

- [ ] **Step 1: Functions の静的検査**

Run:
```bash
source .venv/bin/activate && python3 scripts/check_functions.py; echo "exit=$?"
```
Expected: `exit=0`

- [ ] **Step 2: compose の妥当性**

Run:
```bash
docker compose config -q && echo "compose OK"
```
Expected: `compose OK`

- [ ] **Step 3: 秘匿情報が入っていないこと**

Run:
```bash
grep -nE "sk-cptr-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}" docker-compose.yml .env.example docs/Setup/multi_user_setup.md functions/*.py || echo "平文の鍵なし"
```
Expected: `平文の鍵なし`

- [ ] **Step 4: 変更したファイルの一覧を確認する**

Run:
```bash
git status --porcelain
```
Expected: 次の 9 ファイルが変更または新規として並ぶ。これ以外が出ていたら意図しない変更である。

```
 M .claude/CLAUDE.md
 M .env.example
 M docker-compose.yml
 M docs/DetailedDesign/functions_contract.md
 M functions/descript_guard.py
 M functions/descript_pipe.py
 M functions/descript_studio.py
 M scripts/check_functions.py
?? docs/Setup/multi_user_setup.md
```

（`docs/superpowers/` 配下の設計ドキュメントと本計画も未追跡として出る。）

- [ ] **Step 5: 利用者に引き渡す**

ビルドと起動はリポジトリのルールにより自動実行しない。次を利用者に伝える。

```
docs/Setup/multi_user_setup.md の「移行手順」と「セットアップの順序」に沿って、
既存ボリュームの削除 → docker compose build → 各 Computer の初期化 →
gateway キーの .env への設定 → Open WebUI の起動、の順に実行してください。
```
