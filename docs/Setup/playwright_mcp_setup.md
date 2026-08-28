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

### ⚠️ Codex プロファイルにはツールが渡らない

**登録が正しくても、モデルが `agent:codex/...` だとブラウザツールは 1 つも生えません。**

cptr には**ツール体系の異なる 2 つの実行経路**があります。

| 経路             | モデル指定の例                              | ツールの出どころ                                            |
| ---------------- | ------------------------------------------- | ----------------------------------------------------------- |
| ネイティブ       | Connections のモデル（`openai` / `anthropic`） | `utils/tools.py` の `get_tool_list()` ← **この手順書はここに効く** |
| 外部エージェント | `agent:codex/gpt-5.4` など                  | codex CLI 自身の設定（`~/.codex/config.toml`）              |

`utils/agents/codex.py` は codex CLI に**設定を一切書きません**（`config.toml` の生成も、
ツール一覧の受け渡しも無い）。やっているのは codex が吐くイベントの解釈だけで、
`mcp` という項目種別を _表示のために_ 知っているにすぎません（`codex.py:305`）。

**エラーは出ません。ツールが黙って存在しないだけです。**

どうしても Codex サブスクリプションで動かすなら codex 側に別途登録します
（cptr の管理画面とは二重管理になり、`docker compose down -v` で `codex-home-*` を
消すと再設定が要ります）:

```bash
codex mcp add playwright -- mcp-remote http://playwright-mcp-a:8931/mcp \
  --transport http-only --allow-http
```

### 使うモデルを固定する

gateway のモデル解決は 3 段のフォールバックです（`routers/gateway.py:626-` のコメント）:

1. `gateway.model`（Settings → Admin）
2. ワークスペースの `<workspace>/.cptr/model`
3. `chat.default_model`（Settings → Models）

3 段とも空でも動いてしまいますが、そのときどのモデルが選ばれたかは設定から読み取れません。
**ブラウザ操作エージェントでは 2 を明示してください。** チームごとに用途が違うためです。

```bash
# ネイティブ経路（＝ブラウザツールが生える）。接続の prefix_id が未設定なら
# OpenAI のモデル ID をそのまま書く
echo 'gpt-5.4' > /workspace/<name>/.cptr/model
```

> ⚠️ `.cptr/model` は環境変数でも compose でも設定できません。ワークスペース内のファイルです。
> 接続に `prefix_id` を設定している場合だけ `<prefix>/<model-id>` になります
> （`utils/model_targets.py:96-98`）。

### 組み込みブラウザツールとの重複

cptr は同名の組み込みツールを 6 つ持っています（`browser_navigate` / `browser_snapshot` /
`browser_click` / `browser_type` / `browser_screenshot` / `browser_evaluate`。`tools.py:1824-1934`）。
外部ツールは `{server_id}_{tool_name}` に前置きされるので衝突はしませんが、両方有効だと
モデルにほぼ同じツールが 2 セット見えます。しかも組み込み側は Chrome を自前起動しようとして、
このイメージに Chrome は無いので失敗します。

**既定は off なので通常は何もしなくて構いません**（`browser.enabled` 未設定 → `False`。
`tools.py:1790` / `:3098`）。過去に有効化したことがある場合だけ、
Settings → Admin → **Browser** → Agent browser tools を off に戻してください。


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

### ⚠️ スナップショットは高い。既定にしない

**`browser_snapshot` は実ページで 1 回あたり約 13,000 トークンです。** 実測値
（`playwright-mcp-a` に直接接続して計測）:

| 対象                      | snapshot 文字数 | 概算トークン |
| ------------------------- | --------------: | -----------: |
| `https://example.com`     |             411 |          137 |
| `https://react.dev/`      |          39,885 |       13,295 |
| `https://vite.dev/guide/` |          40,003 |       13,334 |

エージェントループは履歴を積み上げるため、10 往復すれば入力だけで 13 万トークン規模に
なります。**ここが唯一かつ最大のコスト要因**です。

対策は `browser_find` です。同じページで比較した実測:

| ツール                              | 文字数 | 概算トークン | snapshot 比 |
| ----------------------------------- | -----: | -----------: | ----------: |
| `browser_snapshot`                  | 39,049 |       13,016 |        100% |
| `browser_find` `text: "Getting Started"` |    945 |          315 |    **2.4%** |
| `browser_find` `text: "Command Line Interface"` |  1,919 |          639 |        4.9% |
| `browser_find` `regex: "/button/i"` |  8,410 |        2,803 |       21.5% |

