# マルチユーザ対応 設計

- 日付: 2026-08-27
- 対象: Open WebUI / Open Terminal / Open WebUI Computer / Descript Functions
- 前提: Open WebUI Enterprise License は使用しない

## 1. 背景と目的

本ツールは現在、実質 1 ユーザでの利用を前提に構成されている。2 人目のアカウントを作った時点で、以下が同時に成立してしまう。

- 全員が Open Terminal の同一シェル（uid 1000 `user`、パスワード無しで root 昇格可）を共有する
- Open WebUI Computer の全ファイル（Codex の `auth.json` を含む）が全員から読める
- Descript Functions が `chat_id` / `file_id` を所有者検証なしに DB へ渡すため、他ユーザのチャット状態とアップロード動画に到達できる

本設計は、少人数の社内チーム（〜10 名）が安全に同時利用できる状態を、Enterprise License 無しで到達可能な範囲で構築する。

### 目標

1. ユーザごとに認証され、所属グループによって見えるものが決まる
2. 実行環境で他人の作業ファイルを踏まない
3. Descript のプロジェクト空間とチャット状態がユーザ単位に閉じる

### 非目標

- 敵対的なユーザからの防御。本設計が守るのは「事故」であって「悪意」ではない（§7 参照）
- ユーザごとの完全な実行環境分離。これは Enterprise の Terminals オーケストレータが必要
- 不特定多数・外部ユーザへの公開

## 2. 決定事項

| 項目                            | 決定                                                                   |
| ------------------------------- | ---------------------------------------------------------------------- |
| 利用者像                        | 少人数の社内チーム（〜10 名）、相互に信頼関係あり                      |
| 構成案                          | 案 B: Open Terminal は共有 1 コンテナ、Computer はチーム別に分割       |
| チーム分割                      | 3 チームの雛形（`team-a` / `team-b` / `team-c`）。名称は後で差し替える |
| 新規ユーザ                      | `DEFAULT_USER_ROLE=pending`（管理者承認を挟む）                        |
| Computer の開発サーバポート公開 | 全廃（Browser タブ / Port Preview で代替）                             |
| Functions の所有者検証          | 今回のスコープに含める                                                 |

## 3. 調査で確定した事実

実装の前提となる事実。すべて実ソースまたは公式ドキュメントで確認済み。

### 3.1 Enterprise License にコード上の機能ゲートは存在しない

ライセンス処理の実体は `backend/open_webui/utils/auth.py:85-159` の `get_license_data()` のみ。その `data_handler()` が行うのは 4 つだけ。

| キー        | 効果                                   | 行       |
| ----------- | -------------------------------------- | -------- |
| `resources` | 静的アセット差し替え（ブランディング） | `:88-90` |
| `count`     | `app.state.USER_COUNT` にセット        | `:92`    |
| `name`      | `app.state.WEBUI_NAME` を上書き        | `:94`    |
| `metadata`  | `app.state.LICENSE_METADATA` にセット  | `:96`    |

`if license:` のような機能分岐はバックエンドに存在しない。SCIM 2.0（`env.py:851-859`）・LDAP（`config.py:2751-2786`）・グループ RBAC・監査ログ（`env.py:1144-1182`）はすべて OSS のまま動作する。

ユーザ数上限もバックエンドに強制ロジックが無い。seat 超過はフロントのバナー表示のみ（`src/lib/components/chat/Navbar.svelte:258`, `:275`）で、ライセンスキー未設定なら `license_metadata` が `null` になり全分岐が偽になる。

実際に効いてくる Enterprise 限定は次の 3 点。

- 50 ユーザ超でのブランディング除去（`LICENSE` 第 4 条）
- Terminals オーケストレータ（ユーザごとの Terminal コンテナ自動プロビジョニング）
- SLA サポート / LTS 版

### 3.2 Open WebUI の権限はグループを OR 合成する

`utils/access_control/__init__.py:32-69` の `get_permissions()` が所属グループの `permissions` を全部 OR で合成する（`:54` に「最も寛容な値が勝つ」と明記）。`has_permission()`（`:72-105`）はグループに 1 つでも `True` があれば許可し、無ければ既定値へフォールバックする。

**グループは権限を足すことしかできず、奪えない。** したがって「既定を `false` に絞り、許可グループに `true` を足す」以外の設計順序は成立しない。

### 3.3 Functions にアクセス制御は存在しない

`models/functions.py:19-32` の `Function` テーブルの列は `id / user_id / name / type / content / meta / valves / is_active / is_global / updated_at / created_at` のみ。`access_control` 列も `access_grants` 列も無い。

`access_grant` テーブルの `resource_type` として実際に使われているのは次の 10 種で、`function` は含まれない。

```
calendar / channel / folder / knowledge / model / note / prompt / shared_chat / skill / tool
```

`routers/functions.py` の一覧・作成・更新・削除・トグル・valves はすべて `get_admin_user`。一般ユーザに開いているのは `GET /`（content を返さない）と UserValves の取得・更新のみ。

**Functions の制御軸は `is_active` と `is_global` の 2 つだけ。** ユーザ / グループ単位の可視性制御は存在しない。

Pipe を特定グループに見せる唯一の経路は次のとおり。

1. Function を有効化するとモデルが生える（ID は Function の ID そのもの。`pipe.` 接頭辞は付かない — `functions.py:129-133`。`<function_id>.<sub_id>` になるのは `pipes` を持つマニフォールドだけ）
2. `model` テーブルに行が無いため、`utils/models.py:517-523` の `elif user.role == 'admin'` に落ちて **管理者にしか見えない**
3. Workspace → Models で Model エントリを作る
4. その Model に `access_grants`（`resource_type='model'`）を付ける

Filter / Action はモデルの `filterIds` / `actionIds` に紐づく（`utils/models.py:175-177`, `:231-238`）ため、可視性は完全にモデル側に従属する。

