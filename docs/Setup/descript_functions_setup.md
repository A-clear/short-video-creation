# Descript Functions セットアップ手順

ショート動画作成ツールの Open WebUI Functions（Actions / Filters / Pipes）を稼働させるまでの手順。

**所要時間**: 30〜45 分（Descript アカウントと OpenAI API キーが用意済みの場合）

**前提**:

- Descript のアカウントと、対象となる Drive の編集権限
- OpenAI の API キー
- Open WebUI **v0.11.0 以上**
- 管理者（admin）ロールのアカウント

---

## 全体の流れ

```
1. 環境変数を用意する
2. インフラを起動する
3. Descript MCP を登録する           ← 管理者が 1 回
4. OAuth 同意を完了する              ← 各ユーザが 1 回（自動化不可）
5. Functions を 3 本取り込む         ← 管理者が 1 回
6. Valves に MCP の info.id を設定する
7. probe でツール名を確定させる       ← ここが最重要
8. Workspace Model を作る
9. 動作確認
```

ステップ 7 が中核。**Descript MCP はツール名・引数スキーマを公開していない**ため、実行時に確認して Valve に固定する必要がある。

---

## 1. 環境変数を用意する

```bash
cp .env.example .env
```

最低限、以下を埋める。

| 変数                                        | 値                                       |
| ------------------------------------------- | ---------------------------------------- |
| `WEBUI_SECRET_KEY`                          | `openssl rand -hex 32` で生成            |
| `POSTGRES_PASSWORD`                         | 任意の強固なパスワード                   |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`   | 任意                                     |
| `OPENAI_API_KEY`                            | OpenAI の API キー                       |
| `TAVILY_API_KEY`                            | Tavily の API キー（Web 検索を使う場合） |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth を使う場合                  |

`DATABASE_URL` と `PGVECTOR_DB_URL` の `CHANGE_ME` も `POSTGRES_PASSWORD` と同じ値に置換する。

> ⚠️ **`WEBUI_SECRET_KEY` は後から変えないこと。** この鍵で Descript の OAuth トークンを暗号化しているため、値が変わると復号できなくなり `Error decrypting tokens` が発生し、全ユーザが OAuth 同意をやり直すことになる。複数レプリカ構成では**全レプリカで同一の値**にすること。

---

## 2. インフラを起動する

```bash
docker compose up -d
docker compose ps          # 全サービスが healthy になるまで待つ
docker compose logs -f open-webui
```

ブラウザで `http://localhost:8080` を開き、最初のアカウントを作成する。**最初に登録したユーザが管理者になる。**

<details>
<summary>submodule から開発起動する場合</summary>

```bash
# バックエンド
cd full-stack/open-webui/backend
source venv/bin/activate
./dev.sh                        # :8080。WEBUI_SECRET_KEY を自動生成

# フロントエンド（別ターミナル）
cd full-stack/open-webui
pnpm dev                        # :5173
```

この場合 `.env` はルートではなく `full-stack/open-webui/.env` を使う。

</details>

---

## 3. Descript MCP を登録する（管理者・1 回）