`browser_find` は**マッチしたノードとその周辺だけを、ルートからのパス付きで**返します。
`ref=e98` がそのまま得られるので、`browser_click` に直接渡せます。全体像が要るとき以外、
snapshot を呼ぶ理由はありません。

> ⚠️ パラメータ名は `text` または `regex` です（`query` ではありません）。
> 両方を同時に渡すとエラーになります。

システムプロンプトに一文入れておくと効きます:

```
ページの状態を調べるときは browser_find を優先し、browser_snapshot は
ページ全体の構造を把握する必要があるときだけ使うこと。
```

### なぜ Codex ではなくネイティブ経路（OpenAI 等）を使うのか

`codex mcp add` で codex 側に登録すれば Codex でもブラウザ操作はできます（実証済み。
`~/.codex/config.toml` に `[mcp_servers.*]` が書かれる）。それでもネイティブ経路を勧めます。

| 観点         | ネイティブ（Connections）        | Codex                                        |
| ------------ | -------------------------------- | -------------------------------------------- |
| 設定場所     | cptr の Admin → Tools（1 か所）  | `~/.codex/config.toml`（cptr の UI に出ない） |
| 台数分の手間 | 同上                             | 3 コンテナで個別に `codex mcp add`           |
| 消えやすさ   | `/data` ボリューム               | `codex-home-*`。消すと再ログインも必要       |
| 責務         | ブラウザ操作に必要な道具が揃う   | コーディングエージェント。強みが噛み合わない |
| 課金         | API 従量（上表のコストが効く）   | ChatGPT サブスク（ただし 3 チームで枠を共有）|

**コストだけは Codex に分があります。** 上の `browser_find` を徹底すれば従量でも現実的な
範囲に収まるため、設定の一元化と可観測性を取ってネイティブ経路にしています。


### 操作中の画面をリアルタイムに見る（Chrome + noVNC）

**既定の Playwright MCP の画面は覗けません。** Chromium を自分の内側で起動し、CDP を
Playwright 内部のパイプで握るためです。Computer の Browser タブで同じ URL を開いても
**別セッション**なので、ミラーにはなりません。

`PLAYWRIGHT_MCP_CDP_ENDPOINT` を設定すると立場が逆転し、Playwright MCP は
「既にあるブラウザに接続する側」になります。そのブラウザを外に出したのが
`chrome-a` / `-b` / `-c`（`docker/chrome-novnc/`）です。

```
Computer ──MCP──▶ playwright-mcp-a ──CDP:9222──▶ chrome-a ──:6080──▶ 人間
```

構成は `docker-compose.yml` と `.env.example` 17b 節に入っています。

**既定では無効です。** チーム A で使う場合、`.env` に 3 つ設定します。

```bash
# ① 画面のパスワード（⚠️ チームごとに必ず違う値。理由は下記）
CHROME_NOVNC_VNC_PASSWORD_A=<チーム A のパスワード>

# ② 接続先の CDP（使うチームだけ）
PLAYWRIGHT_MCP_CDP_ENDPOINT_A=http://chrome-a:9222

# ③ ⚠️ 必ず同時に。片方だけだと「操作は成功するのに画面が更新されない」
PLAYWRIGHT_MCP_ISOLATED=false
```

```bash
# ⚠️ 素の `docker compose build` では chrome-* はビルドされません（profiles 対象外）。
#    名前を指定するか --profile novnc を付けてください。
docker compose build chrome-a
docker compose up -d chrome-a playwright-mcp-a
```

⚠️ **`chrome-a` を起動せずに ② だけ設定しないでください。** Playwright MCP は
Chromium を自前起動しなくなるため、ブラウザツールがまるごと使えなくなります。
`chrome-*` は `profiles: [novnc]` に入っており、素の `docker compose up -d` では
起動しません。

見るときは **http://localhost:6080/vnc.html**（b は 6081 / c は 6082）を開き、
そのチームのパスワードを入力します。

#### ビルドと起動のコマンド

`chrome-*` は `profiles: [novnc]` に入っているため、**素の `docker compose build` /
`up -d` の対象になりません**（実測。`--dry-run` で確認）。

