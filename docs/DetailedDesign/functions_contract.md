# Functions 共通契約（Descript ショート動画作成）

本書は `functions/` 配下の 3 つの Open WebUI Function が共有する契約の**正本**である。

| ファイル                       | function_id       | 種別                    | 責務                                           |
| ------------------------------ | ----------------- | ----------------------- | ---------------------------------------------- |
| `functions/descript_studio.py` | `descript_studio` | Action（マルチ 4 サブ） | UI 層。フォーム収集と Pipe への RPC            |
| `functions/descript_pipe.py`   | `descript_pipe`   | Pipe（単一）            | オーケストレーション層。MCP・ジョブ・LLM・状態 |
| `functions/descript_guard.py`  | `descript_guard`  | Filter                  | 前後処理。RAG 迂回・URL 恒久化・事前診断       |

> **1 Function = 自己完結した 1 ファイル。** Open WebUI に登録できるのは 1 ファイル分のコードだけで、リポジトリ内の相対 import は解決されない。本書 §5 の共通ヘルパは**各ファイルにインライン展開する**（コピー元は常に本書）。

---

## 1. Action → Pipe の RPC 契約

Action は `open_webui.utils.chat.generate_chat_completion` で Pipe モデルを起動する。`form_data` の `metadata` 以外の任意キーは Pipe の `body` に素通しされる（`backend/open_webui/functions.py:206`）ため、それを RPC 引数として使う。

### 1.1 リクエスト（Action が組み立てる `form_data`）

```python
{
    "model":    <valves.pipe_model_id>,      # 既定 "descript_pipe"
    "messages": [{"role": "user", "content": <人間可読の要約 1 行>}],
    "stream":   False,                       # ★ 必ず False
    "descript_op":   "<op 名>",              # §1.3 の一覧
    "descript_args": {...},                  # op ごとの引数
    "metadata": {
        "user_id":    __user__["id"],
        "chat_id":    body["chat_id"],
        "session_id": body["session_id"],
        "message_id": body["id"],            # ★ Action の body['id'] を再利用（新規 UUID 禁止）
        "task":       None,
        "files":      [],
        "tool_ids":   [],
    },
}
```

**`stream=False` が必須の理由**: `True` にすると `StreamingResponse` が返り（`functions.py:333`）、Action の同期 HTTP レスポンスとして JSON シリアライズできない。`False` なら plain dict が返る（`functions.py:334-350`）。

**`message_id` に `body['id']` を再利用する理由**: フロントは `history.messages[event.message_id]` が存在しないイベントを黙って捨てる（`src/lib/components/chat/Chat.svelte:962`）。新規 UUID を振ると `status`/`embeds` が描画されず、`input`/`confirmation` は `cb` が呼ばれないまま 300 秒でタイムアウトする（`backend/open_webui/socket/main.py:1119`）。

呼び出し方:

```python
res = await generate_chat_completion(
    __request__, form_data, _as_user_model(__user__), bypass_filter=self.valves.bypass_model_access
)
envelope = _unpack_pipe_response(res)   # §5.6
```

### 1.2 レスポンス（Pipe が返す封筒）

Pipe は**常に単一の JSON 文字列**を返し、それが `choices[0].message.content` に入る。Action は `_unpack_pipe_response()` で復号する。

```jsonc
// 成功
{ "ok": true,  "op": "list_projects", "data": { ... }, "state": { ... } }
// 失敗
{ "ok": false, "op": "agent_edit", "code": "JOB_FAILED",
  "message_ja": "編集ジョブが失敗しました。", "hint": "…", "data": { ... } }
```

`state` は保存後の `chat.chat["descript"]` のスナップショット（Action は表示にのみ使う）。

### 1.3 `descript_op` 一覧

| op                 | `descript_args`                                                                                                   | 成功時 `data`                                                                                | 備考                                                                                                                |
| ------------------ | ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `probe`            | `{}`                                                                                                              | `{"tools": [<tool_spec>...], "resolved": {<論理op>: <実ツール名 or null>}, "server": {...}}` | **依存ゼロ。最初に実装する。** MCP 疎通とツール名確定の唯一の手段                                                   |
| `list_projects`    | `{"name": str?}`                                                                                                  | `{"projects": [{"id","name","updated_at"?,"folder_path"?}]}`                                 | `name` は部分一致フィルタ                                                                                           |
| `get_project`      | `{"project_id": str}`                                                                                             | `{"project": {"id","name","media_files":{...},"compositions":[...]}}`                        |                                                                                                                     |
| `ensure_project`   | `{"name": str}`                                                                                                   | `{"project_id": str, "project_name": str, "created": bool}`                                  | 一覧を引いて同名があれば再利用、無ければ新規扱いにする                                                              |
| `import_media`     | `{"project_id": str?, "project_name": str?, "source_url": str?, "file_id": str?}`                                 | `{"project_id","project_url","media": {...}}`                                                | `source_url` / `file_id` はどちらか。**どちらを使うかは MCP スキーマ確定後に決定**（§7 未確定事項）                 |
| `agent_edit`       | `{"project_id": str, "composition_id": str?, "prompt": str, "conversation_id": str?}`                             | `{"agent_response": str, "project_changed": bool, "conversation_id": str?}`                  | `prompt` は Pipe が LLM で合成した最終プロンプト                                                                    |
| `publish`          | `{"project_id": str, "composition_id": str?, "media_type": str?, "resolution": str?}`                             | `{"share_url": str, "download_url": str?, "composition_id": str?}`                           |                                                                                                                     |
| `start_edit_loop`  | `{"project_id": str, "instruction": str, "style": str, "aspect": str, "duration_sec": int, "auto_confirm": bool}` | `{"revision": int, "share_url": str, "agent_response": str, "awaiting": "user"\|"done"}`     | プロンプト合成 → agent_edit → publish → プレビュー embed までを 1 周。`awaiting=="user"` なら embeds フォームで継続 |
| `resume_edit_loop` | `{"action": "revise"\|"confirm", "text": str?}`                                                                   | `start_edit_loop` と同じ                                                                     | embeds フォームからの再入時。`project_id` 等は state から復元                                                       |
| `job_status`       | `{"job_id": str}`                                                                                                 | `{"job_state": str, "result": {...}}`                                                        | 手動再開・デバッグ用                                                                                                |

すべての op は失敗時に §4 のエラーコードを返す。**例外を Action まで伝播させない**（Action 側は `try/except` で保険を張るが、正常系は封筒で受ける）。

### 1.4 Filter → Pipe の受け渡し契約（通常チャット経路）

Filter が走るのは「ユーザが Descript Pipe モデルを選んで通常チャットした時」だけである（`generate_chat_completion` を使う Action→Pipe 直接経路は `process_chat_payload` を通らない）。この経路で Filter の `inlet` が `body` に載せるキーを以下に定める。

| キー                 | 書き手       | 読み手                | 内容                                                                                                                                                                   |
| -------------------- | ------------ | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `descript_preflight` | Filter.inlet | **Pipe.pipe（必須）** | 事前診断の結果。`{"ok": false, "code": "MCP_OAUTH_REQUIRED", "message_ja": ..., "hint": ...}`。`ok` が false のとき **Pipe は MCP に接続せず、この封筒をそのまま返す** |
| `descript_media`     | Filter.inlet | **Pipe.pipe（必須）** | RAG から退避した動画添付のリスト。要素は `{"name": str, "url": str\|None, "file_id": str\|None, "content_type": str\|None}`。`import_media` の入力候補として使う       |

**Pipe 側の実装義務**:

```python
# ディスパッチの最初に置く
preflight = (body or {}).get("descript_preflight") or {}
if preflight and preflight.get("ok") is False:
    return _fail("preflight", preflight.get("code", "INTERNAL"),
                 preflight.get("message_ja", ""), preflight.get("hint", ""))
```

`descript_media` は `descript_op` が無い経路（＝通常チャット）で、`import_media` の `source_url` / `file_id` が未指定のときのフォールバック入力として参照する。

> ⚠️ `body` の独自キーが Pipe まで届くことは、Action→Pipe 直接経路については `functions.py:206`（`params = {'body': form_data}`）で確認済み。通常チャット経路（`process_chat_payload` 経由）については**未検証**。`process_chat_payload` が `form_data` のキーを落とさないことを A7 統合レビューで実機確認すること。落ちる場合は `__metadata__` 経由に切り替える。

### 1.5 進捗マーカー（予約。現状は未使用）

Filter の `stream` は `progress_marker_prefix`（既定 `U+2063 INVISIBLE SEPARATOR` + `DSC:`、UTF-8 で `e2 81 a3 44 53 43 3a`）で始まる行を本文から除去する機能を持つ。

**ただし現行の Pipe はマーカーを一切出力しない。** Pipe は単一の JSON 封筒文字列を返す設計であり、進捗は `__event_emitter__` の `status` イベントで直接送っているためである。したがってこの機能は現時点で**予約（デッドパス）**であり、以下の 2 つの目的で残している。

1. 将来 Pipe をストリーミング化したときの受け口
2. 内部制御文字列が誤って本文に混ざった場合の防御

**将来マーカーを使う場合の必須条件**: SSE チャンクは任意位置で分割されうるため、Filter はチャンクをまたぐマーカーを検出できない。**Pipe 側が「1 マーカー = 1 チャンク、前後を改行で囲む」形で送出すること**を前提とする。バッファリングは本文欠落のリスクが大きいため Filter 側では行わない。

### 1.6 通常チャットのインテント振り分け（`descript_op` が無い経路）

**なぜ Pipe 側で受けるのか。** Action はアシスタントメッセージのツールバーにしか描画されない（`utils/actions.py`）ため、**チャット開始時（まだ 1 通も無い状態）には出せない**。一方、新規チャット画面には Suggestion Prompts が並び（`src/lib/components/chat/Placeholder.svelte:257-261` が `info.meta.suggestion_prompts` → `default_prompt_suggestions` の順に採用）、**クリックすると即送信される**（`Chat.svelte:658-669`。ユーザ設定 `insertSuggestionPrompt` が on のときだけ挿入のみ）。送信先は選択中モデル＝Descript Pipe であり、**Pipe には `__event_call__` が渡る**（`functions.py:263-264`）ので、Action を介さずそのままフローに入れる。