> ### ⛔ 先に必ず読むこと — `localhost` では登録できません
>
> Descript の IdP（Stytch）は、動的クライアント登録（DCR）で受け取る redirect URI に
> **HTTPS かつ非 loopback** を要求します。`http://localhost:8080` のままだと必ずこのエラーで失敗します。
>
> ```
> Dynamic client registration failed: {"error":"invalid_client_metadata",
> "error_description":"could not create client: The redirect URL for this non-public client
> must use the 'https' scheme. Localhost or loopback addresses are not allowed"}
> ```
>
> **原因**: Open WebUI は redirect URI を `{WEBUI_URL}/oauth/clients/{client_id}/callback` として組み立てます
> （`backend/open_webui/utils/oauth.py:506,510`。コールバックのルートは `main.py:2632`）。
> さらに DCR では `token_endpoint_auth_method='client_secret_post'`（`utils/oauth.py:96`）を送るため
> **confidential client** として扱われ、Stytch の厳しい方のルールが適用されます。
> MCP 仕様自体は localhost を許容しますが、**プロバイダ側のポリシーが仕様より厳しい**という状況です。
> Open WebUI 側の設定で公開クライアントに変えることはできません。
>
> **対処**: `WEBUI_URL` を、この Open WebUI に**実際に到達できる公開 HTTPS URL** にする。
> Descript がブラウザをリダイレクトした先に届く必要があるため、飾りの URL では動きません。
>
> | 方法              | 例                                               | 注意                                                                                                         |
> | ----------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
> | Cloudflare Tunnel | `cloudflared tunnel --url http://localhost:8080` | 無料・即時。ただし quick tunnel は**起動のたびにホスト名が変わる**。繰り返し使うなら named tunnel で固定する |
> | ngrok             | `ngrok http 8080`                                | 無料枠でも静的ドメインを 1 つ確保でき、ホスト名を固定できる                                                  |
> | 実ドメイン + TLS  | Caddy / nginx + Let's Encrypt                    | 本番はこれ                                                                                                   |
>
> ### ⚠️ `WEBUI_URL` は `.env` を書き換えても反映されません
>
> `WEBUI_URL` は **PersistentConfig** です。`models/config.py` の `seed_defaults` は
> 「Existing DB values take precedence over defaults」と実装されており、
> **初回起動で DB に入った値が以後は正**になります。
>
> 変更は **Admin Panel → Settings → General → `WEBUI_URL`**
> （`src/lib/components/admin/Settings/General.svelte:356`）で行ってください。
>
> 変更したら、**Register Client を押し直す**こと。
> 前回登録されたクライアントには不正な redirect URI が焼き付いているため作り直しが必要です。
> また、OAuth を行う間はトンネルを起動したままにしてください。

**Admin Panel → Settings → External Tools → ＋（Add Server）**

| 項目           | 値                                |
| -------------- | --------------------------------- |
| **Type**       | **MCP (Streamable HTTP)**         |
| **Server URL** | `https://api.descript.com/v2/mcp` |
| **Auth**       | **OAuth 2.1**                     |

**Register Client** を押してから **Save**。保存後に再起動を促されたら従う。

保存すると接続に **`info.id`** が振られる。**この値を控えておく**（ステップ 6 で使う）。確認方法:

- 管理画面の接続一覧の詳細表示
- または `GET /api/v1/configs/tool_servers`（管理者トークンが必要）

> ⚠️ **Type を間違えないこと。** OpenAPI 型で MCP を登録すると UI が無限ローディングになる。その場合は一度その接続を無効化 → リロード → MCP 型で再追加する。

> ℹ️ 接続時に **Descript のどの Drive を使うか**を選ぶ。MCP は**単一 Drive にスコープ**される。複数 Drive を扱うには接続を複数作る。

### 接続がうまくいかないとき

| 症状                              | 対処                                                                       |
| --------------------------------- | -------------------------------------------------------------------------- |
| `Failed to connect to MCP server` | `.env` の `MCP_INITIALIZE_TIMEOUT` を増やす（既定 10 秒 → 30 秒）          |
| 同上                              | Auth を Bearer にしてキーが空になっていないか確認（Descript は OAuth 2.1） |
| 同上                              | Function Name Filter List が空でエラーになる場合、カンマ 1 個 `,` を入れる |

---

## 4. OAuth 同意を完了する（各ユーザ・1 回）

**この手順は自動化できない。Functions を使う各ユーザが自分で 1 回だけ行う。**

1. チャット画面を開く
2. 入力欄の **＋** ボタン → **Integrations** → **Tools**
3. **Descript** を有効化する
4. ブラウザに Descript の同意画面が出るので、ログインして許可する

一度完了すれば、以降のトークン更新は自動で行われる。

> ⚠️ **この MCP をモデルのデフォルトツール（Workspace Model の Tools）に設定してはいけない。**
> チャット補完リクエストの最中にブラウザリダイレクトを起こせないため、`Failed to connect to MCP server` になる。必ず上記の手動有効化で同意を済ませること。

同意が済んでいない状態で Functions を使うと、`descript_guard`（Filter）が LLM を呼ぶ前に検知して以下を表示する。