| やりたいこと | コマンド |
| --- | --- |
| 3 台ぶんビルド | `docker compose --profile novnc build` |
| 1 台だけビルド | `docker compose build chrome-a` |
| 3 台まとめて起動 | `docker compose --profile novnc up -d` |
| 1 台だけ起動 | `docker compose up -d chrome-a` |

**名前を明示すれば `--profile` は要りません。** 3 台は同じ `image: svc/chrome-novnc:local`
を指すので、どれか 1 つビルドすれば 3 台とも新しいイメージになります
（`--profile novnc build` でも実体は 1 回のビルドです）。

⚠️ **イメージだけ更新しても既存コンテナは作り直されません。** compose が再作成を
判断するのは「compose の設定が変わったとき」で、イメージの中身の変化は見ていません。

```bash
docker compose build chrome-a
docker compose up -d --force-recreate chrome-a chrome-b chrome-c
```

#### ⚠️ Chrome は `--remote-debugging-address` を無視する（cdp-proxy が要る理由）

`docker/chrome-novnc/` に `cdp-proxy.js` がある理由です。**素直に組むと動きません。**

実測で分かったことが 3 段あります。

1. **この Chrome（152）は `--remote-debugging-address=0.0.0.0` を無視し、CDP を
   常に `127.0.0.1` にしか bind する。** headed / headless の双方で確認しました。
   他コンテナから叩くと `connect ECONNREFUSED chrome-a:9222` になります。
2. **単純な TCP 転送（socat）では越えられない。** Chrome は DNS リバインディング
   対策で Host ヘッダを検査し、localhost と IP アドレス以外を拒否します:

   ```
   HTTP 500  Host header is specified and is not an IP address or localhost.
   ```

3. **Host を直すだけでも足りない。** `/json/version` が返す
   `webSocketDebuggerUrl` は `ws://127.0.0.1:<port>/...` で、**Playwright は
   この URL をそのまま使います**。実測のエラー:

   ```
   <ws connecting> ws://127.0.0.1:9222/devtools/browser/... → ECONNREFUSED
   ```

`cdp-proxy.js` はこの 3 つをまとめて解きます。Chrome は内部ポート（既定 9221）で
localhost に待たせ、プロキシが 0.0.0.0:9222 で受けて Host を付け替え、応答本文の
内部アドレスを**リクエストの Host に差し替えて**返します。接続元が `chrome-a:9222`
で来れば `ws://chrome-a:9222/...` を返すので、接続先を設定に持つ必要がありません。

| ポート | 待ち受け | 誰が |
| ------ | -------- | ---- |
| 9221   | `127.0.0.1` | Chrome 本体 |
| 9222   | `0.0.0.0`   | cdp-proxy（外向き） |
| 5900   | `0.0.0.0`   | x11vnc |
| 6080   | `0.0.0.0`   | websockify（noVNC） |

#### エラー: `getaddrinfo EAI_AGAIN chrome-a`

**`chrome-a` が起動していません。** `PLAYWRIGHT_MCP_CDP_ENDPOINT_A` だけ設定して
コンテナを上げていない状態です。有効化には**設定 3 つとビルドが揃っている**必要があります。

現状の確認:

```bash
docker ps -a --filter name=svc-chrome-a          # コンテナの有無
docker images svc/chrome-novnc                   # イメージの有無
docker inspect svc-playwright-mcp-a \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E "CDP_ENDPOINT|ISOLATED"
```

**すぐ元に戻す**（noVNC をいったん諦めてエージェントを復旧させる）:

```bash
# .env の PLAYWRIGHT_MCP_CDP_ENDPOINT_A を空にする
docker compose up -d playwright-mcp-a     # 環境変数の変更を検知して自動で再作成される
```

**noVNC を完成させる**: 下の起動手順に戻り、3 つの設定と `docker compose build chrome-a` を揃えてください。

関連する症状の切り分け:

| 症状 | 原因 |
| --- | --- |
| `getaddrinfo EAI_AGAIN chrome-a` | `chrome-a` が起動していない |
| `connect ECONNREFUSED chrome-a:9222` | `chrome-a` は動いているが CDP が外に出ていない。cdp-proxy が落ちている（`docker logs svc-chrome-a` に `cdp-proxy:` の行があるか確認）|
| noVNC は開くが画面が真っ黒 / 更新されない | `PLAYWRIGHT_MCP_ISOLATED` が `true` のまま |
| `chrome-a` が起動直後に落ちる（restart ループ） | `CHROME_NOVNC_VNC_PASSWORD_<チーム>` が未設定（entrypoint が拒否） |
| `docker compose up -d` で chrome が上がらない | 仕様。`profiles: [novnc]` のため名前指定か `--profile novnc` が要る |