### 3.4 access_grants のワイルドカードは 2 種類ある

`models/access_grants.py:14-17`, `25-44`。

| 形                                                                 | 意味                       |
| ------------------------------------------------------------------ | -------------------------- |
| `principal_type='user'`, `principal_id='*'`, `permission='read'`   | ログイン済みの全員に read  |
| `principal_type='user'`, `principal_id='*'`, `permission='write'`  | ログイン済みの全員に write |
| `principal_type='anyone'`, `principal_id='*'`, `permission='read'` | 未認証でも read            |

`principal_type='group'` にワイルドカードは効かない。grant が空 / None なら private（オーナーと管理者のみ）で、これは `utils/access_control/__init__.py:126-127` および `:171` で明示されている。

### 3.5 Google OAuth 単体ではロール / グループの自動同期ができない

`GOOGLE_OAUTH_SCOPE` の既定は `openid email profile`（`config.py:2465`）で、`roles` / `groups` クレームが返らない。`ENABLE_OAUTH_ROLE_MANAGEMENT` の実装（`utils/oauth.py:1488`）は `oauth_claim and oauth_allowed_roles and oauth_admin_roles` の 3 つすべてが真でないとロール判定に入らない。

したがってグループは Admin UI での手動運用になる。Google Workspace のグループを反映したい場合は、前段に IdP（Keycloak / Entra ID / Authentik 等）を置いて汎用 `oidc` プロバイダとして接続する構成が必要だが、本設計の範囲外とする。

### 3.6 Open Terminal の組み込みマルチユーザはファイルのみを分離する

`OPEN_TERMINAL_MULTI_USER=true` の実装（イメージ内 `open_terminal/utils/user_isolation.py`）。

1. `X-User-Id` の値を `sanitize_username()` で英数小文字の先頭 8 文字に切り詰める（4 文字未満なら SHA-256 の先頭 8 桁）
2. `useradd -m -s /bin/bash <name>` で実アカウントを作成
3. home に `chown -R` と `chmod 2770` を適用
4. サーバプロセスのユーザ（`user`）を新ユーザのグループに `usermod -aG` で追加
5. コマンドは `sudo -u <name>` 経由、PTY は `script -qc "sudo -i -u <name>"`

`X-User-Id` は Open WebUI が無条件に送る（`routers/terminals.py:123`、`utils/tools.py:1348`）。`ENABLE_FORWARD_USER_INFO_HEADERS` とは無関係。

**分離される / されないの境界**

| 対象                                     | 分離         | 根拠                                                                             |
| ---------------------------------------- | ------------ | -------------------------------------------------------------------------------- |
| home / ファイル I/O                      | される       | カーネルのパーミッション + `utils/fs.py` の `is_path_allowed()`                  |
| コマンド実行ユーザ                       | される       | `utils/runner.py:64` の `sudo -u`                                                |
| PTY セッション一覧・接続・削除           | **されない** | `list_terminals()` が全件を返し、`ws_terminal()` が `X-User-Id` を引数に取らない |
| プロセス一覧・コマンド文字列・出力・kill | **されない** | `list_processes()` / `get_status()` / `kill_process()` にユーザ検査なし          |
| ポート / ネットワーク名前空間            | されない     | 公式ドキュメントに明記                                                           |
| CPU / メモリ / 導入パッケージ            | されない     | 同上                                                                             |

Open WebUI 側のプロキシ（`routers/terminals.py:89`）は任意パスを素通しするため、この経路は塞がれていない。

**公式ドキュメントとの差異（記録）**

- home は `/home/{user-id}` ではなく `/home/<先頭8文字>`。UUID の先頭 8 文字が一致する 2 人は同じ OS アカウントに合流する
- `utils/user_isolation.py` の docstring は `chmod 700` と書いているが、実装は `chmod 2770` + サーバユーザのグループ追加。実装のほうが分離が弱い

### 3.7 Open Terminal のボリュームは付け替えでは壊れる

現行の `open-terminal-data` ボリュームの中身は `.bashrc`（3526 B）/ `.profile`（807 B）/ `.bash_logout`（220 B）/ `.local/bin`（空）の合計 24 KB のみ。すべて Debian の `/etc/skel` 由来のスケルトンで、移行価値のある作業ファイルは存在しない。

このボリュームは空でないため、そのまま `/home` にマウントすると Docker がイメージの `/home` で seed せず、ボリュームの中身で `/home` を上書きする。実測結果は次のとおり。

- 既存ボリューム（非空）を `/home` に: dotfile が `/home` 直下に散乱し、`/home/user` が **root 所有の空ディレクトリ**として作られる。uid 1000 の `user` は自分の HOME に書けず、ログディレクトリの作成に失敗する
- 新規（空）ボリュームを `/home` に: `/home/user` が `drwx------ user user` でイメージから正しく seed される

したがって **ボリュームは名前ごと変えて作り直す**。

なお `/home/user` 自体はマルチユーザモードでも必要である。サーバプロセスは uid 1000 `user` のまま動き、`HOME=/home/user`、プロセスログは `/home/user/.local/state/open-terminal/logs/processes/*.jsonl` に出る。`/home` をマウントすればこれも配下に入る。

### 3.8 Computer は API に所有者検査を持たない

イメージ内 `cptr/routers/workspace.py` のファイル操作は絶対パスを受け取るだけで `user_id` 検査が無い。

```
workspace.py:38-39    list_directory(path: str = Query(...))
workspace.py:247-248  read_file(path: str = Query(...))
workspace.py:297-298  write_file(req: WriteFileRequest)
workspace.py:743-744  delete_item(req: DeleteRequest)
```

サインインしていれば任意のユーザが `GET /api/workspace/read?path=/home/cptr/.codex/auth.json` を通せる。`cptr/routers/terminal.py` も同様に無スコープで、`list_sessions()` は引数を取らず、他人の稼働中シェルに WebSocket でアタッチできる。