```
Descript との連携が未認可です。チャット入力欄の ＋ ボタン → Integrations → Tools から
Descript を一度有効化し、ブラウザに表示される同意画面を完了してください。
```

---

## 5. Functions を取り込む（管理者・1 回）

**Admin Panel → Functions → ＋ → Import From Link**

以下の 3 つの URL を 1 つずつ取り込む（`blob` URL であること）。

```
https://github.com/A-clear/short-video-creation/blob/main/functions/descript_studio.py
https://github.com/A-clear/short-video-creation/blob/main/functions/descript_pipe.py
https://github.com/A-clear/short-video-creation/blob/main/functions/descript_guard.py
```

取り込むと**エディタにコードが載るだけ**なので、必ず **Save** を押して DB に登録する。

登録後、3 つとも**有効化（トグルを ON）**する。

| 登録名            | 種別   | 画面上の見え方                              |
| ----------------- | ------ | ------------------------------------------- |
| `descript_studio` | Action | チャットのメッセージ下に 4 つのボタン       |
| `descript_pipe`   | Pipe   | モデル選択に `Descript Orchestrator` が出る |
| `descript_guard`  | Filter | メッセージ入力欄にトグルが出る              |

### 注意点

- **このリポジトリは public である必要がある。** `raw.githubusercontent.com` をトークンなしで取得するため、private だと 404 になる
- **自動同期ではない。** ソースを修正するたびに、再インポート＋保存が必要
- **ブランチ名は URL に含まれる。** 上記は `main` を指している。検証中の作業ブランチを指す場合は `main` の部分を置き換える

> ⚠️ **日本語を含むブランチ名は避けること。**
> 現在の作業ブランチは `feature-動画編集Fuction` のように非 ASCII 文字を含む。`load/url` は URL を `https://raw.githubusercontent.com/{org}/{repo}/refs/heads/{branch}/{path}` に機械的に変換するため、非 ASCII のブランチ名だと取得に失敗する可能性がある。
> **`main` にマージしてから取り込む**のが確実。どうしても作業ブランチから取り込む場合は、ブランチ名部分を percent-encode した URL を貼ること。

---

## 6. Valves に MCP の `info.id` を設定する

**Admin Panel → Functions → 各 Function の ⚙️（Valves）**

### `descript_pipe`（Descript Orchestrator）

| Valve           | 設定値                            |
| --------------- | --------------------------------- |
| `mcp_server_id` | **ステップ 3 で控えた `info.id`** |

これだけで一旦保存する。ツール名の Valve はステップ 7 で埋める。

### `descript_guard`（Descript Guard）

| Valve           | 設定値                       |
| --------------- | ---------------------------- |
| `mcp_server_id` | **`descript_pipe` と同じ値** |

### `descript_studio`（Descript Studio）

既定値のままで動く。`pipe_model_id` は `descript_pipe` のまま。

---

## 7. `probe` でツール名を確定させる（最重要）

Descript MCP は個々のツール名・引数スキーマを公開ドキュメントに載せていないため、**実行時に確認して Valve に固定する**。

### 7-1. 確定済みの設定値（2026-08-02 時点）

**すでに実機で確認済みなので、以下をそのまま `descript_pipe` の Valves に設定すれば動きます。**

| Valve                  | 設定値                     | 自動解決     |
| ---------------------- | -------------------------- | ------------ |
| `tool_list_projects`   | `list_projects`            | ✓            |
| `tool_get_project`     | `get_project`              | ✓            |
| `tool_import_media`    | `import_media`             | ✓            |
| **`tool_agent_edit`**  | **`prompt_project_agent`** | ✗ **要設定** |
| `tool_publish`         | `publish_project`          | ✓            |
| **`tool_job_status`**  | **`wait_for_job`**         | ✗ **要設定** |
| `tool_export_timeline` | `export_timeline`          | —            |
| `tool_resolution_mode` | `valve_only`               | —            |

`tool_agent_edit` と `tool_job_status` の 2 つは名前が想像しにくく**正規表現では自動解決されません**。必ず手で設定してください。