したがって導線は次のようにする:

1. Workspace → Models → Descript Orchestrator → Prompt suggestions に短いコマンド句を登録する
2. 既定モデルを Descript Orchestrator にする
3. Pipe の `_dispatch` がその句を `descript_op` に振り分ける（本節）

> ⚠️ **部分一致で振り分けてはいけない。** 「冒頭をカットして**書き出して**」のような本物の編集指示が `export_timeline` に奪われる。
> **正規化後の文字列が「全体として」短いコマンド句に一致したときだけ**振り分け、それ以外は従来どおり `start_edit_loop`（自然文の編集指示）に落とす。
> 正規化は「前後空白の除去 → 小文字化 → 空白（半角/全角）の除去 → 末尾の句読点・記号の除去」。長さが `_CHAT_INTENT_MAX_LEN`（24）を超える文字列は**一切振り分けない**。

| 入力例（正規化後が全体一致）                              | 振り分け先 op     |
| --------------------------------------------------------- | ----------------- |
| 動画をアップロード / 取り込み / インポート / `upload`     | `import_media`    |
| 動画を編集 / 編集する / `edit`                            | `start_edit_loop` |
| タイムラインを書き出す / エクスポート / `export`          | `export_timeline` |
| プロジェクト一覧 / プロジェクトのリスト / `list projects` | `list_projects`   |
| 診断 / 接続確認 / `probe`                                 | `probe`           |

- `start_edit_loop` に振り分けたときは **`instruction` を空にする**。コマンド句そのもの（「動画を編集」）を編集指示として LLM に渡しても意味がないため、Valve / UserValves のスタイル既定に委ねる
- 引数が足りない場合は各 op が既存のエラーコードで返す（動画未添付なら `TOOL_ARGS_MISSING`、プロジェクト不在なら `NO_PROJECT`）。**振り分け側で先回りして検証しない**
- Valve `enable_chat_intents`（既定 `true`）で機能ごと無効化できる。誤爆したときに即座に従来動作へ戻せるようにするため

---

## 2. 論理操作 ↔ MCP ツール名

Descript MCP（`https://api.descript.com/v2/mcp`）は個々のツール名・引数スキーマを公開ドキュメントに載せていない。よって実行時ディスカバリを前提とする設計にしてある。

呼び出し側は**常に論理操作名だけを書く**:

```python
projects = await _mcp_call(client, specs_by_name, tool_map, "list_projects", {})
```

### 2.0 実測で確定したツール名（2026-08-02、`probe` 実行結果）

**以下は実機で `list_tool_specs()` を叩いて得た事実であり、推測ではない。** 全 12 ツール。

| 論理操作        | 実ツール名                 | 必須引数        | 自動解決     |
| --------------- | -------------------------- | --------------- | ------------ |
| `list_projects` | `list_projects`            | —               | ✓            |
| `get_project`   | `get_project`              | `project_id`    | ✓            |
| `import_media`  | `import_media`             | **`add_media`** | ✓            |
| `agent_edit`    | **`prompt_project_agent`** | `prompt`        | ✗ Valve 必須 |
| `publish`       | `publish_project`          | `project_id`    | ✓            |
| `job_status`    | **`wait_for_job`**         | `job_id`        | ✗ Valve 必須 |

論理操作に割り当てていない残り 6 ツール: `list_jobs` / `cancel_job` / `report_upload_status` / `list_folders` / `export_transcript` / **`export_timeline`**

**設定すべき Valve**（`descript_pipe`）:

```
tool_list_projects = list_projects
tool_get_project   = get_project
tool_import_media  = import_media
tool_agent_edit    = prompt_project_agent
tool_publish       = publish_project
tool_job_status    = wait_for_job
tool_resolution_mode = valve_only
```

### 2.0.1 引数スキーマの実測（重要な差分）

**① `import_media` — `add_media` は「表示名 → メディア定義」のマップ**

平坦な `source_url` / `file_id` では通らない。`tool_arg_map` はキー名の付け替えしかできないので、**構造変換はコード側で行う**。

```jsonc
// URL 取込
{"add_media": {"intro.mp4": {"url": "https://..."}},
 "project_name": "my-short",
 "add_compositions": [{"clips": [{"media": "intro.mp4"}]}]}

// 直接アップロード（レスポンスの upload_urls に PUT する）
{"add_media": {"intro.mp4": {"content_type": "video/mp4", "file_size": 12345678}}}
```

- **新規プロジェクトには `add_compositions` を渡す。** 渡さないと取り込んだメディアがタイムラインに載らない（ツール説明に「this is the expected default」と明記）
- **既存プロジェクトには `add_compositions` を渡さない。** 既存の編集を壊す。追記したい場合は `update_compositions`（今回は未使用）
- 直接アップロードのレスポンス `upload_urls` は `{メディアキー: {upload_url, asset_id, artifact_id}}`。`Content-Type: application/octet-stream` で PUT し、**宣言した `file_size` と実サイズが一致する必要がある**
- アップロードに失敗した場合は `report_upload_status`（`job_id` / `media_id` / `status`）で通知しないと、import ジョブがそのファイルを待ち続ける

> 🔴 **PUT のボディは「非同期」ジェネレータで渡すこと（実測 2026-08-03）。**
> `httpx.AsyncClient` の `content=` に**同期**ジェネレータを渡すと、httpx が同期ストリーム（`IteratorByteStream`）として包み、`AsyncClient._send_single_request` が
> `RuntimeError: Attempted to send an sync request with an AsyncClient instance.` を送出して**送信前に落ちる**（httpx 0.28.1 で確認）。
> ファイル読み出しは blocking I/O なので `asyncio.to_thread` に逃がす。170 MB クラスの動画で同期 read を直接回すとイベントループが止まる。
> 一方 **`Content-Length` を明示すれば `Transfer-Encoding: chunked` は付かない**（`Request._prepare` が「Content-Length があれば transfer-encoding を無視する」と実装済み。同 0.28.1 で確認）。署名付き URL への PUT はこれに依存しているので、ヘッダを外さないこと。

**② `publish_project` — `media_type` は `Video` / `Audio`（先頭大文字）**

`mp4` / `mp3` / `wav` のような拡張子表記では**通らない**。

- `media_type` 省略 かつ 映像なし → Audio として公開される
- `media_type: "Video"` を明示 かつ 映像なし → **422 で失敗する**
- 同一コンポジションを再度 publish すると**前回の share URL を再利用して内容を上書き**する。Video と Audio は別々の share URL になる

**③ `wait_for_job` — ポーリングではなくブロッキング待機**

- 既定で **300 秒** 待ち、進捗をストリームする
- `wait_seconds: 0` で即時リターン（＝従来のポーリング相当）
- ジョブ状態: `queued` / `running` / `stopped` / `cancelled`。**`stopped` は `result.status` を見る**（契約書の既存の記述と一致）
- 完了レスポンスに `project_url` が入る。コンポジション ID が判明していれば短縮 ID 付きで該当コンポジションを直接開ける
- `export_timeline` の完了結果には期限付き `result.download_url` と `result.download_url_expires_at` が入る

> **設計上の含意**: `_poll_job` は `wait_seconds` を明示的に渡すこと。既定のままだと 1 回の呼び出しで 300 秒ブロックし、Action→Pipe の HTTP がリバースプロキシに切られる。

**④ `export_timeline` — FCPXML を直接取得できる**

必須: `project_id`, `format`。`format` の選択肢:

| 値                | 出力                           |
| ----------------- | ------------------------------ |
| `fcp`             | **Final Cut Pro X（.fcpxml）** |
| `premiere`        | Premiere Pro XML               |
| `davinci_resolve` | DaVinci Resolve XML            |
| `aaf`             | Pro Tools / Logic（バイナリ）  |
| `edl`             | Samplitude / Reaper            |
| `sesx`            | Adobe Audition                 |

**メディアファイルは同梱されない**（タイムライン/XML/EDL ファイルのみ）。非同期で `job_id` を返すので `wait_for_job` で待ち、`result.download_url` を提示する。

> ⚠️ **設計の訂正**: 当初「FCPXML 書き出しは Descript の Public API に存在しないため Descript App に遷移してユーザが手動で行う」と結論していたが、**MCP には `export_timeline` が存在する**。BA 要件の「FCPXML 双方向連携」は MCP 経由で直接満たせる。UC3（タイムラインのエクスポート）はこれを使う。

**⑤ `export_transcript` — 字幕/文字起こしの書き出し**

必須: `project_id`, `format`。`format`: `txt` / `markdown` / `html` / `rtf` / **`srt`**。レスポンスに内容が直接入る（非同期ではない）。`srt` は字幕ファイルとして使える。

### 2.1 論理操作は 6 つ

`list_projects` / `get_project` / `import_media` / `agent_edit` / `publish` / `job_status`

加えて **`export_timeline`** を 7 番目の論理操作として追加する（§2.0.1 ④）。Valve 名は `tool_export_timeline`（既定 `export_timeline`）。

### 2.2 解決の 3 段フォールバック（`tool_resolution_mode` Valve で切替）

| モード           | 動作                                                                    |
| ---------------- | ----------------------------------------------------------------------- |
| `valve_only`     | Valve `tool_*` の値のみ使用。未設定なら `TOOL_UNRESOLVED`。**本番推奨** |
| `regex_only`     | Valve → §2.3 の正規表現スコアリング                                     |
| `regex_then_llm` | Valve → 正規表現 → LLM 選択（既定）                                     |

解決結果は `chat.chat["descript"]["tool_map"]` にキャッシュし、同一チャット内で再解決しない。

### 2.3 正規表現候補（優先順）