DB 上の `Workspace` 所有者は一覧表示用であってアクセス境界ではない。公式ドキュメントの表現は "Accounts are not isolation."

**この欠陥は設定でも `config.toml` でも緩和できない。コンテナ境界が唯一の分離手段である。**

### 3.9 Computer の Gateway はキー所有者のワークスペースだけを返す

`cptr/routers/gateway.py:104-105`（`GET /v1/models`）と `:588-594`（`_resolve_workspace`）が、どちらも `Workspace.get_by_user(user_id)` を呼ぶ。`models/workspaces.py:26` の `user_id` 列でスコープされる。

したがって「どのキーでどのワークスペースが見えるか」は制御できる。ただし §3.8 のとおり、そのワークスペースで動くエージェントは `run_command` でコンテナ内の何でも読めるため、**これはルーティングの分離であってセキュリティ境界ではない**。

キーは `Config` ストアの `api_keys` リストに複数保持でき（`gateway.py:772-795`）、発行者の `user_id` に紐づく。SHA-256 ハッシュ保存で平文は発行時の 1 回のみ。

### 3.10 Computer の trusted_header モードは gateway キーを発行できない

`cptr/utils/config.py:387-397` の `trusted_header` 分岐は `AuthResult(username=remote_user_header)` を返し、`user_id` は `None` のままになる（`AuthResult` の定義は `utils/config.py:33-37`）。

一方 gateway キー発行は `gateway.py:780-781` で `if not auth or not auth.user_id: raise HTTPException(401)`。

**gateway キーが必須の本構成では `trusted_header` モードを使えない。** 認証モードは `password` を維持する。

なお Computer の認証モードは環境変数では設定できない。`CPTR_*` 環境変数に認証・ユーザ管理に関するものは存在せず、`<data-dir>/config.toml` の `[auth]` セクションのみが設定箇所である。

### 3.11 Computer の JWT はロール変更を反映しない

`cptr/app.py:135-168` の auth middleware は `auth is None` だけを判定し、`verify_token()`（`utils/config.py:327-338`）は DB を読まずに JWT ペイロードから role を取る。

管理者がユーザを `pending` に降格しても、そのユーザの既存 JWT クッキー（有効期限 30 日）はフルアクセスのまま生き続ける。即時失効の唯一の手段は `config.toml` の `[server] secret` ローテートで、これは全員ログアウトかつ `encrypted:` 保存のプロバイダキーが復号不能になる。

### 3.12 Descript の認証は既にユーザ単位で分離されている

`utils/tools.py:158-167` が MCP の OAuth 2.1 トークンを `user.id` で引く。

```python
elif auth_type in ('oauth_2.1', 'oauth_2.1_static'):
        oauth_token = await request.app.state.oauth_client_manager.get_oauth_token(
            user.id, f'{connection_type}:{oauth_server_id}'
        )
```

Descript の API キー・エンドポイント URL・プロジェクト名は Functions の Valve にも存在しない。**この層は追加作業なしでマルチユーザが成立している唯一の層である。**

ただし MCP 接続の `auth_type` を `bearer` にすると `utils/tools.py:148-149` で管理者の共有キーになり、全ユーザが同一の Descript ワークスペースを共有する。この 1 設定だけが分離の可否を決める。

### 3.13 Functions のプロセス内共有状態は問題ない

3 ファイルとも次を確認済み。

- `global` 文 0 件。モジュールレベルの可変キャッシュ・シングルトン 0 件。定数 dict はすべて読み取り専用
- `__init__` は `self.valves = self.Valves()` の 1 行のみ。リクエスト状態はローカル `ctx` dict（`descript_pipe.py:1868-1884`）
- `tempfile` / `/tmp` / `mkdtemp` の使用 0 件。ファイル実体は `Storage.get_file()` に委譲
- `httpx.AsyncClient` は `async with` でリクエストスコープに閉じている（`descript_pipe.py:3237-3239`）
- MCP は接続と切断を同一 asyncio タスクで対にしている（`descript_pipe.py:2010-2122`）
- リトライ / バックオフのカウンタはすべてローカル変数

唯一のプロセス共有変異は `_apply_log_level()` が `logging.Logger` を毎回 `setLevel` する点だが、`log_level` は管理者 Valve で全ユーザ共通値のため実害はない。

### 3.14 Functions に所有者検証の欠落が 3 件ある

いずれも「Open WebUI にスコープ付き API があるのにスコープ無し版を呼んでいる」同一パターン。

| #   | 箇所                                                                        | 内容                                                                                                                                                           |
| --- | --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A   | `descript_pipe.py:3168`                                                     | `Files.get_file_by_id` に所有者検証が無い。`models/files.py:168` に `get_file_by_id_and_user_id`、`:215` に `check_access_by_user_id` があるのに `:153` を使用 |
| B   | `descript_pipe.py:209` / `descript_studio.py:202` / `descript_guard.py:226` | `_load_state` が `Chats.get_chat_by_id` を使用。`models/chats.py:1540` に `get_chat_by_id_and_user_id` がある                                                  |
| C   | `descript_pipe.py:227-245` / `descript_guard.py:244-262`                    | `_save_state` が無検証の `chat_id` で `Chats.update_chat_by_id` を呼ぶ。`models/chats.py:606` が `chat_item.chat` を丸ごと差し替える                           |

**A の到達経路**（成立を確認済み）: `metadata` 以外の任意キーは Pipe の `body` に素通しされる（`routers/functions.py:206`）。一般ユーザが `/api/chat/completions` へ次を投げるだけでよい。

```json
{
  "model": "descript_pipe",
  "descript_op": "import_media",
  "descript_args": { "file_id": "<他ユーザのファイル id>" }
}
```

`_dispatch`（`:1917-1921`）→ `_op_import_media`（`:2417-2422`）→ `:3115` → `_resolve_stored_file` → `_upload_media`（`:3210-`）で、他人の動画が攻撃者自身の Descript プロジェクトへ PUT される。もう 1 つの入口として `body["descript_media"]`（`_as_media_list`, `:1974-1995`）もある。