> **なぜ `valve_only` にするのか**
> 既定の `regex_then_llm` は、Valve が空のとき正規表現と LLM でツール名を推定する。初回セットアップを楽にするための仕組みだが、推定は外れることがあり、LLM 呼び出しの分だけ遅く高価になる。**ツール名が確定したら `valve_only` に固定するのが本番の正しい状態。**

### 7-2. Descript 側が変わったときの確認方法

上表が合わなくなった場合（Descript のアップデート等）は、次の手順で取り直す。

1. チャットで何か 1 通送信し、アシスタントの応答を出す
2. 応答メッセージの下に出る **「Descript MCP 診断」** ボタンを押す
3. 全ツールの名前・説明・必須引数が表として表示される
4. 表の「推定」列を見て、各論理操作に対応する実ツール名を読み取り Valve に転記する

### 7-3. 参考：確認できた全 12 ツール

| ツール名               | 用途                                                                       |
| ---------------------- | -------------------------------------------------------------------------- |
| `list_projects`        | プロジェクト一覧（カーソルページング対応）                                 |
| `get_project`          | プロジェクト詳細（メディア・コンポジション・既存の publish 一覧）          |
| `list_folders`         | フォルダ階層の閲覧                                                         |
| `import_media`         | メディア取込（URL / Google Drive / Dropbox / 直接アップロード）            |
| `prompt_project_agent` | Underlord による自然言語編集                                               |
| `publish_project`      | 共有リンクの生成（Video / Audio）                                          |
| **`export_timeline`**  | **タイムライン書き出し（FCPXML / Premiere / DaVinci / AAF / EDL / SESX）** |
| `export_transcript`    | 文字起こし書き出し（txt / markdown / html / rtf / **srt**）                |
| `wait_for_job`         | ジョブ完了待ち（ブロッキング。既定 300 秒）                                |
| `list_jobs`            | ジョブ一覧                                                                 |
| `cancel_job`           | ジョブのキャンセル                                                         |
| `report_upload_status` | 直接アップロードの失敗通知                                                 |

> **なぜ `valve_only` にするのか**
> 既定の `regex_then_llm` は、Valve が空のとき正規表現と LLM でツール名を推定する。これは初回セットアップを楽にするための仕組みだが、推定は外れることがあり、LLM 呼び出しの分だけ遅く高価になる。**ツール名が確定したら `valve_only` に固定するのが本番の正しい状態。**

### 引数名が合わないとき

`ツール '...' の必須引数が不足しています` が出た場合、こちらが渡している引数名と Descript 側の引数名が違う。probe の表の「必須引数」列を見て、`tool_arg_map` Valve に対応を書く。

```json
{
  "agent_edit": { "project_id": "projectId", "prompt": "instruction" },
  "publish": { "media_type": "format" }
}
```

論理引数名は `project_id` / `composition_id` / `prompt` / `conversation_id` / `job_id` / `media_type` / `resolution` / `source_url` / `file_id` / `name`。

---

## 8. Workspace Model を作る

**Workspace → Models → ＋**

| 項目           | 値                                            |
| -------------- | --------------------------------------------- |
| **Base Model** | `Descript Orchestrator`（＝ `descript_pipe`） |
| **Model Name** | 例: `ショート動画作成`                        |
| **Filters**    | `Descript Guard` を有効化                     |
| **Tools**      | **何も設定しない**（ステップ 4 の警告参照）   |

### システムプロンプトを Prompts で管理する場合（任意）

**Workspace → Prompts → ＋** でプロンプトを作り、`descript_pipe` の Valve `system_prompt_command` にそのコマンド文字列を**完全一致**で設定する。

空のままなら Function 内蔵の `system_prompt_fallback` が使われるので、この手順は省略してよい。

---

## 9. 動作確認

### 9-1. 診断

「Descript MCP 診断」ボタン → ツール一覧が表示される。

### 9-2. アップロード

「動画のアップロード」ボタン → プロジェクト名と動画を指定 → Descript にプロジェクトとメディアが作られる。

`descript_studio` の Valve `form_mode` で入力方法が変わる。