#### ⚠️ パスワードをチームで共有してはいけない

3 台は**同じホストの別ポート**に出ます。値を共有すると、A のパスワードで B と C の
画面が見られます。Computer と Playwright MCP をチームごとに分けている理由
（他チームの Computer と dev サーバに到達させない）が、この 1 点で無効になります。

そのうえで、VNC のパスワードは**共有シークレットであってユーザ認証ではありません**。

| | 効くか |
| --- | --- |
| チーム間の越境を防ぐ | ✓（値を分ければ） |
| チーム内で閲覧者を区別する | ✗ |
| 誰が見たかを記録する | ✗（`utils/audit.py` は cptr の受信 HTTP のみ） |
| Open WebUI / Computer のアカウントと連動する | ✗（無関係な第 4 の認証面） |

マルチユーザ全体での位置づけは `multi_user_setup.md` の「既知の限界」13 / 14 を参照。

#### ⚠️ 可視性とセッション分離はトレードオフ（両立しません）

Playwright MCP は**本来クライアント接続ごとにセッションを分けます**。ところが CDP 接続で
`--isolated` を外すと、その分離が消えます。実測（同一 MCP に 2 クライアント同時接続し、
別々の URL へ navigate してから互いに snapshot）:

| 構成                                      | セッション分離 | 画面が見える |
| ----------------------------------------- | :------------: | :----------: |
| 内部ブラウザ + `isolated=true`（従来）    |       ✓        |      ✗       |
| CDP + `isolated=true`                     |       ✓        |      ✗       |
| **CDP + `isolated=false`（noVNC 構成）**  |     **✗**      |    **✓**     |

分離が消えたときの症状は分かりにくいものです。クライアント B が `vite.dev` へ navigate
しても、**snapshot は A が開いた `example.com` を返します**（実測: A 522 文字 / B 522 文字。
分離時は B が 32,223 文字）。エラーは出ません。

**現状の cptr では実害がありません。** `stdio_manager` が `server_id` だけをキーに
クライアントを使い回すため（`self._instances: dict[str, MCPClient]`）、そもそも
Computer コンテナにつき MCP クライアントは 1 本で、セッションは元から 1 つだからです
（既知の限界 10）。

**ただし将来の逃げ道を 1 つ塞ぎます。** 「ユーザごとにセッションを分ける」を実現する
自然な道筋は Playwright MCP のこの機能に乗ることですが、noVNC 構成ではその層が
使えません。ユーザ単位の分離が必要になったら、Computer ごと分ける（限界 5 の解）か、
noVNC を諦めるかの二択になります。

#### ⚠️ プロファイルが永続化される（挙動の変更）

従来の `PLAYWRIGHT_MCP_ISOLATED=true` は「プロファイルをディスクに残さない」設定でした。
CDP 構成では false 固定になり、プロファイルは `chrome-*-profile` ボリュームに残ります。

**あるユーザがエージェントに行わせたログインは、同じチームの別ユーザのエージェントからも、
コンテナ再起動後もそのまま使えます。** 便利ですが、資格情報がチーム内で共有される
ことは認識してください。消すには `docker compose down` 後に
`docker volume rm <project>_chrome-a-profile`。

#### ⚠️ `PLAYWRIGHT_MCP_ISOLATED` は false でなければならない

**これが最大の落とし穴です。** CDP 接続と `--isolated` を併用すると、Playwright は
新しい BrowserContext を作り、それが X ディスプレイ上の可視ウィンドウになりません。

同一手順で `--isolated` の有無だけを変えた対照実験（画面を xwd で取得し gzip したサイズ）:

| 条件               | 遷移前 | 遷移後  | 判定           |
| ------------------ | -----: | ------: | -------------- |
| `--isolated` あり  | 26,808 |  26,808 | **映らない**   |
| `--isolated` なし  | 26,808 | 176,931 | 映る           |

**ツール呼び出しは成功し、`browser_snapshot` も正しい内容を返します。**
壊れるのは画面だけなので、設定を疑う手がかりが出ません。compose では false 固定です。

#### 見るだけにしてある

`VNC_VIEW_ONLY` の既定は `true` です。人間が同じウィンドウを操作すると、エージェントが
掴んだ要素が動いてツール呼び出しが不可解に失敗します。介入したい場合だけ false にしてください。