**B の漏洩経路**: 読んだ state は `_ok()`（`descript_pipe.py:471-476`）で `{"state": ...}` として呼び出し元に返る。他ユーザの `project_id` / `project_name` / `share_url` / `revision` / `history`（最大 50 件）が渡る。`descript_guard.py:731-758` の `_inject_hint()` は他チャットの値を攻撃者の system メッセージへ注入する。

`chat_id` の出所はすべてクライアント制御で、`utils/actions.py:66-68` を見ると `data['chat_id']` は POST された `form_data` そのものであり、ユーザとの突き合わせ検証は無い。

## 4. 設計: 層① Open WebUI の認証・認可

### 4.1 入口

```
OAUTH_ALLOWED_DOMAINS=<社内ドメイン>
DEFAULT_USER_ROLE=pending
ENABLE_OAUTH_SIGNUP=true
```

新規ユーザは `pending` で作られ、ログインできない。管理者が Admin Settings → Users で承認し、同時に所属グループへ追加する。この 2 手が「アカウント作成」の完了条件になる。

### 4.2 既定権限を閉じる

`USER_PERMISSIONS_*` のうち既定 `True` のもので、共有と越境に関わるものを `false` にする。チャット体験に直結する `features.*`（web_search / image_generation / code_interpreter / notes / channels / memories / calendar）は既定のまま開けておく。

**共有系（すべて false）**

```
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
```

**grant 操作の禁止**

```
USER_PERMISSIONS_ACCESS_GRANTS_ALLOW_USERS=false
USER_PERMISSIONS_ACCESS_GRANTS_ALLOW_GROUPS=false
```

`filter_allowed_access_grants()`（`utils/access_control/__init__.py:220-298`）は権限の無いユーザが付けた grant を**エラーにせず保存時に削る**。一般ユーザが自分のリソースを他人へ開く経路が閉じる。

**迂回路の遮断（既定のまま維持することを明示する）**

```
USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS=false
USER_PERMISSIONS_FEATURES_API_KEYS=false
USER_PERMISSIONS_FEATURES_AUTOMATIONS=false
```

`direct_tool_servers` は特に重要である。ユーザ個人設定からの direct 接続には `access_grants` が付かず（`AddTerminalServerModal.svelte:359` が `!direct` のときだけ付与）、ブラウザが Open Terminal を直叩きするため `X-User-Id` が付かない。その結果 uid 1000 = root 相当になる。本プロジェクトは Open Terminal のポートを公開していないため実際には到達できないが、この 2 つは併せて維持する。

### 4.3 管理者の越境

```
ENABLE_ADMIN_CHAT_ACCESS=false
ENABLE_ADMIN_EXPORT=false
BYPASS_ADMIN_ACCESS_CONTROL=true   # ← 2026-08-28 改訂。理由は下記
BYPASS_MODEL_ACCESS_CONTROL=false
```

これらは製品 UI 経路を塞ぐだけであり、DB やインフラへの直接アクセスは別問題である点を手順書に明記する。

> **改訂（2026-08-28）— `BYPASS_ADMIN_ACCESS_CONTROL` は `false` にできない。**
> 当初この節は 4 つとも `false` にすることで「管理者の越境」を塞ぐ設計だった。しかし `false` にすると `main.py:1077` の条件が管理者でも真になり、管理者も `check_model_access` を通る。同関数は `model` テーブルに行が無いモデルを問答無用で拒否する（`utils/models.py:447`）ため、**Workspace → Models に登録していないモデルが全員にとって使用不能**になる。
>
> しかも可視性を決める `get_filtered_models` は未登録モデルを管理者に見せる（`utils/models.py:521-523`）ので、**モデルピッカーには並ぶのに選ぶと 400 `Model not found`** という食い違いになる。実測では MODELS プール 122 件に対し Model エントリが 2 件しか無く、`POST /api/chat/completions` が全件 400 になっていた。
>
> upstream 既定も `true`（`config.py:2062` が `ENABLE_ADMIN_WORKSPACE_CONTENT_ACCESS` を継承）。一般ユーザ側の制限は `BYPASS_MODEL_ACCESS_CONTROL=false` が担うため、管理者側を `true` に戻しても一般ユーザの分離は損なわれない。失うのは「管理者が Model ACL を素通しできない」という上乗せ分だけで、`ENABLE_ADMIN_CHAT_ACCESS=false` / `ENABLE_ADMIN_EXPORT=false` は引き続き有効。

### 4.4 監査

```
AUDIT_LOG_LEVEL=REQUEST
ENABLE_AUDIT_LOGS_FILE=true
```

`AUDIT_LOG_LEVEL` の既定は `NONE` で、明示的に上げない限り何も記録されない。

### 4.5 セッション

```
WEBUI_AUTH_COOKIE_SECURE=true    # HTTPS 運用時
WEBUI_AUTH_COOKIE_SAME_SITE=lax
JWT_EXPIRES_IN=4h                # 既存の値を維持
```

`WEBUI_SECRET_KEY` は全レプリカで同一にする（既存の方針を維持）。この鍵は `OAUTH_CLIENT_INFO_ENCRYPTION_KEY`（`env.py:817`）と `OAUTH_SESSION_TOKEN_ENCRYPTION_KEY`（`env.py:819`）の既定値でもあるため、変更すると Descript MCP の保存済みトークンが復号できなくなる。

### 4.6 グループと Model エントリ

3 グループ `team-a` / `team-b` / `team-c` を Admin Settings → Users → Groups で作成する。

Functions を配布するための Model エントリを作る。