```python
_TOOL_PATTERNS = {
    "list_projects": [r"^list_?projects?$", r"list.*project", r"projects?_?list", r"^get_?projects?$", r"search.*project"],
    "get_project":   [r"^get_?project$", r"project.*(detail|info|metadata)", r"^describe_?project$", r"open.*project"],
    "import_media":  [r"import.*(media|file|video|url)", r"^upload", r"(create|add).*(composition|media)", r"add.*file"],
    "agent_edit":    [r"agent.*edit", r"^agent$", r"underlord", r"^edit$", r"apply.*edit", r"edit.*project"],
    "publish":       [r"^publish", r"export.*(video|media|link)", r"^share", r"render"],
    "job_status":    [r"job.*(status|state)", r"^get_?job$", r"^poll", r"task.*status"],
}
```

スコアリング: パターン順に前方優先で加点し、`description` に論理操作の日本語/英語キーワードが含まれればさらに加点。最高得点が 1 件に定まらなければ次の段へ落とす。

### 2.4 引数名の写像

Valve `tool_arg_map`（JSON 文字列）で論理引数名 → 実引数名を写像する。

```json
{
  "agent_edit": { "project_id": "projectId", "prompt": "instruction" },
  "publish": { "media_type": "format" }
}
```

`_coerce_args()`（§5.4）が `spec["parameters"]` の `properties` / `required` と突き合わせ、
① Valve の写像を適用 → ② 大文字小文字・アンダースコア差を吸収して再探索 → ③ それでも該当しないキーは**落とす** → ④ `required` の不足を返す。

---

## 3. 状態スキーマ（`chat.chat["descript"]`）

### 3.1 保存先の選定理由

`Chat.meta` 列には汎用 writer が存在しない（`backend/open_webui/models/chats.py` で meta を書くのは tags 専用の `:729-742` のみ）。`Chat.variables` は通常チャット完了時に `main.py:1420` が列ごと置換するため使えない。

したがって **`chat.chat` JSON blob のトップレベル独自キー `descript`** に置く。フロントは `models` / `messages` / `history` / `params` / `files` しか送らず、`routers/chats.py:1358` の `{**chat.chat, **form_data.chat}` は DB を読み直してからマージするため、独自キーは保存され続ける。

> ⚠️ **`update_chat_by_id` の罠**: `chat_item.title = chat['title'] if 'title' in chat else 'New Chat'`（`models/chats.py:608`）。blob に `title` を含めずに渡すと**チャットタイトルが `New Chat` にリセットされる**。§5.3 の `_save_state()` は必ず `title` を保持する。

### 3.2 スキーマ

```jsonc
{
  "descript": {
    "v": 1, // スキーマバージョン
    "project_id": "…",
    "project_name": "…",
    "composition_id": "…",
    "conversation_id": "…", // Descript agent の会話継続用
    "last_job_id": "…",
    "share_url": "…",
    "revision": 3, // 編集ループの周回数
    "instruction": "…", // 直近のユーザ編集指示（原文）
    "edit_prompt": "…", // LLM が合成した最終プロンプト
    "awaiting": "user", // "user" | "done" | null
    "style": "jet_cut",
    "aspect": "9:16",
    "duration_sec": 60,
    "tool_map": { "list_projects": "…", "agent_edit": "…" },
    "history": [
      {
        "ts": 1767225600,
        "op": "agent_edit",
        "job": "…",
        "result": "success",
        "note": "…",
      },
    ],
  },
}
```

### 3.3 書き込み規律

- **書き手は Pipe のみ。Action と Filter は読み取り専用**（同時書き込み競合の回避）。
  - 例外: Filter の `outlet` は `history` への append のみ許可（Pipe の実行が終わった後に走るため競合しない）。
- `history` は末尾 50 件で切り詰める。
- `_save_state()` は read-modify-write。`touch=False` を必ず指定し、チャットの更新日時を汚さない。

---

## 4. エラーコード表

Pipe は失敗時に `{"ok": false, "code": ..., "message_ja": ..., "hint": ..., "data": ...}` を返す。Action は `status(done=True)` ＋ `notification` ＋ メッセージ本文の 3 系統に展開する。

| code                 | 検知条件                                                                                      | `message_ja`                                   | リトライ                     |
| -------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------- | ---------------------------- |
| `MCP_NOT_FOUND`      | `Config.get('tool_server.connections')` に `type=='mcp'` かつ `info.id==server_id` が無い     | Descript MCP が登録されていません。            | 不可                         |
| `MCP_FORBIDDEN`      | 接続は存在するが `connect_mcp_server` が `None` を返す（`has_connection_access` 失敗）        | Descript MCP へのアクセス権がありません。      | 不可                         |
| `MCP_OAUTH_REQUIRED` | `oauth_client_manager.get_oauth_token(user.id, f'mcp:{server_id}')` が `None`／connect が 401 | Descript との連携が未認可です。                | 不可（下記固定文言）         |
| `MCP_CONNECT_FAILED` | `connect` が例外（`MCP_INITIALIZE_TIMEOUT` 既定 10 秒）                                       | Descript MCP に接続できませんでした。          | `mcp_connect_retries` 回     |
| `TOOL_UNRESOLVED`    | `_resolve_tools` が該当なし                                                                   | 必要な Descript ツールを特定できませんでした。 | 不可（probe 表を自動表示）   |
| `TOOL_ARGS_MISSING`  | `_coerce_args` が `required` 不足を返す                                                       | ツールの必須引数が不足しています。             | `input` で 1 回だけ追質問    |
| `JOB_FAILED`         | `job_state=='stopped'` かつ `result.status=='error'`                                          | 処理が失敗しました。                           | 不可                         |
| `JOB_PARTIAL`        | `result.status=='partial'`                                                                    | 一部のメディアの処理に失敗しました。           | 継続（warning 通知）         |
| `JOB_CANCELLED`      | `job_state=='cancelled'`                                                                      | 処理がキャンセルされました。                   | 不可                         |
| `JOB_TIMEOUT`        | `poll_timeout_sec` 超過                                                                       | 処理が既定時間内に完了しませんでした。         | job_id を state に保存し継続 |
| `QUOTA_EXCEEDED`     | エラー本文に `402` / `credit` / `quota` / `insufficient`                                      | Descript のクレジットが不足しています。        | **リトライしない**           |
| `RATE_LIMITED`       | エラー本文に `429` / `rate limit` / `too many`                                                | Descript のレート制限に達しました。            | 指数バックオフ最大 3 回      |
| `UI_DISCONNECTED`    | `__event_call__` が `{"error": ...}` を返す                                                   | 画面との接続が切れました。                     | 進行中ジョブは中断しない     |
| `USER_CANCELLED`     | `__event_call__` が `False` を返す                                                            | 中止しました。                                 | —                            |
| `NO_PROJECT`         | `list_projects` が 0 件                                                                       | Descript にプロジェクトがありません。          | —                            |
| `INTERNAL`           | 上記以外の例外                                                                                | 内部エラーが発生しました。                     | —                            |

### 4.1 `MCP_OAUTH_REQUIRED` の固定文言（そのまま使う）

```
Descript との連携が未認可です。チャット入力欄の ＋ ボタン → Integrations → Tools から
Descript を一度有効化し、ブラウザに表示される同意画面を完了してください。
（一度完了すれば、以降のトークン更新は自動で行われます）
```

**自動復帰は不可**。チャット補完リクエストの最中にブラウザリダイレクトを起こせないため（Open WebUI 公式ドキュメント「OAuth 2.1 Tools Cannot Be Set as Default Tools」）。同じ理由で、この MCP を**モデルのデフォルトツールに設定してはいけない**。

### 4.2 `TOOL_UNRESOLVED` の動線

`data` に `{"tools": [<tool_spec>...], "op": "<未解決の論理操作>"}` を載せ、Action 側で `_TOOL_TABLE` テンプレート（§6.2）を embeds 表示する。ユーザ／管理者は表から実ツール名を読み取り、対応する `tool_*` Valve に貼る。**これが実装ブロックを解除する中核動線。**

---

## 5. 共通ヘルパ正本

以下を **3 ファイルすべてに同一内容でインライン展開**する。Filter は §5.1〜5.3 と 5.9・5.10 のみ使用する。

### 5.0 import 規約

```python
import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from open_webui.models.chats import Chats
from open_webui.models.users import UserModel
```

> `replace_imports()`（`backend/open_webui/utils/plugin.py:187-203`）は `from utils` / `from apps` / `from main` / `from config` を**素の文字列置換**で書き換える。`from open_webui.…` と最初から書けば安全。逆に `from utils_of_mine import x` のような文字列も壊されるため、この 4 語で始まる import を書かないこと。

### 5.1 定数・例外

```python
_STATE_KEY = "descript"
_STATE_VERSION = 1
_HISTORY_MAX = 50
_LOGICAL_OPS = ("list_projects", "get_project", "import_media", "agent_edit", "publish", "job_status")


class DescriptError(Exception):
    """Pipe 内部で送出し、封筒に変換して返すためのエラー。"""

    def __init__(self, code: str, message_ja: str, hint: str = "", data: Any = None):
        super().__init__(f"{code}: {message_ja}")
        self.code = code
        self.message_ja = message_ja
        self.hint = hint
        self.data = data

    def envelope(self, op: str = "") -> dict:
        return {
            "ok": False,
            "op": op,
            "code": self.code,
            "message_ja": self.message_ja,
            "hint": self.hint,
            "data": self.data,
        }
```

### 5.2 小物

```python
def _pick(obj: Any, *keys: str, default: Any = None) -> Any:
    """dict から最初に見つかった非 None の値を返す。MCP の戻り値のキー揺れ吸収用。"""
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return default


def _norm_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _esc(text: Any) -> str:
    """HTML テンプレートに差し込む前のエスケープ。"""
    return (
        str("" if text is None else text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render(tpl: str, **kwargs: Any) -> str:
    """{{key}} を置換するだけの軽量テンプレート。

    str.format() を使わないのは、テンプレート中の CSS の { } を
    すべてエスケープする必要が生じて保守不能になるため。
    """
    out = tpl
    for key, value in kwargs.items():
        out = out.replace("{{" + key + "}}", "" if value is None else str(value))
    return out


def _as_user_model(user: Any) -> UserModel:
    """Action/Pipe の __user__（dict）を UserModel に変換する。

    connect_mcp_server / generate_chat_completion は UserModel を要求する。
    'valves' は UserValves の pydantic インスタンスなので必ず除去する。
    """
    if isinstance(user, UserModel):
        return user
    return UserModel(**{k: v for k, v in dict(user or {}).items() if k != "valves"})
```

