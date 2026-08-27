# Playwright MCP セットアップ手順

Open WebUI Computer の中で立てた開発サーバを、**エージェントにブラウザで操作させる**ための手順。

対象は `docker-compose.yml` の `playwright-mcp-a` / `-b` / `-c` と、各 Computer の Tool Server 登録。

---

## 0. これが必要かどうか

| やりたいこと                                               | 使うもの                                                   |
| ---------------------------------------------------------- | ---------------------------------------------------------- |
| **人間が**目で dev サーバを見たい                          | Computer の **Browser タブ**（プロキシ）。この手順書は不要 |
| **エージェントに**クリック・入力・コンソール確認をさせたい | この手順書                                                 |

Browser タブは HTTP プロキシ型で、`http://localhost:5173` をそのまま開けます。HMR も通ります。
ポート公開もこの手順も要りません。**まず Browser タブを試してください。**

---

## 1. なぜ別コンテナなのか

Computer のイメージには **Chromium が要求する共有ライブラリが 1 つも入っていません**。実測値:

```
libnss3.so        0     libgbm.so.1        0     libcups.so.2       0
libatk-1.0.so.0   0     libxkbcommon.so.0  0     libpango-1.0.so.0  0
libasound.so.2    0     libdrm.so.2        0     libxcomposite.so.1 0
                                    （ldconfig -p のヒット数）
```

`cptr` は非 root で `sudo` も `apt` も無いため、`playwright install` でブラウザ本体を落としても
**起動時にリンクエラーで落ちます**。ブラウザは必ずコンテナの外に置きます。

チームごとに 3 台置くのは、1 台を 3 ネットワークに参加させるとそのブラウザが
3 チームすべての Computer と dev サーバに到達できてしまい、コンテナを分けている意味が
消えるためです。

### ネットワーク境界

```
┌─ svc-computer-net-a ────────┐   ┌─ svc-playwright-net-a ──────────┐
│                             │   │                                 │
│  open-webui ──▶ open-webui-computer-a ◀──▶ playwright-mcp-a       │
│  (gateway を叩く)           │   │   :8931/mcp ↑   ↓ dev サーバ    │
└─────────────────────────────┘   └─────────────────────────────────┘
```

**Playwright を `svc-computer-net-*` に相乗りさせてはいけません。** そちらには
`open-webui` 本体も参加しているため、「エージェントが指示した URL を開くブラウザ」が
Open WebUI 本体に到達できてしまいます。両者の間に必要な通信は 1 本もないので、
Computer と 1:1 の `svc-playwright-net-*` を用意しています。

実測（`docker compose config` の解決結果と到達性）:

| 経路 | 結果 |
| --- | --- |
| Computer → `playwright-mcp-a:8931/mcp` | 到達（HTTP 406 = MCP ハンドラ生存） |
| Playwright → Computer の dev サーバ `:5173` | 到達（HTTP 200） |
| Playwright → `open-webui:8080` | **到達不可**（`fetch failed`） |

ブラウザから見える先はチームの Computer と外部インターネットだけです。
Computer 自身の cptr ログイン画面（`:8000`）には到達できますが、dev サーバと同居している
以上これは避けられません。外部へも出したくない場合は `docker-compose.yml` の
`svc-playwright-net-*` にある `internal: true` を有効にしてください。

---

## 2. 起動

```bash
# ① Computer イメージに mcp-remote を焼き込む（理由は §3）
docker compose build

# ② Playwright MCP と Computer を起動
docker compose up -d playwright-mcp-a playwright-mcp-b playwright-mcp-c
docker compose up -d open-webui-computer-a open-webui-computer-b open-webui-computer-c
```

`docker compose ps` で 3 台とも `healthy` になれば準備完了です。
ヘルスチェックは `POST /mcp` に `406` が返ることを確認しています（`Accept` ヘッダ不足による
正常な応答で、MCP ハンドラが生きている証明になります）。

ホストにポートは公開していません。**認証なしでブラウザを遠隔操作できる API** なので、
チームの Computer からサービス名で引ければ十分です。

---

## 3. Computer への登録

各 Computer（`http://localhost:8001` / `:8002` / `:8003`）で
**Settings → Admin → Tools → Add Tool Server**:

| 項目    | 値                                                                              |
| ------- | ------------------------------------------------------------------------------- |
| Type    | **MCP (stdio)**                                                                 |
| ID      | `playwright`                                                                    |
| Command | `mcp-remote`                                                                    |
| Args    | `http://playwright-mcp-a:8931/mcp`<br>`--transport http-only`<br>`--allow-http` |

`-b` / `-c` はホスト名をそれぞれ `playwright-mcp-b` / `playwright-mcp-c` に読み替えてください。

### ⚠️ Type を「MCP (HTTP)」にしてはいけない

Streamable HTTP を直接指す `type: mcp` は、**接続はできるがブラウザ操作が成立しません。**

cptr は `type: mcp` のときツール呼び出しのたびに接続して切断します:

```python
# cptr/utils/tools.py:2931-2940
if tool_type == "mcp":
    client = MCPClient()
    await client.connect(server.get("url", ""), headers)
    try:
        result = await client.call_tool(original_name, args)
        return _extract_mcp_result(result)
    finally:
        await client.disconnect()          # ← 毎回切れる
```

MCP セッションが毎回作り直されるため、`browser_navigate` の次に `browser_snapshot` を呼ぶと
**`about:blank` が返ります**（実測）。`--shared-browser-context` を付けても直りません。
前のタブは `browser_tabs` の一覧にも残らず、完全に消えます。

一方 `mcp_stdio` は `stdio_manager` がプロセスを**ツール呼び出しをまたいで生かし続ける**ため、
上流の MCP セッションが 1 本のまま保たれます。そこで stdio ↔ HTTP を変換する薄いブリッジ
（`mcp-remote`）を挟みます。ブラウザは別コンテナのまま、セッションだけが持続します。

`mcp-remote` は `npx` ではなくイメージに焼き込んであります（`docker/computer/Dockerfile`）。
`npx` にすると最初のツール呼び出しでネットワーク取得が走り、「ビルドは通ったのに実行時に
落ちる」が起きるためです。pnpm を corepack で入れない理由と同じです。

### ⚠️ `--allow-http` は必須

`mcp-remote` は既定で非 HTTPS の URL を拒否します。付け忘れると cptr 側には

```
mcp.shared.exceptions.McpError: Connection closed
```

としか出ず、原因が分かりません。ブリッジのプロセスに直接聞くと分かります:

```
Error: Non-HTTPS URLs are only allowed for localhost or when --allow-http flag is provided
```

---

## 4. 使い方

登録すると 24 個のツールが生えます（`browser_navigate` / `browser_snapshot` / `browser_click` /
`browser_type` / `browser_fill_form` / `browser_console_messages` / `browser_network_requests` ほか）。

### 到達先はサービス名で指す

ブラウザは別コンテナに居るので **`http://localhost:5173` では届きません**。

```bash
# Computer のターミナルで
pnpm dev --host 0.0.0.0
```

エージェントには `http://open-webui-computer-a:5173` を渡します。

| 立てる側 | `0.0.0.0` bind の指定     |
| -------- | ------------------------- |
| vite     | `pnpm dev --host 0.0.0.0` |
| next     | `next dev -H 0.0.0.0`     |
| uvicorn  | `uvicorn --host 0.0.0.0`  |

### Snapshot モードが既定

`browser_snapshot` はアクセシビリティツリーを返します:

```yaml
- generic [active] [ref=e1]:
    - heading "Vite Dev Server" [level=1] [ref=e2]
    - button "Click me" [ref=e3]
```

`ref=e3` を `browser_click` に渡す形で、座標ではなく要素で操作します。
ツール呼び出しができるモデルなら何でも動き、座標クリックより安定して安価です。
座標が要る場合だけ `--caps=vision` を足してください（`PLAYWRIGHT_MCP_CAPS`）。

**「Computer use」モデル（gpt-5.4 以降）は使えません。** cptr のソース全体に
`computer_use` / `computer-use` の文字列が無く、アクションループが実装されていません。
そして Snapshot モードがあるので必要もありません。

---

## 5. 落とし穴（すべて実測）

### `PLAYWRIGHT_MCP_ALLOWED_HOSTS` が無いと 403

DNS リバインディング対策で、`Host` ヘッダが許可リストに無いと `403 Forbidden` になります。
サーバ側のログには**何も出ません**。compose では設定済みです:

```
PLAYWRIGHT_MCP_ALLOWED_HOSTS: playwright-mcp-a:8931,localhost:8931
```

`localhost:8931` はヘルスチェック用です。消すとヘルスチェックだけが落ちます。

### 起動ログの "localhost" は表示だけ

`PLAYWRIGHT_MCP_HOST=0.0.0.0` を設定していても、ログは

```
Listening on http://localhost:8931
```

と出ます。実際の bind は環境変数に従っているので、この行で判断しないでください。

### スクリーンショットは文脈を壊す

`PLAYWRIGHT_MCP_IMAGE_RESPONSES` を `omit` にしないと、`browser_take_screenshot` が
base64 の `image` ブロックを返します。cptr の `_extract_mcp_result` は非 text ブロックを
`json.dumps` してテキストに混ぜるため（`utils/tools.py:2906-2915`）、

- モデルには**画像として渡らない**（ただの文字列）
- 実測で小さなページでも **12KB** のゴミが文脈に入る

compose では `omit` にしてあり、スクリーンショットはファイルに保存されてパスだけが返ります。
画面を目で見たいときは Computer の **Browser タブ**を使ってください。

### `/dev/shm` は 64MB では足りない

Docker の既定 `/dev/shm` は 64MB で、Chromium がタブを開いた瞬間にクラッシュすることが
知られています。compose で `shm_size: 1gb` にしています（`PLAYWRIGHT_MCP_SHM_SIZE`）。

### ワークスペースを共有マウントしてはいけない

`--output-dir` を Computer の `/workspace` に向けたくなりますが、Playwright のイメージに
`/workspace` が存在しないため、Docker が **root 所有**でディレクトリを作り
`EACCES: permission denied, mkdir '/workspace'` になります。
Computer のボリュームと同じ罠です（`.claude/CLAUDE.md` の Dockerfile 節を参照）。

そもそも共有する必要はありません。`browser_snapshot` は**インラインで**内容を返すためです。
加えて、任意のページを開くブラウザにチームのワークスペースへの書き込み権を与えることになり、
プロンプトインジェクション経由でファイルを置かれる経路になります。

### ブラウザは Computer 単位で 1 つ

`stdio_manager` は `server_id` だけをキーにしてクライアントを使い回します
（`self._instances: dict[str, MCPClient]`）。つまり **1 つの Computer コンテナにつき
ブラウザセッションは 1 本**で、同じチームの別チャット・別ユーザと共有されます。
2 人が同時に使うとタブを奪い合います。

cptr にそもそもユーザ間分離が無い設計なので新しい穴ではありませんが、
「同時に触ると相手の画面が飛ぶ」ことは知っておいてください。

### `--allowed-origins` は既定で無効

セミコロン区切りでオリジンを列挙すると、それ以外への遷移が `ERR_BLOCKED_BY_CLIENT` で
止まります（実測で動作確認済み）。ただし CDN や Google Fonts も落ちるため、
実ページの描画が崩れます。

既定では設定せず、境界はネットワーク分離（`svc-playwright-net-a` / `-b` / `-c`）に任せています。
持ち出しまで塞ぎたい場合は `.env` の `PLAYWRIGHT_MCP_ALLOWED_ORIGINS_A` に自分の
dev サーバのオリジンを列挙してください。

### Service Worker はブロックしない

`--block-service-workers` は**付けていません**。本プロジェクトの成果物は PWA で、
ブロックすると検証したい挙動そのものが消えるためです。

---

## 6. 動作確認

```bash
# ブリッジ経由でツールが見えるか（Computer のターミナルから）
mcp-remote http://playwright-mcp-a:8931/mcp --transport http-only --allow-http
```

正常なら 24 個のツールを持つ MCP サーバとして応答します。
`Connection closed` になる場合は `--allow-http` の付け忘れ、
`403` になる場合は `PLAYWRIGHT_MCP_ALLOWED_HOSTS` にそのホスト名が入っていません。

チャットからは次の順で確認するのが確実です:

1. `http://open-webui-computer-a:5173` を開いて — `browser_navigate`
2. 何が見える? — `browser_snapshot`（`about:blank` が返ったら Type が HTTP になっています）
3. コンソールエラーは? — `browser_console_messages`