| Model エントリ               | base                         | 紐づけ                                                        | grant                                                 |
| ---------------------------- | ---------------------------- | ------------------------------------------------------------- | ----------------------------------------------------- |
| Descript 編集                | `descript_pipe`              | `filterIds: [descript_guard]`, `actionIds: [descript_studio]` | `group: team-a` / `team-b` / `team-c` の read を 3 つ |
| `cptr/<workspace>`（team-a） | Computer A の gateway モデル | —                                                             | `group: team-a`, read                                 |
| `cptr/<workspace>`（team-b） | Computer B の gateway モデル | —                                                             | `group: team-b`, read                                 |
| `cptr/<workspace>`（team-c） | Computer C の gateway モデル | —                                                             | `group: team-c`, read                                 |

Descript の Pipe は 3 チームで同一の機能を使うため、**Model エントリは 1 つとし、そこに 3 つの group grant を付ける**。チームごとに UserValves の既定や説明文を変えたくなった時点でエントリを分割する。

Computer は接続 URL がチームごとに異なるため、gateway モデルは最初から 3 エントリに分かれる。

`is_global` は 3 つの Function すべてでオフにする。オンにすると全モデル・全ユーザに適用され、モデル単位の制御が意味を失う。

### 4.7 Basic RAG とナレッジベース（2026-08-28 追記）

本節はマルチユーザ設計の当初スコープ外だったが、Basic RAG の設定が §4.2 の権限方針と直接干渉するため、決定を記録する。

**構成**

| 項目               | 選択     | 理由                                                                                       |
| ------------------ | -------- | ------------------------------------------------------------------------------------------ |
| Embedding engine   | OpenAI   | 既定の SentenceTransformers は**ワーカーあたり約 500MB**。`UVICORN_WORKERS=4` では非現実的 |
| Content extraction | Docling（独自イメージ） | 既定の pypdf は継続的な取り込みでメモリリークする。公式イメージは英語の tesseract 言語パックしか持たないため、`docker/docling/Dockerfile` で `tesseract-langpack-jpn` / `-jpn_vert` を足す（CentOS Stream 9 の AppStream にあり EPEL 不要） |
| Vector database    | PGVector | 既定の ChromaDB は SQLite ベースで fork-safe ではない（複数ワーカー / 複数レプリカで破綻） |

3 点とも「マルチユーザだから必要になる」選択であり、独立した好みではない。

**ナレッジベースの運用: 管理者が作り、チームに read grant を配る**

`USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS` は upstream 既定の `false` を維持する（`config.py:1716-1717`）。根拠は次の 3 点。

1. 一般ユーザの作成は 401 になる（`routers/knowledge.py:286-292`）が、**利用は塞がれない**。`GET /api/v1/knowledge/` は `get_verified_user` のみ（同 `:130`）なので、read grant を貰ったナレッジベースはチャットの `#` から使える。「作成 = 管理者 / 利用 = 全員」という分割が権限 1 つで成立する
2. §4.2 で `USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ALLOW_SHARING=false` としたため、仮に作成を開けても一般ユーザは共有できない（`filter_allowed_access_grants()` が保存時に grant を黙って削る）。作成だけ許しても、チームで使えるナレッジベースは作れない
3. 管理者は `filter_allowed_access_grants()` の対象外である（`utils/access_control/__init__.py:249` で早期 return）。したがって管理者が付けたグループ grant は削られず、この運用が成立する

**検索経路の認可（調査結果）**

RAG の検索経路は upstream 側で二重に守られている。層①の設計と矛盾しない。

```
アイテム単位の検査        retrieval/utils.py:1468 / 1499 / 1514
                          （admin / 所有者 / access_grants / フォルダ）
    ↓ 通過したコレクション名だけ
コレクション名単位の再検査 filter_accessible_collections (:1258)
                          file-* → has_access_to_file
                          user-memory-* → 自分の ID と一致必須
                          knowledge-bases → 非管理者は常に拒否
                          その他 → 実在する KB かつ check_access_by_user_id
```

ただし次の 2 つの環境変数はこの認可を丸ごと無効化する。いずれも PersistentConfig ではないため `.env` で再起動のたびに効く。**明示的に `false` を書いて固定する**。

- `BYPASS_RETRIEVAL_ACCESS_CONTROL`（`env.py:787`）— `retrieval/utils.py:1489 / 1565 / 1590 / 1598` の 4 箇所が検査を飛ばし、クライアントが送った `collection_name` をそのままベクトル DB へ投げる
- `ENABLE_RETRIEVAL_UNSCOPED_COLLECTIONS`（`env.py:793`）— KB に紐付かないコレクション名を既定拒否せず通す

**落とし穴（設定では直せないもの）**

- **モデルに紐付けたナレッジは、モデルの grant とは別にナレッジ自身の read grant が要る**（`retrieval/utils.py:1514-1521`）。付け忘れると「モデルは選べるのに検索結果だけ 0 件」になる。§4.6 の Model エントリ表と対になる作業であり、手順書ではステップ 14 に置いた
- 本構成は `function_calling=native` のため、モデル紐付けナレッジは `utils/middleware.py:2466` の legacy RAG 注入経路を通らず、`kb_exec` / `query_knowledge_files` というビルトインツールとしてモデルに渡る（`utils/tools.py:602-609`）。`ENABLE_KB_EXEC=true` はこの構成でこそ意味を持つ
- **日本語 OCR は設定ではなくイメージの問題である。** 公式イメージの tesseract は英語の言語パックしか持たない（docling-serve の `os-packages.txt` に `tesseract-langpack-eng` のみ）。設定で `do_ocr: true` にしても、日本語のスキャン PDF は文字化けした本文としてベクトル DB に入り検索結果を汚染する。主に日本語文書を扱う本プロジェクトでは独自イメージを作る方を選んだ（`docker/docling/Dockerfile`）。easyocr へ切り替える案は、日本語モデルがイメージに焼かれておらず実行時の外部通信が必要になるため採らない（`svc-docling-net` の `internal: true` を外すことになる）
- 管理者は `filter_accessible_collections()` を無条件で通過する（`:1289`）。§7 の限界 12 として記録する