### 5.3 状態の読み書き

```python
async def _load_state(chat_id: Optional[str]) -> dict:
    if not chat_id:
        return {}
    try:
        chat = await Chats.get_chat_by_id(chat_id)
    except Exception:
        return {}
    if chat is None:
        return {}
    state = (chat.chat or {}).get(_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


async def _save_state(chat_id: Optional[str], patch: dict) -> dict:
    """chat.chat['descript'] にパッチをマージして保存し、マージ後の state を返す。

    update_chat_by_id は blob に 'title' が無いとチャットタイトルを
    'New Chat' にリセットする（models/chats.py:608）ため、必ず補う。
    """
    if not chat_id:
        return dict(patch or {})
    try:
        chat = await Chats.get_chat_by_id(chat_id)
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


async def _append_history(chat_id: Optional[str], entry: dict) -> None:
    state = await _load_state(chat_id)
    history = list(state.get("history") or [])
    history.append({"ts": int(time.time()), **entry})
    await _save_state(chat_id, {"history": history})
```

### 5.4 引数整形

```python
def _coerce_args(spec: dict, args: dict, arg_map: Optional[dict] = None) -> tuple[dict, list]:
    """論理引数名を実引数名に写像し、スキーマに無いキーを落とす。

    戻り値は (整形済み引数, 不足している required キー)。
    spec['parameters'] が空（スキーマ不明）の場合は素通しする。
    """
    arg_map = arg_map or {}
    schema = (spec or {}).get("parameters") or {}
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])

    normalized = {_norm_key(k): k for k in props}
    out = {}
    for key, value in (args or {}).items():
        if value is None:
            continue
        name = arg_map.get(key, key)
        if props:
            if name not in props:
                match = normalized.get(_norm_key(name))
                if match is None:
                    continue  # スキーマに無い引数は送らない
                name = match
        out[name] = value

    missing = [r for r in required if r not in out]
    return out, missing
```

### 5.5 MCP 呼び出し

> ⚠️ **`call_tool` は `structuredContent` を返さない。** `MCPClient.call_tool` は `CallToolResult.content` だけを返し、構造化出力は捨てる（`utils/mcp/client.py:117-123`。コード中にも `# TODO: handle outputSchema if needed` とある）。
> したがって **text ブロックから JSON を取り出すのが唯一の経路**であり、ここが弱いと全操作が静かに壊れる。

> 🔴 **実測（2026-08-03）— text ブロックは「JSON + 人間向けの案内文」で返ってくる。**
> `import_media` の応答は `type=str size=1159`、中身は `{"job_id": …, "project_id": …, "project_url": …}` で始まるが、**JSON のあとに続きがあるため全体では `json.loads` が失敗**する。
> 旧実装は失敗時に連結文字列をそのまま返していたため、`_pick(raw, "upload_urls")` が `{}` になり `JOB_FAILED`（アップロード先 URL が返らない）になっていた。**実体を PUT しないので Descript 側には中身が空のメディアだけが残る。**
> これは `import_media` 固有ではなく**全操作に効く欠陥**で、`list_projects` も str のまま返るため「一覧が常に空 → 毎回新規プロジェクト扱い」も同じ原因。
> よって「全体が JSON でなければ、**最初に現れる均衡した JSON 値を切り出す**」ところまでを正本とする。

````python
def _json_from_text(text: str) -> tuple:
    """テキストから JSON 値を取り出す。戻り値は (値, 取り出し方)。

    取り出せなければ (None, "")。取り出し方は "whole" / "fence" / "embedded"。
    MCP サーバは JSON の前後に人間向けの説明文を付けることがあるため、
    全体 parse だけに頼らない（§5.5 冒頭の実測）。
    """
    body = (text or "").strip()
    if not body:
        return None, ""
    try:
        return json.loads(body), "whole"
    except Exception:
        pass

    # ```json … ``` で囲んで返す実装
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fence:
        try:
            return json.loads(fence.group(1).strip()), "fence"
        except Exception:
            pass

    # 前後に説明文が付いている実装。raw_decode は「値の直後で終わらない」ことを
    # 許すので、最初の { または [ から均衡する位置までを 1 値として取り出せる。
    decoder = json.JSONDecoder()
    for index, char in enumerate(body):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(body, index)
        except Exception:
            continue
        return value, "embedded"
    return None, ""


def _unwrap_mcp(raw: Any) -> Any:
    """MCPClient.call_tool の戻り値を正規化する。

    call_tool は CallToolResult.content（コンテンツブロックの配列）を返す
    （utils/mcp/client.py:117-123）。structuredContent は捨てられるため、
    text ブロックから JSON を取り出すのが唯一の経路になる。
    """
    if raw is None or isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        # 文字列で返す実装もありうる。JSON なら開いておく。
        value, _mode = _json_from_text(raw)
        return raw if value is None else value
    if not isinstance(raw, list):
        return raw

    texts = []
    for block in raw:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource.get("text"), str):
                texts.append(resource["text"])

    if not texts:
        return raw
    joined = "\n".join(texts).strip()
    value, mode = _json_from_text(joined)
    if value is None:
        # JSON がまったく無い＝本当に人間向けの文章だけ。文字列として返す。
        _log_warn("mcp.unwrap_text_only", length=len(joined), head=joined[:200])
        return joined
    if mode != "whole":
        # サーバが JSON に説明文を混ぜている。動作はするが取りこぼしの温床なので残す。
        _log_warn("mcp.unwrap_mixed", mode=mode, length=len(joined), head=joined[:200])
    return value