| `form_mode`          | 挙動                                                                        |
| -------------------- | --------------------------------------------------------------------------- |
| `sequential`（既定） | プロジェクト名 → 動画 URL の順にダイアログが出る。**動画は公開 URL が必要** |
| `modal`              | 1 つのモーダルでプロジェクト名とファイル選択ができる                        |

### 9-3. 編集

「動画の編集」ボタン → プロジェクト選択 → 編集指示を入力。

編集内容は `descript_pipe` の **UserValves**（各ユーザが Settings から設定）で制御する。

| UserValve               | 対応する業務要件                                                       |
| ----------------------- | ---------------------------------------------------------------------- |
| `enable_silence_cut`    | サイレンスカット（無音区間の自動削除）                                 |
| `enable_filler_removal` | フィラーカット（言い淀みの自動削除）                                   |
| `enable_studio_sound`   | ノイズリダクション / オーディオ・コンプレッサー                        |
| `enable_auto_captions`  | 自動テロップ生成とタイムコード同期                                     |
| `enable_pan_zoom`       | オート・パン＆ズーム                                                   |
| `enable_broll`          | インサート・アセット配置                                               |
| `caption_language`      | テロップの言語（`ja` / `en` / `auto`）                                 |
| `editing_style`         | プリセット（`jet_cut` / `caption_focus` / `dynamic_effects` / `full`） |

編集が終わるとプレビュープレイヤーが表示され、「この指示で再編集」「これで確定する」を選べる。**確定するまでこのループが回る。**

### 9-3-1. 初回だけ確認してほしいこと

編集または書き出しが 1 回成功したら、**Descript が返す `download_url` のクエリパラメータ**を確認する（ログか、`descript_guard` の UserValve `show_raw_job_json` を一時的に ON にする）。

`descript_guard` は期限切れになる署名付き URL を恒久リンクに差し替えるが、その検出パターンは **AWS SigV4（`X-Amz-Signature` 等）と GCS（`X-Goog-Signature` / `GoogleAccessId`）を前提**にしている。Descript がこれ以外の独自方式を使っていた場合、**期限切れ URL がチャット履歴に残り、後日 403 になる**。

該当しなかった場合は `docs/DetailedDesign/functions_contract.md` §5.9 の `_SIGNED_URL_RE` にその署名パラメータ名を追加し、**3 ファイルすべてに反映**したうえで `python3 scripts/check_functions.py` で一致を確認する。

### 9-4. エクスポート

「タイムラインのエクスポート」ボタン → プロジェクト選択 → **出力形式の選択** → 結果が表示される。

選べる形式は 8 つ。

| 選択肢                         | 出力                                  | 使うツール                            |
| ------------------------------ | ------------------------------------- | ------------------------------------- |
| 動画の共有リンク（Video）      | `https://share.descript.com/view/...` | `publish_project`                     |
| 音声の共有リンク（Audio）      | 同上（Video とは別 URL）              | `publish_project`                     |
| **Final Cut Pro X（.fcpxml）** | ダウンロードリンク                    | `export_timeline` (`fcp`)             |
| Premiere Pro XML               | 同上                                  | `export_timeline` (`premiere`)        |
| DaVinci Resolve XML            | 同上                                  | `export_timeline` (`davinci_resolve`) |
| Pro Tools / Logic（AAF）       | 同上                                  | `export_timeline` (`aaf`)             |
| EDL（Samplitude / Reaper）     | 同上                                  | `export_timeline` (`edl`)             |
| Adobe Audition（.sesx）        | 同上                                  | `export_timeline` (`sesx`)            |

既定の形式は `descript_studio` の UserValve `default_export_format` で変えられる。

> ⚠️ **タイムライン書き出しにメディアファイルは含まれない。** タイムライン / XML / EDL ファイルのみが出力される。編集先の NLE で元素材をリンクし直す必要がある。
>
> ⚠️ **ダウンロードリンクは期限付き**（`download_url_expires_at` まで）。チャット履歴には残らないので、その場でダウンロードすること。期限が切れたらもう一度エクスポートすればよい。

<details>
<summary>設計変更の経緯（当初の記述からの訂正）</summary>