## 5. 設計: 層② 実行環境

### 5.1 Open Terminal — 1 コンテナ + 組み込みマルチユーザ

`docker-compose.yml` の変更点。

```yaml
open-terminal:
  environment:
    OPEN_TERMINAL_MULTI_USER: "true"
    OPEN_TERMINAL_MAX_SESSIONS: ${OPEN_TERMINAL_MAX_SESSIONS:-32}
  volumes:
    - open-terminal-home:/home # 名前・マウント先とも変更
```

`volumes:` 宣言の `open-terminal-data:` を `open-terminal-home:` に置き換える。既存ボリュームは削除する（§3.7）。

`OPEN_TERMINAL_FILE_BROWSER_ROOT: home` はそのまま正しい。マルチユーザ時は各ユーザの home に解決される。ただし `docker-compose.yml:192-193` のコメント「home = /home/user（既定）」は不正確になるため修正する。

`OPEN_TERMINAL_MAX_SESSIONS` はコンテナ全体の合計上限であり、ユーザ単位ではない。既定 16 を人数で分け合う形になるため 32 に引き上げる。

**access_grants**: 初期 seed は `[]`（管理者のみ）とする。グループ ID はグループを作るまで存在せず、`TERMINAL_SERVER_CONNECTIONS` は初回起動時にしか取り込まれない（`Config.seed_defaults()` は DB に無いキーだけを INSERT する）ため、グループ grant は Admin Settings → Integrations で設定する。この順序を手順書に書く。

なお `TERMINAL_SERVER_CONNECTIONS` の `json.loads`（`config.py:386`）には try/except が無い。JSON を壊すと Open WebUI が起動しない。

**TERMINAL_PROXY_HEADERS**: Open WebUI 側に sandbox CSP を追加する。

```yaml
TERMINAL_PROXY_HEADERS: >-
  {"Content-Security-Policy": "sandbox allow-scripts; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'",
  "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}
```

こちらは `config.py:391-393` に try/except があるため、壊れた JSON でも `{}` にフォールバックして起動する。

### 5.2 Open WebUI Computer — チーム別 3 コンテナ

**構造**: YAML アンカーで共通部分を括り、チームごとの差分だけを各サービスに書く。

```yaml
x-computer-env: &computer-env
  CPTR_DATA_DIR: /data
  CPTR_LOG_LEVEL: ${OPEN_WEBUI_COMPUTER_LOG_LEVEL:-INFO}
  CPTR_LOG_FORMAT: json
  CPTR_AUDIT_LOG_LEVEL: ${OPEN_WEBUI_COMPUTER_AUDIT_LOG_LEVEL:-METADATA}
  CPTR_EXECUTE_TIMEOUT: ${OPEN_WEBUI_COMPUTER_EXECUTE_TIMEOUT:-600}
  TAVILY_API_KEY: ${TAVILY_API_KEY:-}

x-computer-base: &computer-base
  build:
    context: ./docker/computer
    args: { ... 既存のまま ... }
  image: svc/open-webui-computer:local
  restart: unless-stopped
  deploy:
    resources:
      limits:
        memory: ${OPEN_WEBUI_COMPUTER_MEMORY_LIMIT:-3G}
        cpus: "${OPEN_WEBUI_COMPUTER_CPU_LIMIT:-2.0}"
      reservations:
        memory: 1G
  healthcheck: { ... 既存のまま ... }
```

各サービスは `<<: *computer-base` を展開し、`environment` は `{<<: *computer-env, CPTR_CORS_ALLOWED_ORIGINS: ...}` の形でマージする。

**チームごとの差分**

| 項目         | team-a                                                     | team-b | team-c |
| ------------ | ---------------------------------------------------------- | ------ | ------ |
| サービス名   | `open-webui-computer-a`                                    | `-b`   | `-c`   |
| コンテナ名   | `svc-open-webui-computer-a`                                | `-b`   | `-c`   |
| ホストポート | 8001                                                       | 8002   | 8003   |
| ネットワーク | `svc-computer-net-a`                                       | `-b`   | `-c`   |
| ボリューム   | `computer-a-{data,workspace,cache,local}` + `codex-home-a` | `-b`   | `-c`   |
| gateway キー | `OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A`                        | `_B`   | `_C`   |

3 台とも `build:` を持ち、同じ `image:` タグを指す。BuildKit のキャッシュにより実質 1 回のビルドになる。

**リソース上限を下げる**: 現行は 1 台で `memory: 4G` / `cpus: 4.0`。3 台に増えるため `memory: 3G` / `cpus: 2.0` に下げ、`reservations.memory: 1G` を追加する。`limits` はピーク保護であり常時消費ではないが、3 台合計で 9G / 6 CPU がホストに要求される点は手順書に書く。

**既存ボリュームの扱い**: 現行の `open-webui-computer-data` / `-workspace` / `-cache` / `-local` / `codex-home` は、サービス名の変更に伴い team-a 用の名前に置き換える。パッケージキャッシュ（`-cache` / `-local`）は再取得されるだけなので捨ててよい。`codex-home` を新しい名前にすると **Codex の再ログインが必要**になる。`/workspace` に残したい作業ファイルがある場合は、事前に `docker cp` で退避する。

**開発サーバのポート公開は全廃する。** `COMPUTER_DEV_PORT_VITE` / `_NODE` / `_HTTP` / `COMPUTER_DEV_BIND` を `.env.example` から削除し、代わりに Browser タブ / Port Preview を使う旨を記載する。これにより複数人でのポート衝突問題（2 人目の vite が 5174 にずれて見えなくなる）が構造的に消える。

**ネットワーク**: `svc-computer-net` を `svc-computer-net-a/b/c` の 3 本に分割し、Open WebUI が 3 本すべてに参加する。これによりチーム間の Computer コンテナが相互に到達不能になる。