#### 性能チューニング（実測の記録）

「重い」と感じたときに、**同じ道を二度調べないため**の記録です。すべて
1280x720・常時アニメするページ・Raw エンコードでの測定です。

| 試したこと | 結果 | 採否 |
| --- | --- | --- |
| Chrome の What's New タブを抑止 | **CPU 120% → 17%** | **採用**（最大の効果） |
| メモリ上限 1G → 2G | 実サイト 1 タブで 930〜978MiB / 1GiB（96%）に達していた | **採用** |
| x11vnc `-defer 20 → 5` / `-wait 20 → 5` | **36.4 → 52.6 件/秒（+44%）**、x11vnc の CPU は 0.1% で不変 | **採用** |
| x11vnc `-noxdamage` を外す | 35.4 → 35.2 件/秒（**差なし**） | 外した（無効化する理由が無いため。効果は無い） |
| 解像度 1280x720 → 1024x576 | 35.5 → 35.0 件/秒、帯域 −13%、Chrome CPU はむしろ +3 ポイント | **不採用**（効果なし） |
| `--disable-gpu` | **測定できず**（下記） | **不採用** |

⚠️ **`--disable-gpu` を「最適化」として足さないでください。** 一度は
「CPU 31.9% → 5.0%」という結果が出ましたが、**対照実験も同じように固まった**ため
測定系の不具合と判明しました（Chrome を再起動するとウィンドウが非可視扱いになり
`requestAnimationFrame` が絞られる）。GPU プロセスは実測 11.9% を使いますが、
止めるとソフトウェア描画がどこへ移るのか確認できていません。**未検証です。**

現状（実サイト 1 タブ・アイドル時）は CPU 1.8〜3.3%、メモリ 1.25〜1.47GiB / 2GiB
です。**ここから先の体感遅延は構造的なもの**で、内訳は次の 3 つです。

1. GPU が無くソフトウェア描画になる（コンテナでは解消できない）
2. ページ自体の重さ（Amazon のような商用サイト）
3. ホストの CPU 4 コアを全サービスで分け合っている

3 は効きます。関係の無いコンテナ（別プロジェクトの CI など）を止めると変わります。
実際、負荷が高かったときの load average は 4.16 / 6.26 / 15.38 でした。

#### コスト

Chrome が 3 台増えます。既存の見積り（Computer 3 台 9G + Playwright 3 台 3G = 12G / 9 CPU）に
**3G / 3 CPU が加算**され、ホスト 4 コアでは 3 チーム同時は成立しません。
使うチームだけ起動する運用（`docker compose up -d chrome-a`）を勧めます。

#### 安全側の既定

- **CDP ポート（9222）はホストに公開していません。** CDP に認証は無く、到達できれば
  任意のページを開かせ Cookie も読めます。公開するのはパスワード付きの noVNC だけです
- `CHROME_NOVNC_VNC_PASSWORD` は未設定だと**起動を拒否**します（compose の `:?`）
- ただし `svc-playwright-net-*` に居る Computer からは CDP に直接到達できます。
  エージェントと同じ信頼境界なので許容していますが、**MCP を経由しないブラウザ操作が
  可能**である点は認識してください

#### これが要るかどうか

**まず要らないと考えてください。** 画面が必要になるのは「なぜクリックが効かないか
分からない」ような視覚固有の不具合に限られます。それ以外は次のほうが速く、安いです。

| 知りたいこと           | 使うもの                                        |
| ---------------------- | ----------------------------------------------- |
| 要素の状態・`ref`      | `browser_find`（snapshot の 2.4%）              |
| JS エラー              | `browser_console_messages`                      |
| 通信の失敗             | `browser_network_requests`                      |
| 見た目そのもの         | `browser_take_screenshot`（ファイルに残ります） |


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

**先にモデル経路を確認してください。** ここが `agent:codex/...` だと、
登録が正しくてもツールは 1 つも生えません（§3 参照）:

```bash
cat /workspace/<name>/.cptr/model     # 空 or OpenAI のモデル ID なら OK
```

チャットからは次の順で確認するのが確実です:

1. `http://open-webui-computer-a:5173` を開いて — `browser_navigate`
2. 何が見える? — `browser_snapshot`（`about:blank` が返ったら Type が HTTP になっています）
3. コンソールエラーは? — `browser_console_messages`