def _classify_mcp_error(exc: Exception, tool_name: str) -> DescriptError:
    text = str(exc).lower()
    if any(t in text for t in ("402", "credit", "quota", "insufficient", "payment")):
        return DescriptError(
            "QUOTA_EXCEEDED",
            "Descript のクレジットまたはメディア時間が不足しています。",
            "Descript の残高をご確認ください。",
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    if any(t in text for t in ("429", "rate limit", "too many")):
        return DescriptError(
            "RATE_LIMITED",
            "Descript のレート制限に達しました。",
            "しばらく待ってから再実行してください。",
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    if any(t in text for t in ("401", "unauthorized", "invalid_token", "expired")):
        return DescriptError(
            "MCP_OAUTH_REQUIRED",
            "Descript との連携が未認可です。",
            _OAUTH_HINT,
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    if any(t in text for t in ("403", "forbidden")):
        return DescriptError(
            "MCP_FORBIDDEN",
            "Descript の操作権限がありません。",
            "対象 Drive での編集権限をご確認ください。",
            {"tool": tool_name, "raw": str(exc)[:800]},
        )
    return DescriptError(
        "JOB_FAILED",
        f"Descript ツール '{tool_name}' の実行に失敗しました。",
        "",
        {"tool": tool_name, "raw": str(exc)[:800]},
    )


async def _mcp_call(client, specs_by_name: dict, tool_map: dict, op: str,
                    args: dict, arg_maps: Optional[dict] = None) -> Any:
    """論理操作名で MCP ツールを呼ぶ。呼び出し側は実ツール名を意識しない。"""
    name = (tool_map or {}).get(op)
    if not name:
        raise DescriptError(
            "TOOL_UNRESOLVED",
            f"操作 '{op}' に対応する Descript MCP ツールを特定できませんでした。",
            "「Descript MCP 診断」アクションでツール名を確認し、対応する Valve に設定してください。",
            {"op": op, "tools": list(specs_by_name.values())},
        )

    spec = specs_by_name.get(name) or {}
    payload, missing = _coerce_args(spec, args, (arg_maps or {}).get(op))
    if missing:
        raise DescriptError(
            "TOOL_ARGS_MISSING",
            f"ツール '{name}' の必須引数が不足しています: {', '.join(missing)}",
            "tool_arg_map Valve で引数名の対応を設定してください。",
            {"tool": name, "missing": missing, "spec": spec},
        )

    try:
        raw = await client.call_tool(name, payload)
    except DescriptError:
        raise
    except Exception as exc:
        raise _classify_mcp_error(exc, name) from exc

    return _unwrap_mcp(raw)
````

### 5.6 封筒の詰め／開け

```python
def _ok(op: str, data: Any = None, state: Optional[dict] = None) -> str:
    return json.dumps(
        {"ok": True, "op": op, "data": data if data is not None else {}, "state": state or {}},
        ensure_ascii=False,
    )


def _fail(op: str, code: str, message_ja: str, hint: str = "", data: Any = None) -> str:
    return json.dumps(
        {"ok": False, "op": op, "code": code, "message_ja": message_ja, "hint": hint, "data": data},
        ensure_ascii=False,
    )


def _unpack_pipe_response(res: Any) -> dict:
    """generate_chat_completion(stream=False) の戻り値から封筒を取り出す。"""
    if isinstance(res, str):
        content = res
    elif isinstance(res, dict):
        try:
            content = res["choices"][0]["message"]["content"]
        except Exception:
            content = json.dumps(res, ensure_ascii=False)
    else:
        content = str(res)

    try:
        envelope = json.loads(content)
    except Exception:
        return {
            "ok": False,
            "op": "",
            "code": "INTERNAL",
            "message_ja": "オーケストレータの応答を解釈できませんでした。",
            "hint": "",
            "data": {"raw": str(content)[:2000]},
        }

    if not isinstance(envelope, dict) or "ok" not in envelope:
        return {
            "ok": False,
            "op": "",
            "code": "INTERNAL",
            "message_ja": "オーケストレータの応答形式が不正です。",
            "hint": "",
            "data": {"raw": envelope},
        }
    return envelope
```

### 5.7 `__event_call__` の戻り値判定（Action 専用）

```python
_OAUTH_HINT = (
    "チャット入力欄の ＋ ボタン → Integrations → Tools から Descript を一度有効化し、"
    "ブラウザに表示される同意画面を完了してください。"
    "（一度完了すれば、以降のトークン更新は自動で行われます）"
)


def _form_result(value: Any) -> tuple[bool, Any, Optional[dict]]:
    """__event_call__ の戻り値を (成功か, 値, エラー封筒) に分解する。

    ユーザキャンセル -> False（Chat.svelte:3758-3769）
    タイムアウト/切断 -> {'error': ...}（socket/main.py:1119-1127）
    """
    if value is False or value is None:
        return False, None, {
            "ok": False, "code": "USER_CANCELLED",
            "message_ja": "中止しました。", "hint": "", "data": None,
        }
    if isinstance(value, dict) and "error" in value:
        return False, None, {
            "ok": False, "code": "UI_DISCONNECTED",
            "message_ja": "画面との接続が切れたため入力を受け取れませんでした。",
            "hint": "もう一度お試しください。", "data": {"raw": value},
        }
    return True, value, None
```

### 5.8 ジョブポーリング（Pipe 専用）

```python
async def _poll_job(client, specs_by_name: dict, tool_map: dict, arg_maps: dict,
                    job_id: str, emit_status, valves) -> dict:
    """Descript の非同期ジョブを完了まで追跡する。

    job_state は queued -> running -> stopped (+ cancelled)。
    stopped は「終わった」だけで成功ではない。必ず result.status を見る。
    """
    started = time.monotonic()
    interval = float(valves.poll_interval_sec)
    ticks = 0

    while True:
        raw = await _mcp_call(client, specs_by_name, tool_map, "job_status", {"job_id": job_id}, arg_maps)
        state = str(_pick(raw, "job_state", "state", "status", default="") or "").lower()
        result = _pick(raw, "result", default={}) or {}

        if state in ("stopped", "completed", "succeeded", "success", "finished"):
            status = str(_pick(result, "status", "result_status", default="") or "").lower()
            if status == "success":
                return {"ok": True, "partial": False, "result": result}
            if status == "partial":
                return {"ok": True, "partial": True, "result": result}
            if status == "":
                # result を返さない実装向けのフォールバック
                return {"ok": True, "partial": False, "result": result}
            raise DescriptError(
                "JOB_FAILED",
                str(_pick(result, "error_message", "message", default="処理が失敗しました。")),
                "",
                {"job_id": job_id, "result": result},
            )

        if state in ("cancelled", "canceled"):
            raise DescriptError("JOB_CANCELLED", "処理がキャンセルされました。", "", {"job_id": job_id})

        ticks += 1
        elapsed = int(time.monotonic() - started)
        if ticks % max(1, int(valves.poll_status_every_n)) == 0:
            progress = _pick(raw, "progress", default={}) or {}
            label = _pick(progress, "label", default="処理中")
            percent = _pick(progress, "percent")
            suffix = f" {percent}%" if percent is not None else ""
            await emit_status(f"{label}{suffix}（{elapsed} 秒経過 / job {str(job_id)[:8]}）")

        if time.monotonic() - started > float(valves.poll_timeout_sec):
            raise DescriptError(
                "JOB_TIMEOUT",
                f"処理が {int(valves.poll_timeout_sec)} 秒以内に完了しませんでした。",
                "Descript 側では処理が継続している可能性があります。",
                {"job_id": job_id},
            )

        await asyncio.sleep(interval)
        interval = min(interval * 1.5, float(valves.poll_backoff_max_sec))
```

### 5.9 署名付き URL の秘匿（Filter・Pipe 共用）

> ⚠️ **`outlet` はチャット履歴の保存内容を不可逆に書き換える。** 誤爆すると無関係な URL が壊れ、元に戻せない。
> したがって検出条件は「**AWS SigV4 / GCS の署名パラメータを持つ**」ことに限定する。
> 汎用的な `token=` や `Expires=` 単独は**マッチさせない**（`?token=` を使う社内ツールのリンクなどを巻き込むため）。

```python
# 署名付き URL の検出。
# ⚠️ 意図的に「AWS SigV4 / GCS 署名の固有パラメータ」だけに絞っている。
#    汎用的な token= / Expires= を入れると、Descript と無関係な URL まで
#    outlet で書き換えてしまい、チャット履歴が不可逆に壊れる。
_SIGNED_URL_RE = re.compile(
    r"https?://[^\s\"'<>)\]]+?[?&]"
    r"(?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|X-Goog-Signature|GoogleAccessId)"
    r"=[^\s\"'<>)\]]*",
    re.IGNORECASE,
)

_SIGNED_URL_PLACEHOLDER = "（ダウンロードリンクは期限切れのため非表示）"


def _redact_signed_urls(text: str, replacement: str = "") -> str:
    """期限付き署名 URL を除去/置換する。

    Descript の download_url は署名付き・期限付きなので、チャット履歴に
    残すと後日 403 になる。share_url など恒久リンクに差し替える。

    replacement に恒久 URL（share_url）を渡せばリンクとして生き続ける。
    空なら注記文言に置き換える。
    """
    if not text or not isinstance(text, str):
        return text
    return _SIGNED_URL_RE.sub(replacement or _SIGNED_URL_PLACEHOLDER, text)
```

**追加の防御**: Filter の `outlet` で本文を書き換える前に、`chat.chat["descript"]` が存在するチャットかどうかを確認すること。Descript を使っていないチャットの履歴には一切触れない。

> ⚠️ **未確定 — 実機で 1 回だけ確認すること**
> このパターンは AWS SigV4 と GCS の署名 URL を前提にしている。実際の SigV4 presigned URL は必ず `X-Amz-Signature` を含むため `X-Amz-Algorithm` / `X-Amz-Date` 単独を拾わなくても実害はない。
> ただし **Descript の `download_url` がこのどちらでもない独自の署名方式だった場合、マッチせず期限切れ URL が履歴に残る**（後日 403 になる）。
> E2E で `publish` を 1 回実行し、返ってきた `download_url` のクエリパラメータを確認すること。該当しなければパターンにその署名パラメータ名を追加する。
> 誤爆側（過剰マッチ）より取りこぼし側（過小マッチ）のほうが被害が小さいため、意図的にこの方向に倒している。

### 5.10 ロギング（3 ファイル共用）

Functions は Open WebUI のプロセス内で `exec` されるため、**標準ライブラリの `logging` にそのまま乗る**。専用の初期化は不要で、してはいけない。

> ⚠️ **`SRC_LOG_LEVELS` を参照しないこと。** v0.11.0 の `backend/open_webui/env.py:127` では `SRC_LOG_LEVELS = {}`（"Legacy variable, do not remove"）であり、旧来の記事にある `log.setLevel(SRC_LOG_LEVELS["MAIN"])` は **`KeyError` で Function のロードごと失敗する**。
> 現行の作法はロガーを取るだけ。レベルは `GLOBAL_LOG_LEVEL` が `logging.basicConfig(..., force=True)` で一括設定する（`env.py:107-118`）。

- ロガー名は `logging.getLogger(__name__)` で足りる。Function は `types.ModuleType(f"function_{function_id}")` として `exec` されるため（`backend/open_webui/utils/plugin.py:276`）、`__name__` は `function_descript_pipe` のように **Function ID がそのまま入る**。grep 対象として十分
- **Uvicorn Workers で複数プロセス / 複数レプリカに分かれる**ため、`chat_id` と `op` が無い行はどのリクエストのものか追えない。イベント名 + `key=value` の 1 行 1 イベント形式に固定する
- 秘匿情報は §11 の規律をログにも適用する。**Valve の値・アクセストークン・ファイル本体を載せない。** 署名 URL は `_redact_signed_urls`（§5.9）を通す
- MCP の応答は**型とキーだけ**を DEBUG で出す。本文をそのまま流すとチャンクごとに肥大し、署名 URL の漏洩経路にもなる

```python
_LOG_VALUE_MAX = 300

# Function は function_<function_id> という名前のモジュールとして exec される
# （backend/open_webui/utils/plugin.py:276）ので、__name__ がそのまま識別子になる。
_LOGGER = logging.getLogger(__name__)


def _log(level: int, event: str, **fields: Any) -> None:
    """1 行 1 イベントの構造化ログを出す。

    Open WebUI は stdlib logging を GLOBAL_LOG_LEVEL で一括設定する
    （backend/open_webui/env.py:107-118）。v0.11.0 の SRC_LOG_LEVELS は
    空の互換用変数なので参照してはいけない（KeyError になる）。

    Uvicorn Workers で複数プロセスに分かれるため、呼び出し側は chat_id と
    op を必ず渡すこと。値は署名 URL を伏せ、長すぎるものは切り詰める。
    ⚠️ Valve の値・アクセストークン・ファイル本体は載せない（§11）。
    """
    if not _LOGGER.isEnabledFor(level):
        return
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        text = _redact_signed_urls(str(value)).replace("\n", " ")
        if len(text) > _LOG_VALUE_MAX:
            text = text[:_LOG_VALUE_MAX] + "…"
        parts.append(f"{key}={text}")
    _LOGGER.log(level, "descript %s%s", event, (" " + " ".join(parts)) if parts else "")


def _log_debug(event: str, **fields: Any) -> None:
    _log(logging.DEBUG, event, **fields)


def _log_info(event: str, **fields: Any) -> None:
    _log(logging.INFO, event, **fields)


def _log_warn(event: str, **fields: Any) -> None:
    _log(logging.WARNING, event, **fields)


def _log_error(event: str, **fields: Any) -> None:
    _log(logging.ERROR, event, **fields)


def _apply_log_level(level: Any) -> None:
    """Valve のレベルをこの Function のロガーにだけ適用する。

    GLOBAL_LOG_LEVEL を上げると Open WebUI 全体が饒舌になるため、
    Function 単位で切り替えられるようにする。未知の値は無視する。
    getLevelNamesMapping は Python 3.11+（本プロジェクトの下限）。
    """
    name = str(level or "").strip().upper()
    if name in logging.getLevelNamesMapping():
        _LOGGER.setLevel(name)


def _ms(started: float) -> int:
    """time.monotonic() の起点からの経過ミリ秒。"""
    return int((time.monotonic() - started) * 1000)
```

**Valve**: 3 ファイルとも `log_level`（既定 `INFO`）を持つ。

> ⚠️ **`_apply_log_level` を `__init__` で呼んではいけない（呼んでも効かない）。**
> Open WebUI は**呼び出しのたびに `function_module.valves` をインスタンスごと差し替える**（Pipe: `functions.py:61-66` / Action: `utils/actions.py:87` / Filter: `utils/filter.py:113-119`）。
> `__init__` が読めるのは常に既定値であり、UI で `log_level` を変えても反映されない。
> **各エントリポイントの先頭**（`Pipe.pipe` / `Action.action` / `Filter.inlet` / `Filter.outlet`）で呼ぶこと。`Filter.stream` はチャンクごとに呼ばれるため対象外（同一リクエストでは先に `inlet` が通る）。
> これは `self.valves` を読むすべての処理に当てはまる性質であって、ロギング固有の話ではない。

**レベルの使い分け**:

| レベル    | 出すもの                                                                         |
| --------- | -------------------------------------------------------------------------------- |
| `INFO`    | 進捗の節目（Action 起動 / RPC 開始・終了 / MCP ツール呼び出し / ジョブ状態遷移） |
| `DEBUG`   | 引数キー一覧、MCP 応答の型とキー、フォームの往復                                 |
| `WARNING` | 想定外だが処理を続けられる分岐（`upload_urls` 欠落、部分失敗、ツール未解決）     |
| `ERROR`   | 封筒 `ok=False` として返す直前の 1 回だけ。同じ失敗を二重に出さない              |

**イベント名は固定文字列**にする（`pipe.start` / `mcp.call` / `mcp.result` / `job.tick` / `import.upload_urls` / `action.start` / `rpc.done` / `filter.inlet` など）。可変文字列を混ぜると grep できなくなる。

---

## 6. HTML テンプレート正本

すべて `embeds` イベントで送る。**iframe は sandbox（`allow-same-origin` は既定 off）なので、高さ報告スクリプトが無いとコンテンツが切れる**（`src/lib/components/common/FullHeightIframe.svelte:161-163`）。

送信は必ず `replace: True` を明示し、こちらで全件を渡すこと。理由: backend は非 replace 時に**保存側だけ既存 embeds に追記**する（`backend/open_webui/socket/main.py:1036-1054`）一方、フロントは全置換する（`Chat.svelte:1007`）ため、DB と表示がズレる。

```python
await __event_emitter__({"type": "embeds", "data": {"embeds": [html], "replace": True}})
```

🔴 **embeds を送ったら、続けて最下部へのピン留めを送ること（§6.0.1）。** 送らないとチャットが最下部に戻らない。

### 6.0 共通フッタ（全テンプレートの末尾に入れる）

🔴 **高さの測り方を変えてはいけない。iframe 自身の高さに依存する値で測ると無限に伸びる。**

`document.documentElement.scrollHeight` は**ビューポート高（＝親が設定した iframe の高さ）を下回らない**。親はその値を iframe の高さに設定するので、報告値に定数を足すと

```
親が高さを H にする → 中の scrollHeight が H になる → H + 24 を報告 → 親が H + 24 にする → …
```

と 1 サイクルごとに +24px 増え続ける。ResizeObserver は毎フレーム発火するので **毎秒約 1,200px** で伸びる。実測（本番のチャットを Playwright で観測）: iframe の高さ 45,008px、増加率 1,196 px/秒、`scrollTop` は 189 のまま `scrollHeight` だけが伸び続ける。これが「チャットが最下部に行かず徐々に上へずれる」の正体で、**§6.0.1 のピン留めでは直せない**（最下部が毎秒 1,200px 逃げていくため）。

守ること:

1. **測るのは `document.body` の内容高**（`getBoundingClientRect().height`）。body の高さは iframe の高さに依存しないので閉じている。`documentElement.scrollHeight` は使わない。
2. **報告値に定数を足さない。** 測定値へ入力が戻る系に定数を足すと必ず発散する。余白が要るなら CSS 側（`.wrap` の padding）で確保する。
3. **前回と同じ値なら送らない**（2px 未満の差は無視）。残ったループを断ち切る保険。
4. `ResizeObserver` は `document.body` を観測する。

同じ理由で、**テンプレート内で `vh` 単位を使うときは注意**する。iframe の高さは自分で決めているので `70vh` のような指定は循環参照になる。係数が 1 未満なら収束するが、上記 2 を破ると一気に発散側へ倒れる。

```python
_HEIGHT_JS = """
<script>
(function () {
  var last = -1;
  function measure() {
    var b = document.body;
    return b ? Math.ceil(b.getBoundingClientRect().height) : 0;
  }
  function report() {
    var h = measure();
    if (h <= 0 || Math.abs(h - last) < 2) return;
    last = h;
    parent.postMessage({ type: 'iframe:height', height: h }, '*');
  }
  try { new ResizeObserver(report).observe(document.body); } catch (e) {}
  addEventListener('load', report);
  addEventListener('resize', report);
  setTimeout(report, 100);
  setTimeout(report, 600);
})();
</script>
"""

_BASE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font: 14px/1.65 system-ui, -apple-system, "Segoe UI", "Noto Sans JP", sans-serif;
  background: transparent; color: #1a1a1e;
}
.wrap { padding: 14px; }
.card { background: #fff; border: 1px solid #e3e3e8; border-radius: 14px; padding: 14px; }
.h { font-weight: 650; font-size: 15px; margin: 0 0 10px; }
.muted { opacity: .62; font-size: 12px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.btn { padding: 9px 16px; border-radius: 10px; border: 1px solid #d9d9e0;
       background: #f4f4f6; color: #1a1a1e; font: inherit; font-size: 13px;
       cursor: pointer; text-decoration: none; display: inline-block; }
.btn:hover { background: #ebebef; }
.btn.primary { background: #3b6fe0; border-color: #3b6fe0; color: #fff; }
.btn.primary:hover { background: #3260cc; }
textarea { width: 100%; min-height: 84px; padding: 10px; border-radius: 10px;
           border: 1px solid #d9d9e0; font: inherit; resize: vertical;
           background: #fff; color: inherit; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid #e8e8ee;
         vertical-align: top; }
th { font-weight: 600; opacity: .7; font-size: 12px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
       background: #f0f0f4; padding: 1px 5px; border-radius: 5px; }
.scroll { overflow-x: auto; }
@media (prefers-color-scheme: dark) {
  body { color: #e8e8ec; }
  .card { background: #1c1c20; border-color: #303038; }
  .btn { background: #2a2a31; border-color: #3a3a44; color: #e8e8ec; }
  .btn:hover { background: #33333c; }
  textarea { background: #16161a; border-color: #3a3a44; }
  th, td { border-bottom-color: #2c2c34; }
  code { background: #26262e; }
}
"""
```

> `embeds` の要素が `http://` / `https://` / `//` で始まると **URL とみなされ iframe の src** になる（`FullHeightIframe.svelte:52`）。生 HTML を渡すときは必ず `<!doctype html>` で始めること。

### 6.0.1 `_SCROLL_BOTTOM_JS` — embeds 直後の最下部ピン留め

**症状**: Functions 終了後、チャットが最下部に行かず、時間とともに最下部から遠ざかる。

**原因**: 上流は `embeds` 受信の **100 ms 後**に `scrollIntoView({behavior:'smooth', block:'center'})` を実行する（`Chat.svelte:1012-1017`）。ところがこの時点でレイアウトは確定していない。

1. iframe は sandbox（`allow-same-origin` 既定 off）なので、`resizeSameOrigin()` は例外で抜け、**高さは §6.0 `_HEIGHT_JS` の `postMessage` でしか決まらない**（`FullHeightIframe.svelte:144-163`）。その報告は `load` / **100 ms** / **600 ms** / `ResizeObserver`（`<video>` のメタデータ読み込み等）と後から続く。
2. さらにメッセージは `content-visibility: auto; contain-intrinsic-size: auto 150px`（`Message.svelte:155-156`）なので、画面外メッセージの高さは**推定値**であり、`scrollHeight` 自体が確定していない。上流の `scrollToBottom` が rAF を 3 段重ねているのはこの補正のため（`Chat.svelte:2110-2131`）。
3. `block: 'center'` は元々最下部を指していない。embeds の下には操作ボタン列と `bottomPadding` がある。

実測（上記構造を最小再現し Chrome headless で計測）: 現状は最下部から **839 px** 手前で停止。ピン留めを入れると **0 px**。

**対処**: `execute` イベント（`Chat.svelte:1103-1116`）で親ウィンドウの JS を実行し、高さが確定するまで `#messages-container` を最下部へ寄せ直す。`__event_emitter__` と `__event_call__` は同じ `events` チャンネル → 同じ `chatEventHandler` に届く（`socket/main.py:986` / `:1112`）ため、**`execute` は emitter でも動く**。応答を待つ必要はないので必ず emitter を使う（`__event_call__` にすると 300 秒ハングの経路を増やすだけ）。

**待ち方は時間ではなく状態で決める。** 高さ報告がいつ終わるかは `<video>` のメタデータ取得やネットワーク次第で、固定の待ち時間では足りたり足りなかったりする。`scrollHeight` が変化しなくなるまでピン留めし、上限だけ時間で切る。

```python
_SCROLL_BOTTOM_JS = """
const TAG = '[descript:scroll]';
const el = document.getElementById('messages-container');
if (!el) {
  console.warn(TAG, 'no #messages-container');
} else {
  let stop = false;
  const abort = (e) => { stop = true; console.log(TAG, 'aborted by ' + e.type); };
  el.addEventListener('wheel', abort, { passive: true, once: true });
  el.addEventListener('touchstart', abort, { passive: true, once: true });
  const gap = () => el.scrollHeight - el.scrollTop - el.clientHeight;
  console.log(TAG, 'start gap=' + gap() + ' h=' + el.scrollHeight);
  const t0 = Date.now();
  let lastH = -1;
  let stable = 0;
  const iv = setInterval(() => {
    if (stop) { clearInterval(iv); return; }
    el.scrollTop = el.scrollHeight;
    if (el.scrollHeight === lastH) { stable += 1; } else { stable = 0; lastH = el.scrollHeight; }
    const over = Date.now() - t0 > 15000;
    if (stable >= 12 || over) {
      clearInterval(iv);
      el.scrollTop = el.scrollHeight;
      console.log(TAG, 'end gap=' + gap() + ' h=' + el.scrollHeight +
                  ' ms=' + (Date.now() - t0) + (over ? ' (timeout)' : ''));
    }
  }, 80);
}
"""


async def _emit_scroll_bottom(event_emitter) -> None:
    """embeds 直後にチャットを最下部へ寄せ直す（契約書 §6.0.1）。"""
    if event_emitter is None:
        return
    try:
        await event_emitter({"type": "execute", "data": {"code": _SCROLL_BOTTOM_JS}})
    except Exception:
        pass
```

- 終了条件は「`scrollHeight` が **12 tick（約 1 秒）**変わらないこと」。上限 15 秒は保険であって設計値ではない。固定の待ち時間にしないのは、高さ報告がいつ終わるかが `<video>` のメタデータ取得とネットワーク次第だから。
- **`console.log('[descript:scroll] …')` は残すこと。** 上流の `execute` は `catch` が空で、投げた JS が失敗しても**画面にもサーバログにも何も出ない**（配信バンドルで確認: `catch($e){}`）。効いているかを外から確かめる手段がこれしかない。
- ユーザが `wheel` / `touchstart` で自分でスクロールしたら即座に降りる。ユーザ操作と綱引きしない。
- **embeds が空配列のときは送らない。** 打ち消し用の `replace: True, embeds: []` でスクロールを動かす理由がない。
- `execute` は `Chat.svelte:964` の `if (message)` の内側にあるため、`message_id` が history に存在しないと**無視される**。§1.1 のとおり Action の `body['id']` を再利用していることが前提。

### 6.0.2 🔴 embeds 内のリンクで外部サイトを開かせない

**症状**: 埋め込みカードの「Descript で開く」を押すと、タブは開くのにアプリが表示されない。ブラウザ拡張が `Failed to connect to MetaMask / extension not found` を投げることがある。

**原因**: 埋め込み iframe の `sandbox` は `allow-scripts allow-forms allow-popups allow-downloads`（`allow-same-origin` は設定次第）で、**`allow-popups-to-escape-sandbox` が無い**（配信バンドルで確認）。HTML 仕様上、`allow-popups-to-escape-sandbox` が無い sandbox から開いた新規ウィンドウは**同じ sandbox フラグを引き継ぐ**。結果、`https://web.descript.com/...` は**オリジンが `null`（opaque）**の状態で読み込まれ、Cookie も `localStorage` も使えないので SPA が起動しない。拡張の `inpage.js` が「見つからない」と言うのも、opaque オリジンでは拡張のブリッジが張れないため。原因ではなく**症状**である。

**対処**: 外部サイトへのリンクは **iframe の中に置かない**。トップドキュメント側、すなわち**アシスタントメッセージ本文の Markdown リンク**として出す。メッセージ本文は Open WebUI 本体が描画するので sandbox の影響を受けない。

- カード内のボタンは残してよいが、**本文側のリンクが正**とする。Pipe は結果 `data` に `project_url` を必ず載せ、Action はそれを `- [Descript で開く](...)` として本文に書く。
- ユーザ設定 `iframeSandboxAllowSameOrigin` を有効にすればカード内のボタンも通るが、これは **srcdoc の iframe が親オリジンを名乗れる**ようになる設定で、埋め込み HTML から `localStorage` のトークンに手が届く。既定 off のまま運用する。

### 6.1 `_PLAYER` — publish 結果のプレビュー

```python
_PLAYER = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">{{title}}</p>
  <video src="{{src}}" controls playsinline preload="metadata"
         style="width:100%;max-height:70vh;border-radius:10px;background:#000;display:block"></video>
  <div class="row">
    <a class="btn primary" href="{{app_url}}" target="_blank" rel="noopener">Descript で開く</a>
    <a class="btn" href="{{share_url}}" target="_blank" rel="noopener">共有リンク</a>
  </div>
  <p class="muted" style="margin:10px 0 0">rev {{revision}} ・ {{note}}</p>
</div></div>{{height_js}}</body></html>"""
```

差し込み: `_render(_PLAYER, css=_BASE_CSS, height_js=_HEIGHT_JS, title=_esc(...), src=..., app_url=..., share_url=..., revision=..., note=_esc(...))`

`src` に `download_url` を使う場合、**期限切れになるため state には保存しない**（表示のその場限り）。`share_url` は恒久リンクなので保存してよい。

### 6.2 `_TOOL_TABLE` — probe / TOOL_UNRESOLVED のツール一覧

```python
_TOOL_TABLE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">Descript MCP ツール一覧（{{count}} 件）</p>
  <p class="muted">{{lead}}</p>
  <div class="scroll"><table>
    <thead><tr><th style="width:30%">ツール名</th><th style="width:16%">推定</th><th>説明 / 必須引数</th></tr></thead>
    <tbody>{{rows}}</tbody>
  </table></div>
  <p class="muted" style="margin-top:12px">
    ここで確認した名前を Admin → Functions → Descript Orchestrator の Valves
    <code>tool_list_projects</code> / <code>tool_get_project</code> / <code>tool_import_media</code> /
    <code>tool_agent_edit</code> / <code>tool_publish</code> / <code>tool_job_status</code> に設定してください。
  </p>
</div></div>{{height_js}}</body></html>"""
```

`rows` の 1 行:

```python
"<tr><td><code>{name}</code></td><td>{guess}</td><td>{desc}<div class='muted'>必須: {required}</div></td></tr>"
```

`guess` は解決済み論理操作名（未解決なら `—`）。`name` / `desc` / `required` は必ず `_esc()` を通す。

### 6.3 `_EDIT_LOOP_FORM` — 編集ループの継続フォーム

```python
_EDIT_LOOP_FORM = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{{css}}</style></head>
<body><div class="wrap"><div class="card">
  <p class="h">{{title}}</p>
  <video src="{{src}}" controls playsinline preload="metadata"
         style="width:100%;max-height:60vh;border-radius:10px;background:#000;display:block"></video>
  <p class="muted" style="margin:10px 0 6px">{{agent_response}}</p>
  <textarea id="d-input" placeholder="追加の編集指示（例: 冒頭 3 秒をカット、字幕をもう少し大きく）"></textarea>
  <div class="row">
    <button class="btn primary" type="button" data-act="revise">この指示で再編集</button>
    <button class="btn" type="button" data-act="confirm">これで確定する</button>
    <a class="btn" href="{{app_url}}" target="_blank" rel="noopener">Descript で開く</a>
  </div>
  <p class="muted" style="margin:10px 0 0">rev {{revision}} / 残り {{remaining}} 回</p>
</div></div>
<script>
(function () {
  document.querySelectorAll('button[data-act]').forEach(function (b) {
    b.addEventListener('click', function () {
      var payload = JSON.stringify({
        op: b.getAttribute('data-act'),
        text: (document.getElementById('d-input') || {}).value || ''
      });
      parent.postMessage({ type: 'input:prompt:submit', text: '@descript ' + payload }, '*');
      document.querySelectorAll('button[data-act]').forEach(function (x) { x.disabled = true; });
    });
  });
})();
</script>{{height_js}}</body></html>"""
```

**動作**: iframe 内のボタンが `parent.postMessage({type:'input:prompt:submit', ...})` を送ると、`Chat.svelte:1201-1218` が確認ダイアログを挟んで `submitHandler` に流す。これが Descript Pipe モデルへの**新規チャットメッセージ**として再入する。

**Pipe 側の受け口**: 通常チャット経路で入ってきた `body["messages"][-1]["content"]` が `@descript {` で始まる場合、JSON を復号して `resume_edit_loop` として扱う。`project_id` / `conversation_id` / `revision` は `chat.chat["descript"]` から復元する。

この方式を採る理由は §「300 秒タイムアウト」を踏まないため。`__event_call__` を開いたまま長時間ジョブを回すと `WEBSOCKET_EVENT_CALLER_TIMEOUT`（既定 300 秒）に抵触する。

---

## 7. タイムアウトの規律

| 制約             | 値                                                                                                                                                                                     | 対処                                                                                                                                                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `__event_call__` | `WEBSOCKET_EVENT_CALLER_TIMEOUT`。**未設定なら無期限**（`env.py:502-510` で `None` になり、`socket/main.py:1119` の `sio.call(timeout=None)` は待ち続ける）。値が不正なときだけ 300 秒 | フォームは短時間で閉じるものだけに使う。`confirm_timeout_sec`（既定 240）を上限とし、**`asyncio.wait_for` では包まない**（socket の ack を壊す）。戻り値の `{"error": ...}` で判定する |
| Action の HTTP   | リバースプロキシ依存（nginx `proxy_read_timeout` 既定 60 秒、Vercel の関数上限）                                                                                                       | `action_soft_timeout_sec`（既定 45）を超えそうな処理は Action を早期 return し、以降は Pipe ＋ embeds フォームで継続                                                                   |
| MCP 初期化       | `MCP_INITIALIZE_TIMEOUT` 既定 10 秒                                                                                                                                                    | `mcp_connect_retries` 回だけ指数バックオフで再試行                                                                                                                                     |
| Descript ジョブ  | 不定                                                                                                                                                                                   | `_poll_job` の `poll_timeout_sec`（既定 900）。超過しても**ジョブは中断せず** `job_id` を state に保存する                                                                             |

### 順序の原則（この順を守る）

1. `status` で「開始」を出す
2. 長時間ジョブを回す（`_poll_job` が定期的に `status` を更新）
3. `status(done=True)` を出す
4. **その後で** `__event_call__` または embeds フォームを出す

`__event_call__` を開いたままジョブを回してはいけない。

---

## 8. MCP クライアントのライフサイクル

```python
client = None
try:
    connected = await connect_mcp_server(__request__, server_id, user, metadata, extra_params)
    if connected is None:
        raise DescriptError(...)          # MCP_NOT_FOUND / MCP_FORBIDDEN を自前で切り分け
    client, specs = connected
    ...
finally:
    if client is not None:
        await client.disconnect()
```

> ⚠️ `disconnect()` は **connect と同一の asyncio タスク**で呼ぶ必要がある（`utils/mcp/client.py:163-172`）。`asyncio.shield` / `asyncio.wait_for` / `anyio.CancelScope` / `anyio.fail_after` で包むと MCP SDK の TaskGroup 制約に違反して壊れる。

`MCP_NOT_FOUND` と `MCP_FORBIDDEN` は `connect_mcp_server` がどちらも `None` を返すため区別できない。Pipe 側で先に `Config.get("tool_server.connections")` を自前走査して切り分ける:

```python
from open_webui.models.config import Config

connections = await Config.get("tool_server.connections", []) or []
found = any(
    c.get("type") == "mcp" and (c.get("info") or {}).get("id") == server_id
    for c in connections
)
```

---

## 9. クラス属性の規律（v0.11.0 の実装に基づく）

`load_function_module_by_id` は `module.Filter()` のように**インスタンス**を返す（`backend/open_webui/utils/plugin.py:296-306`）。以降のすべての検出は `getattr(instance, ...)` で行われる。

したがって次の 3 つは**必ず `class` 直下のクラス属性**として書く。モジュールトップレベルに書くと**エラーも警告も出ないまま無視される**。

| 属性           | 検出箇所                                 | 対象                       |
| -------------- | ---------------------------------------- | -------------------------- |
| `actions`      | `backend/open_webui/utils/models.py:246` | Action（マルチアクション） |
| `file_handler` | `backend/open_webui/utils/filter.py:171` | Filter                     |
| `toggle`       | `backend/open_webui/utils/models.py:409` | Filter                     |

```python
class Filter:
    file_handler = True     # ← ここ。モジュールトップレベルではない
    toggle = True

    class Valves(BaseModel):
        priority: int = 0

    def __init__(self):
        self.valves = self.Valves()   # ← これが無いと Valves が注入されない
```

> Open WebUI の公式ドキュメントには `file_handler` を「module attribute, not `self.file_handler`」と記載した箇所があるが、v0.11.0 のコードとは一致しない。

### 9.1 `file_handler = True` の副作用

`utils/filter.py:229-233` の `skip_files` による削除は、**全 Filter の inlet 完了後に 1 回だけ**走り、`body["files"]` と `body["metadata"]["files"]` を**丸ごと**消す。

**Filter 単位ではなくパイプライン単位**なので、`file_handler = True` にすると動画以外の添付（PDF、画像など）も RAG に載らなくなる。`toggle = True` でユーザが明示 ON にしたときだけ有効になる前提で許容している。動画以外も扱いたくなった場合は、Filter の inlet で必要なものを退避してから消させること。

### 9.2 Filter の各フックに実際に渡る dunder 引数

`utils/filter.py:123-132` の `get_filter_params` は、**シグネチャに宣言した引数だけ**を渡す（宣言し忘れても `TypeError` にはならず、単に渡らない）。

| フック                                       | 渡る dunder                                                                                                                                                                                                        |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `inlet`                                      | `__event_emitter__` `__event_call__` `__user__` `__metadata__` `__oauth_token__` `__request__` `__model__` `__chat_id__` `__message_id__` `__id__`                                                                 |
| `stream`（経路A: `middleware.py:4218`）      | `__body__` `__event_emitter__` `__event_call__` `__user__` `__metadata__` `__oauth_token__` `__request__` `__model__` `__id__`                                                                                     |
| `stream`（経路B: `middleware.py:5618/5631`） | 上記から `__body__` を除いたもの。**ただし `__event_emitter__` の値は `None`**（この分岐は `event_emitter` が falsy のときに入るため）                                                                             |
| `outlet`                                     | `__event_emitter__` `__event_call__` `__user__` `__metadata__` `__request__` `__model__` `__id__`。**`__chat_id__` / `__message_id__` は無い**（`middleware.py:3505-3512`）。chat_id は `body["chat_id"]` から取る |

**`stream` の実装上の注意**（いずれも実ソースで確認済み）:

- 第 1 引数名は `event` 固定（`filter.py:124`）
- `__event_emitter__` は宣言すれば渡るが、**経路B では `None`**。必ず None ガードすること
- 経路B の `event` は `response.body_iterator` の生の行なので、**dict ではなく `str` / `bytes` が渡ることがある**（`middleware.py:5630`）。dict 以外は無条件で素通しすること
- `__user__` を宣言すると `params["__user__"]["valves"]` に UserValves が注入される（`filter.py:181-183`）

### 9.3 Filter が例外を投げてはいけない

Filter が落ちるとチャット全体が止まる。`inlet` / `stream` / `outlet` の**全体を try/except で包み、失敗時は入力をそのまま返す**こと。想定外の入力（`body` が `None`、`files` が非 list、`messages` が無い等）でも例外にならないようにする。

---

## 10. frontmatter 規約

```python
"""
title: <表示名>
author: A-clear
author_url: https://github.com/A-clear/short-video-creation
version: 0.1.0
license: MIT
description: <1 行説明>
required_open_webui_version: 0.11.0
requirements:
"""
```

- **1 行目が厳密に `"""` でなければ frontmatter は全無視される**（`backend/open_webui/utils/plugin.py:160-162`）。前に空行・コメント・shebang を置かないこと。
- キーは `^\s*([a-z_]+):\s*(.*)\s*$` にマッチする必要がある（`plugin.py:156`）。**ハイフンや数字を含むキーは無視される**。
- **`requirements` は空にする。** 本番では `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false` を推奨しており（複数ワーカーでの同時 pip install がワーカーをクラッシュさせるため）、そもそも DB 経由ロード時には pip install が走らない（`plugin.py:263-276`）。Open WebUI 同梱の `mcp` / `httpx` / `pydantic` / 標準ライブラリのみを使う。
- `function_id` は `isidentifier()` を満たし、小文字化しても同一である必要がある（`backend/open_webui/routers/functions.py:206-213`）。

---

## 11. 秘匿情報の規律

**本リポジトリは public**（Functions を GitHub の raw URL から取り込むため）。

- API キー・MCP の URL・Drive 名・プロジェクト名を**コードに直書きしない**。すべて Valves / UserValves に逃がし、値は Open WebUI 側で設定する。
- `download_url` は署名付き・期限付き。**state に保存せず**、チャット履歴に残さない（Filter の `outlet` が `_redact_signed_urls` で `share_url` に差し替える）。
- ログ・`status` イベント・エラーの `data` に生のトークンやヘッダを載せない。例外文字列は 800 文字で切り詰める（§5.5）。

---

## 12. 未確定事項（推測で埋めない）

### 12.0 解決済み（2026-08-02、実機 `probe` により確定）

| 項目                                  | 結論                                                                                                                                                                  |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Descript MCP のツール名・引数スキーマ | **確定。§2.0 / §2.0.1 を参照。** 全 12 ツール。`agent_edit` は `prompt_project_agent`、`job_status` は `wait_for_job`                                                 |
| FCPXML 書き出しの可否                 | **可能だった。** `export_timeline`（`format: "fcp"`）で .fcpxml を直接ダウンロードできる。「Descript App で手動書き出し」という当初の結論は**誤り**。UC3 はこれを使う |
| `publish` の `media_type`             | **`Video` / `Audio`（先頭大文字）**。拡張子表記（`mp4` 等）では 422                                                                                                   |
| ジョブ待機の方式                      | `wait_for_job` は**ブロッキング待機**（既定 300 秒、進捗ストリーム）。`wait_seconds: 0` で即時リターン                                                                |

### 12.1 未解決

| 項目                                                              | 状況                                                                                                                                                                                                       | 実装での扱い                                                                                                                                                                                                             |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ~~ローカルファイルの直接アップロード~~ **解決済み（2026-08-03）** | `Files.get_file_by_id` → `Storage.get_file` で実体パスを解決できることを実機で確認（`MinIO` 構成で 170MB の `.mov`、`content_type=video/quicktime`）。`import_media` が `upload_urls` を返すことも確認済み | `_resolve_stored_file` で解決し、`httpx.AsyncClient` に**非同期**ジェネレータで PUT する（§2.0.1 ① の 🔴 注記）。PUT 失敗時は `report_upload_status` で import ジョブを解放する                                          |
| `publish_project` の `resolution`                                 | probe のツール説明に記載が無い。REST API 側（`descript_api.json`）には `480p`〜`4K` の enum が存在する                                                                                                     | 渡さない方向に倒す。`_coerce_args` がスキーマに無いキーを落とすため実害は無い                                                                                                                                            |
| Descript の `download_url` の署名方式                             | AWS SigV4 / GCS のどちらでもない独自方式の可能性がある                                                                                                                                                     | §5.9 の注記を参照。E2E で 1 回クエリパラメータを実見して確認する                                                                                                                                                         |
| `WEBUI_URL` を HTTPS 公開 URL にする必要性                        | Descript の IdP（Stytch）は confidential client に **HTTPS かつ非 loopback** の redirect URI を要求する。`http://localhost:8080` では動的クライアント登録（DCR）が 400 で失敗する                          | `WEBUI_URL` を公開 HTTPS URL にする（トンネル or 実ドメイン）。**PersistentConfig なので `.env` ではなく Admin Panel → Settings → General で変更する**（`models/config.py` の `seed_defaults` は既存の DB 値を優先する） |
| `gpt-realtime-2.1` の接続方法                                     | Open WebUI v0.11.0 に該当する設定項目が存在しない                                                                                                                                                          | `.env.example` に注記のみ。使うなら別途 Pipe Function として実装が必要                                                                                                                                                   |