```yaml
open-webui:
  networks:
    [
      svc-net,
      svc-terminal-net,
      svc-computer-net-a,
      svc-computer-net-b,
      svc-computer-net-c,
    ]
```

**Open WebUI 側の接続**

```yaml
OPENAI_API_BASE_URLS: ${OPENAI_API_BASE_URL:-https://api.openai.com/v1};http://open-webui-computer-a:8000/v1;http://open-webui-computer-b:8000/v1;http://open-webui-computer-c:8000/v1
OPENAI_API_KEYS: ${OPENAI_API_KEY};${OPEN_WEBUI_COMPUTER_GATEWAY_KEY_A:-};${OPEN_WEBUI_COMPUTER_GATEWAY_KEY_B:-};${OPEN_WEBUI_COMPUTER_GATEWAY_KEY_C:-}
```

`OPENAI_API_CONFIGS` は添字 `"0"` に本物の OpenAI、`"1"` `"2"` `"3"` に各 Computer を置く。`"1"`〜`"3"` はいずれも既存と同じヘッダ 5 種と `api_type: ""`（Chat Completions）を持つ。

**認証モード**: `password` を維持する（§3.10）。Computer の Settings → Admin でセルフ登録は無効のままとし、管理者がアカウントを作る。

### 5.3 Codex

`codex-home` をチームごとに分けるため、同じ ChatGPT アカウントで 3 回 `codex login --device-auth` する運用になる。レート制限は 1 アカウントで共有される。この点を手順書に明記する。

## 6. 設計: 層③ Functions の user_id スコープ

### 6.1 変更するヘルパ

| ヘルパ                 | 現在                             | 変更後                                                 |
| ---------------------- | -------------------------------- | ------------------------------------------------------ |
| `_load_state`          | `Chats.get_chat_by_id(chat_id)`  | `Chats.get_chat_by_id_and_user_id(chat_id, user_id)`   |
| `_save_state`          | 同上 + `Chats.update_chat_by_id` | 取得をスコープ付きに変更。取得できなければ書き込まない |
| `_resolve_stored_file` | `Files.get_file_by_id(file_id)`  | `Files.get_file_by_id_and_user_id(file_id, user_id)`   |

`_load_state` と `_save_state` は 3 ファイル共通ヘルパであるため、**`docs/DetailedDesign/functions_contract.md` §5.3 を正本として先に改訂し、そこから 3 ファイルへ同一展開する**。`_resolve_stored_file` は `descript_pipe.py` のみ。

### 6.2 拒否時の挙動

いずれも fail-closed とし、他ユーザのリソースの存在自体を推測させない。

| ヘルパ                 | 拒否時                                     |
| ---------------------- | ------------------------------------------ |
| `_load_state`          | `{}` を返す（state が無い場合と同じ）      |
| `_save_state`          | 書き込まずに戻る。`_log_debug` で記録する  |
| `_resolve_stored_file` | 既存のファイル未検出エラー経路に合流させる |

エラーコードは既存のものを流用し、新規に増やさない。`scripts/check_functions.py` がエラーコードのファイル間整合を検査するため、追加する場合は 3 ファイルすべてに展開が必要になる。

### 6.3 user_id の取得元

`ctx["user"]` は既に各ファイルで組み立てられている（`descript_pipe.py:189-197` の `_as_user_model()` など）。ここから `.id` を取ってヘルパへ渡す。`__user__` が無い経路がある場合は fail-closed（state を読まない / 書かない）とする。

### 6.4 検証

```bash
python3 scripts/check_functions.py    # 0 = 問題なし
```

frontmatter、クラス属性の位置、`stream` の引数名、`replace_imports` が壊す import、**共通ヘルパの 3 ファイル間ドリフト**、`descript_op` とエラーコードの整合、秘匿情報の直書きを検査する。

### 6.5 スコープ外とするもの

`_save_state` の read-modify-write レース（`descript_pipe.py:227-245`）は今回のスコープに含めない。`Chats.update_chat_by_id` がチャット JSON を丸ごと置換するため、同一チャットの同時操作で state が後勝ちで失われ、最悪の場合は古い blob の書き戻しでメッセージがロールバックされうる。

`functions_contract.md:401-404` の「書き手は Pipe のみ」という規律が単一リクエスト内の順序しか守れないことを含め、既知の制約として `functions_contract.md` に記録する。排他制御の導入は影響範囲が別次元であるため切り分ける。

## 7. 既知の限界

本設計が守るのは事故であって悪意ではない。以下は Enterprise License 無しでは解決できない。

| #   | 限界                                              | 影響                                                                                                                                                                                                                                                                                                 |
| --- | ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Open Terminal のセッション横取り                  | 同一 Terminal を使う全ユーザが、他人の PTY セッションを一覧・アタッチ・削除でき、実行中コマンドの文字列と出力を読める                                                                                                                                                                                |
| 2   | Open Terminal のポート共有                        | ユーザがバインドしたポートに他ユーザの proxy URL から到達できる                                                                                                                                                                                                                                      |
| 3   | Open Terminal のリソース共有                      | CPU / メモリ / `MAX_SESSIONS` は全ユーザで分け合う                                                                                                                                                                                                                                                   |
| 4   | Open Terminal のユーザ名衝突                      | ユーザ ID の先頭 8 文字が一致する 2 人は同じ OS アカウントに合流する（10 名規模では実質無視できる）                                                                                                                                                                                                  |
| 5   | Computer のチーム内無分離                         | 同一チームのメンバーは互いのファイル・端末セッション・gateway キーに到達できる                                                                                                                                                                                                                       |
| 6   | Computer のロール降格が最大 30 日効かない         | 即時失効には `[server] secret` のローテートが必要で、全員ログアウトとプロバイダキー復号不能を伴う                                                                                                                                                                                                    |
| 7   | Computer の gateway キー一覧・削除が無フィルタ    | `GET /v1/keys` / `DELETE /v1/keys/{id}` に所有者検査が無い（コンテナ境界で緩和される）                                                                                                                                                                                                               |
| 8   | Functions の read-modify-write レース             | §6.5                                                                                                                                                                                                                                                                                                 |
| 9   | 監査の粒度                                        | Computer の `CPTR_AUDIT_LOG_LEVEL` は API レベルの記録であり、端末操作の全トランスクリプトではない                                                                                                                                                                                                   |
| 10  | 管理者は RAG 経由で全ファイル・全ナレッジを読める | `filter_accessible_collections()` が `user.role == 'admin'` で無条件に通す（`retrieval/utils.py:1289`）。`BYPASS_ADMIN_ACCESS_CONTROL` とは別系統で、設定では閉じられない（§4.7）。**手順書 `docs/Setup/multi_user_setup.md` では限界 12**（Playwright MCP の 2 件が先に入っているため番号がずれる） |