当初この手順書には「FCPXML への書き出しは Descript App 側で行う。Public API にエンドポイントが存在しない」と記載していた。これは `docs/RequirementDefinition/DA/descript_api.json`（REST API 仕様）に `export/timeline` の schema もエンドポイントも定義されていないことに基づく判断だった。

しかし実機で `probe` を実行したところ、**MCP には `export_timeline` ツールが存在**し、`format: "fcp"` で .fcpxml を直接取得できることが判明した。BA 要件の「FCPXML 双方向連携」は MCP 経由で直接満たせる。

</details>

---

## トラブルシューティング

| 症状                                           | 原因と対処                                                                                                                              |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| ボタンが表示されない                           | `ENABLE_PLUGINS=true` か確認。Function が有効化（トグル ON）されているか確認。Action は**アシスタントの応答メッセージの下**に出る       |
| `Descript MCP が登録されていません`            | `mcp_server_id` Valve の値がステップ 3 の `info.id` と一致しているか確認                                                                |
| `Descript との連携が未認可です`                | ステップ 4 の OAuth 同意が未完了。＋ → Integrations → Tools から有効化する                                                              |
| `必要な Descript ツールを特定できませんでした` | ステップ 7 が未完了。probe を実行して Valve に転記する                                                                                  |
| 進捗表示が出ない / 入力ダイアログが出ない      | Redis 構成を確認（`WEBSOCKET_MANAGER=redis`）。複数レプリカで Redis が無いとイベントが届かない                                          |
| 処理が途中で止まる                             | `poll_timeout_sec`（既定 900 秒）を超えた可能性。Descript 側ではジョブが継続していることがある                                          |
| `Descript のクレジットが不足しています`        | Descript の残高を確認。**リトライしても解消しない**                                                                                     |
| Function を修正したのに反映されない            | Import From Link は自動同期ではない。**再インポート＋保存**が必要                                                                       |
| チャットのタイトルが `New Chat` に戻る         | Functions のバグ。`Chats.update_chat_by_id` に `title` を含めずに呼んでいる箇所がある。契約書 §5.3 を参照                               |
| 動画以外の添付（PDF 等）が RAG に載らない      | `descript_guard` の `file_handler = True` の副作用。削除はパイプライン単位で `body["files"]` を丸ごと消す。Filter のトグルを OFF にする |

### ログの確認

```bash
docker compose logs -f open-webui | grep -i "descript\|mcp\|function"
```

Function がロードに失敗すると、Open WebUI は**その Function を自動的に無効化する**（`utils/plugin.py:312`）。有効化したはずのトグルが OFF に戻っていたらロードエラーを疑う。

---

## 運用上の注意

- **リポジトリ側が正（source of truth）。** Workspace UI 上で直接編集して済ませない。UI で急ぎ直した場合は必ずリポジトリへ書き戻す
- **このリポジトリは public。** Functions のコードに API キー・MCP URL・Drive 名・プロジェクト名を直書きしない。環境依存値はすべて Valves に置く
- **共通ヘルパは 3 ファイルに重複展開されている。** 1 Function = 自己完結 1 ファイルという Open WebUI の制約による。修正するときは `docs/DetailedDesign/functions_contract.md`（正本）を先に直してから 3 ファイルへ反映する
- Descript の API / MCP 利用は**メディア時間と AI クレジットを消費する**。取込は媒体時間、Underlord 編集（Studio Sound / フィラー除去 / キャプション）は AI クレジットを使う

---

## 関連ドキュメント

| ファイル                                          | 内容                                                                          |
| ------------------------------------------------- | ----------------------------------------------------------------------------- |
| `docs/DetailedDesign/functions_contract.md`       | 3 ファイル共通の契約の**正本**。RPC・state スキーマ・エラーコード・共通ヘルパ |
| `docs/DetailedDesign/movie_flow.mmd`              | ユースケースのシーケンス（実装の一次仕様）                                    |
| `docs/RequirementDefinition/BA/`                  | 業務要件（実装対象の編集機能一覧）                                            |
| `docs/RequirementDefinition/DA/descript_api.json` | Descript の REST API 仕様（MCP の裏側の挙動を理解する参考）                   |
| `.env.example`                                    | 環境変数の全リストと解説                                                      |