1〜3 を解決するには Terminals オーケストレータ（Enterprise License）が必要である。5 を解決するにはユーザごとの Computer コンテナが必要で、10 名なら 10 コンテナになる。

## 8. 変更するファイル

| ファイル                                    | 変更内容                                                                                                                                                               |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker-compose.yml`                        | Open Terminal のマルチユーザ化とボリューム変更、Computer の 3 台化、ネットワーク分割、開発サーバポートの削除、Open WebUI の接続 4 本化と `TERMINAL_PROXY_HEADERS` 追加 |
| `.env.example`                              | 新節「マルチユーザ」、Computer 3 台分の変数、権限系変数の追加、開発サーバポート変数の削除                                                                              |
| `docs/Setup/multi_user_setup.md`            | 新規。セットアップの順序と既知の限界                                                                                                                                   |
| `docs/DetailedDesign/functions_contract.md` | §5.3 の共通ヘルパ改訂、既知の制約の追記                                                                                                                                |
| `scripts/check_functions.py`                | `SHARED` リストに `_user_id` を追加（新しい共通ヘルパをドリフト検査の対象にする）                                                                                      |
| `functions/descript_pipe.py`                | `_load_state` / `_save_state` / `_resolve_stored_file`                                                                                                                 |
| `functions/descript_studio.py`              | `_load_state` / `_save_state`                                                                                                                                          |
| `functions/descript_guard.py`               | `_load_state` / `_save_state`                                                                                                                                          |
| `.claude/CLAUDE.md`                         | マルチユーザ前提の追記                                                                                                                                                 |

§4.7（2026-08-28 追記）に伴う追加分:

| ファイル                         | 変更内容                                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `docker-compose.yml`             | `docling-serve` サービスと `svc-docling-net`（`internal: true`）の追加、`UVICORN_WORKERS` 増加に伴う接続数の見直し |
| `docker/docling/Dockerfile`      | 新規。公式 docling-serve イメージに日本語の tesseract 言語パックを追加（ビルド時に `test -f` で実在を検証）        |
| `.env.example`                   | 13 節を「Basic RAG」として全面改訂、6 節 `UVICORN_WORKERS` を 4 へ、18 節にナレッジ権限と認可バイパス 2 件を追加   |
| `docs/Setup/multi_user_setup.md` | ステップ 14「Basic RAG とナレッジベース」を追加（以降を繰り下げ）、既知の限界 12 を追加                            |

## 9. セットアップの順序

順序を誤ると反映されない設定が複数あるため、手順書はこの順で書く。

1. 既存の Open Terminal コンテナとボリュームを削除する
2. `.env` を新しい `.env.example` に合わせて更新する
3. `docker compose build`（3 台とも同じ `image:` タグを指すため実質 1 回のビルドになる）
4. Computer を 3 台起動し、各コンテナのログから初回セットアップ URL（`/?token=...`）を拾って管理者アカウントを作る
5. 各 Computer で Settings → Admin → Connections に LLM プロバイダを登録する
6. 各 Computer で Settings → Admin → Gateway から API キーを発行し、`.env` の `OPEN_WEBUI_COMPUTER_GATEWAY_KEY_{A,B,C}` に貼る（平文は 1 回しか表示されない）
7. 各 Computer で `codex login --device-auth` を実行する
8. Open WebUI を起動する
9. Admin Settings → Users → Groups で `team-a` / `team-b` / `team-c` を作る
10. Admin Settings → Integrations で Open Terminal 接続に 3 グループの read grant を付ける
11. Admin Settings → Connections で Computer 3 接続の API Type が Chat Completions になっていることを確認する
12. Workspace → Models で Model エントリを作り、`filterIds` / `actionIds` を紐づけて grant を付ける
13. ユーザを招待し、承認してグループへ追加する

`TERMINAL_SERVER_CONNECTIONS` / `OPENAI_API_CONFIGS` / `DEFAULT_USER_ROLE` / `USER_PERMISSIONS_*` はいずれも PersistentConfig であり、環境変数は初回起動時にしか取り込まれない。既に起動済みの環境では Admin Settings で変更する。

## 10. 検証

| 対象                   | 方法                                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------------------- |
| Functions              | `python3 scripts/check_functions.py` が 0 を返す                                                  |
| compose                | `docker compose config -q` が通る                                                                 |
| Terminal の分離        | 2 アカウントでログインし、それぞれのファイルブラウザに相手のファイルが出ないことを確認する        |
| Terminal の永続化      | コンテナを再作成しても各ユーザの home が残ることを確認する                                        |
| Computer の出し分け    | team-a のユーザに `cptr/<team-b のワークスペース>` が見えないことを確認する                       |
| 権限                   | グループ未所属ユーザにターミナルと Descript モデルが見えないことを確認する                        |
| Functions の所有者検証 | ユーザ A のチャットで得た `chat_id` をユーザ B のリクエストに載せ、state が返らないことを確認する |

ビルド・起動系のコマンドはリポジトリのルールにより自動実行しない。手順書に記載し、利用者が実行する。
