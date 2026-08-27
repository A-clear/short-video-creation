# Codex（coding agent subscription）セットアップ手順

既に払っている **ChatGPT のサブスクリプション**を、API キー課金を挟まずにチャットのバックエンドとして使うための手順。

**所要時間**: 15〜20 分（ChatGPT アカウントが用意済みの場合。3 台分のログインを含めるとさらに 10 分程度）

**前提**:

- ChatGPT のサブスクリプション（Plus / Pro / Business など、Codex を含むプラン）
- `docker-compose.yml` の `open-webui-computer-a` / `-b` / `-c`（チーム別 3 台）をビルド・起動できる状態
- Computer の admin アカウント作成済み（`docs/Setup/multi_user_setup.md` の Computer 初回セットアップ、または `.env.example` の 15 節）
- ホストから `github.com` へ HTTPS で出られること（CLI の取得に使う）

> ⚠️ **Open WebUI Computer はマルチユーザ化に伴いチーム別 3 台構成（`open-webui-computer-a` / `-b` / `-c`）になりました。** 本手順書のコマンドは**すべて 3 台分**実行する必要があります。以降、`<team>` は `a` / `b` / `c` のいずれかに読み替えてください（例: `open-webui-computer-<team>` は `open-webui-computer-a` / `open-webui-computer-b` / `open-webui-computer-c` の 3 通り）。3 台構成の全体像は `docs/Setup/multi_user_setup.md` を参照。

---

## この機能がどこに属するか

**Open WebUI 本体の機能ではなく、Open WebUI Computer（cptr）の機能。** 設定場所も Computer の管理画面で、Open WebUI 側の環境変数には対応する項目が無い。

```
ブラウザ ──► Open WebUI (:8080)
                │  モデルピッカーで cptr/<workspace> を選ぶ
                │  OpenAI 互換 gateway（open-webui-computer-a/-b/-c への 3 本の接続）
                ▼
             Computer-<team> (:8001 / :8002 / :8003)
                │  実際に動くモデルを決める
                ▼
             codex CLI (/usr/local/bin/codex)
                │  ~/.codex/auth.json のログインを使う（チームごとに独立）
                ▼
             ChatGPT サブスクリプション（3 チーム共通のレート上限）
```

Open WebUI から見えるのは `cptr/<workspace>` というモデルだけで、その裏で Codex が動いているかどうかは見えない。**どちらが動くかは Computer 側の設定で決まる**（後述のステップ 5）。

---

## 全体の流れ

```
1. イメージをビルドする              ← codex CLI もここで入る
2. Computer を起動する
3. Codex にログインする              ← デバイスコード方式。1 回だけ
4. Agents プロファイルを登録する      ← 管理者が 1 回
5. ワークスペースに Codex を割り当てる ← ここが Open WebUI との接続点
6. 動作確認
```

ステップ 5 が中核。**4 まで終わっても Open WebUI 側からは Codex が使われない。**

---

## 1. イメージをビルドする

```bash
docker compose build
```

`open-webui-computer-a` / `-b` / `-c` は公式イメージをそのまま使わず、`docker/computer/Dockerfile` でビルドする。3 台とも同じ `image: svc/open-webui-computer:local` タグを指すため、サービス名を指定しない `docker compose build` だけでよい（実質 1 回のビルドで 3 台分に反映される）。codex CLI はここでイメージへ焼き込まれる。

> Computer だけをビルドしたい場合は `docker compose build open-webui-computer-a` と指定する。サービス名を省くと Docling（`docker/docling/Dockerfile`。日本語 OCR の言語パック追加）も一緒にビルドされる。どちらも冪等なので、まとめて流して問題ない。

> **なぜビルドが必要なのか**
>
> 公式イメージ `ghcr.io/open-webui/computer:latest` は意図的に最小構成で、入っているのは git / openssl / tar / Python 3.12（+ pip, uv）だけ。node も npm も curl も gcc も無い。そして非 root ユーザ `cptr`（uid 1000）で動き、`sudo` も無い。
>
> **apt パッケージは実行中のコンテナには足せない。** だから「起動時にインストールする」方式が取れず、イメージを作ることになる。

同時に、Python / TypeScript 開発のためのツールチェーンも入る。

| 分類   | 内容                                                                                                                                     |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| apt    | `build-essential` / `curl` / `wget` / `jq` / `ripgrep` / `git-lfs` / `openssh-client` / `unzip` / `xz-utils` / `less` / `vim` / `procps` |
| Node   | `node` / `npm` / `npx` / `corepack`（Node 22）                                                                                           |
| npm -g | `pnpm` / `typescript` / `tsx`                                                                                                            |
| Codex  | `/usr/local/bin/codex`                                                                                                                   |
| 元から | `git` / `python3.12` / `pip` / `uv` / `openssl` / `tar`                                                                                  |

ビルドの最後に自己検証が走る。ここで落ちたらイメージは作られないので、壊れたイメージが起動することはない。

### バージョンを上げる / ARM ホストで動かす

`.env` の値を変えて **再ビルド**する。

```bash
docker compose build
docker compose up -d open-webui-computer-a open-webui-computer-b open-webui-computer-c
```

| 変数                        | 既定値                               | 備考                                                       |
| --------------------------- | ------------------------------------ | ---------------------------------------------------------- |
| `CODEX_CLI_VERSION`         | `rust-v0.149.1`                      | [リリースのタグ](https://github.com/openai/codex/releases) |
| `CODEX_CLI_TARGET`          | `x86_64-unknown-linux-musl`          | ARM ホストでは `aarch64-unknown-linux-musl`                |
| `NODE_VERSION`              | `22`                                 | ⚠️ 下記の理由で 22 固定                                    |
| `PNPM_VERSION`              | `11.24.0`                            |                                                            |
| `TYPESCRIPT_VERSION`        | `7.0.2`                              |                                                            |
| `TSX_VERSION`               | `4.23.12`                            |                                                            |
| `OPEN_WEBUI_COMPUTER_IMAGE` | `ghcr.io/open-webui/computer:latest` | `:dev` にすると main 追従                                  |

> **Node を 24 に上げないこと**
>
> `full-stack/open-webui` の `engines` が `>=18.13.0 <=22.x.x`。24 以上にすると submodule のフロントエンドがインストールできなくなる。Node 22 は Maintenance LTS（EOL 2027-04-30）で、Active LTS は 24。ここは「新しさ」ではなくリポジトリの制約を優先している。

> **pnpm を corepack で入れていない理由**
>
> corepack のシムは初回実行時にネットワークから pnpm 本体を取りに行く。「ビルドは通ったのに実行時に落ちる」を避けるため、`npm install -g pnpm@<version>` でイメージ内に固定している。`corepack` 自体もコマンドとしては使えるので、`packageManager` フィールドを持つプロジェクトではそちらも選べる。

> **`codex-package-*` を使う理由**
>
> 単体バイナリの `codex-*.tar.gz` ではなく `codex-package-*.tar.gz` を展開している。後者には `bin/codex` に加えて `bin/codex-code-mode-host` / `codex-path/rg` / `codex-resources/{bwrap,zsh}` が同梱され、codex はこれらを自分の実行ファイルからの相対パスで解決する。**レイアウトを崩すと該当機能が黙って落ちる**ので、丸ごと展開する。
>
> `/usr/local/bin/codex` は `/opt/codex/bin/codex` への symlink だが、codex は `/proc/self/exe` で実体パスを解決するため resources は正しく見つかる（検証済み）。

---

## 2. Computer を起動する

```bash
docker compose up -d open-webui-computer-a open-webui-computer-b open-webui-computer-c
```

⚠️ `docker compose up -d` だけでは既存イメージが再利用される。Dockerfile や `.env` のバージョンを変えたら、必ず `docker compose build` を先に流すこと。

ツールチェーンが入ったか確認する。**3 台とも同じイメージなので、確認は代表 1 台（例: `-a`）で十分。**

```bash
docker compose exec open-webui-computer-a sh -c 'node -v; pnpm -v; tsc -v; codex --version'
# => v22.23.2
#    11.24.0
#    Version 7.0.2
#    codex-cli 0.149.1
```

---

## 3. Codex にログインする

**コンテナにはブラウザが無いので、デバイスコード方式を使う。**

> ⚠️ **3 台それぞれでログインが必要。** `codex login` の認証情報はコンテナ（チーム）ごとに独立した `codex-home-<team>` ボリュームに保存されるため、`open-webui-computer-a` でログインしても `-b` / `-c` には引き継がれない。**同じ ChatGPT アカウントで 3 回 `codex login --device-auth` する運用になる。** サブスクリプションのレート上限は ChatGPT アカウント単位、つまり **3 チームで共有**される点に注意（1 チームが使い切ると他のチームにも影響する）。

```bash
docker compose exec -it open-webui-computer-<team> codex login --device-auth
```

`<team>` を `a` / `b` / `c` に変えて 3 回実行する。

表示されたコードを、手元の PC やスマートフォンのブラウザで承認する。

確認:

```bash
docker compose exec open-webui-computer-<team> codex login status
```

> **`--device-auth` を付け忘れないこと**
>
> 素の `codex login` はローカルの **1455 番ポート**に OAuth のコールバックを待ち受ける。手元のブラウザの `localhost:1455` はコンテナの中には届かないため、コンテナでは完了しない。

> **ログインは永続化されている（チームごとに）**
>
> 認証情報は `CODEX_HOME`（既定 `~/.codex`）の `auth.json` に入る。`docker-compose.yml` はこのパスに `codex-home-a` / `codex-home-b` / `codex-home-c` ボリュームを被せているので、コンテナを作り直しても再ログインは不要。**逆にそのチームの `codex-home-<team>` ボリュームを消すと、そのチームだけ再ログインになる**（他のチームには影響しない）。

### サブスクリプションではなく API キーで使いたい場合

```bash
docker compose exec -T open-webui-computer-<team> \
  sh -c 'printenv OPENAI_API_KEY | codex login --with-api-key'
```

ただしこれは従量課金であり、本手順書の目的（サブスクリプションの活用）からは外れる。

---

## 4. Agents プロファイルを登録する

Computer（`http://localhost:8001` / `:8002` / `:8003` — チームごと）で **Settings → Admin → Agents** を開き、プロファイルを追加する。**管理画面の設定なので、3 台それぞれで行う必要がある。**

| フィールド        | 値                      |
| ----------------- | ----------------------- |
| **Name**          | `Codex`                 |
| **Type**          | `Codex`                 |
| **Profile ID**    | `codex`                 |
| **Command**       | `/usr/local/bin/codex`  |
| **Home**          | （空のまま）            |
| **Approval mode** | `auto`                  |
| **Sandbox**       | `danger-full-access`    |
| **Models**        | （空のまま = 自動検出） |

**Sandbox は `danger-full-access` にする。** これを外すと `bwrap: No permissions to create a new namespace` でシェル実行が全部失敗する。理由は後述の「サンドボックスはコンテナ内では動かない」を参照。**なお `docker/computer/Dockerfile` のパッチが当たっていないとこの設定自体が無視される。**

**Command は絶対パスで指定する。** `codex` は `PATH` にも載っている（`/usr/local/bin/codex`）ので名前だけでも通るが、Computer がどのシェル環境でプロファイルを検出するかに依存しないよう、絶対パスにしておく。

**Home は空のままにする。** これは「既定と別の設定 / ログインディレクトリ」を指すフィールドで、別アカウントを使い分けたいときのもの。既定の `~/.codex` を `codex-home-<team>` ボリュームで永続化済みなので指定不要。

### Status の読み方

| Status               | 意味                       | 対処                                                           |
| -------------------- | -------------------------- | -------------------------------------------------------------- |
| `ready`              | 正常                       | モデルを選んで使える                                           |
| `not found`          | Command が見つからない     | 絶対パスか確認。ステップ 1 が失敗していないか確認              |
| `auth unknown`       | ログイン状態が確認できない | ステップ 3 をやり直す                                          |
| `missing dependency` | 依存が足りない             | Codex では通常起きない（Claude Code の `claude-agent-sdk` 用） |

**検出結果は約 30 秒キャッシュされる。** 直したあと少し待ってから再確認する。

---

## 5. ワークスペースに Codex を割り当てる

**ここを飛ばすと、Open WebUI から `cptr/<workspace>` を選んでも Codex は動かない。**

Computer が「実際に動かすモデル」を決める優先順:

```
1. Settings → Admin → Gateway のモデル      ← 全ワークスペースに効く
2. <workspace>/.cptr/model                  ← ワークスペース単位  ★これを使う
3. 既定のチャットモデル
4. 最初の有効な接続の最初のモデル
```

本プロジェクトは **2 を使う**。ワークスペースごとに Codex と OpenAI API を使い分けられるため。

### 5-1. ワークスペースを作る

**ディレクトリを掘っただけではワークスペースにならない。** Computer に登録して初めて `cptr/<フォルダ名>` として gateway に出てくる。

Computer（`http://localhost:8001` / `:8002` / `:8003` — 対象チーム）のサイドバーで**ワークスペースピッカー**を開き、絶対パス `/workspace/<name>` を追加する。**ピッカーの中で新規フォルダを作れる**ので、ターミナルは要らない。ワークスペースはチームのコンテナ単位（`/workspace` は `computer-<team>-workspace` ボリューム）なので、そのワークスペースを使いたいチームの Computer で行う。

> **パスはコンテナ内のパス**
>
> ピッカーが受け取るのは「`cptr` が動いているマシンのパス」であり、ブラウザを開いている PC のパスではない。Docker で動かしている本構成では `/workspace/...` というコンテナ内のパスになる。

### 5-2. `.cptr/model` を書く

```bash
docker compose exec open-webui-computer-<team> \
  sh -c 'mkdir -p /workspace/<name>/.cptr && echo "agent:codex/gpt-5.4" > /workspace/<name>/.cptr/model'
```

> **`Permission denied` になったら**
>
> `/workspace` が root 所有になっている。ベースイメージに `/workspace` が存在せず（`VOLUME` 宣言は `/data` だけ）、**イメージに無いパスへ名前付きボリュームを張ると Docker が root 所有でディレクトリを作る**ため。5-1 のピッカーからのフォルダ作成も、コンテナ内では `cptr` として実行されるので同じ理由で失敗する。
>
> 現在の所有者はこれで確認できる（`1000 1000` が正しい）。
>
> ```bash
> docker compose exec open-webui-computer-<team> ls -lnd /workspace
> ```
>
> `docker/computer/Dockerfile` が `/workspace` を cptr 所有で作るので、**再ビルドして作り直せば直る**（ボリュームが空であれば、マウント時にイメージ側の所有者が反映される）。対象チームだけでよい。
>
> ```bash
> docker compose build
> docker compose up -d --force-recreate open-webui-computer-<team>
> ```
>
> 既にワークスペースを作ってしまっていてボリュームを空にできない場合は、所有者だけを直す。
>
> ```bash
> docker compose run --rm --user root --entrypoint sh open-webui-computer-<team> \
>   -c 'chown -R cptr:cptr /workspace'
> ```

結果として、Open WebUI のモデルピッカーはこうなる。

| モデル            | 実際に動くもの                      |
| ----------------- | ----------------------------------- |
| `cptr/<name>`     | Codex（ChatGPT サブスクリプション） |
| `cptr/<別のname>` | `gpt-5.4-2026-03-05`（OpenAI API）  |

> **モデル ID は API の ID とは別物**
>
> `agent:codex/gpt-5.4` は有効だが、**`agent:codex/gpt-5.4-2026-03-05` は存在しない**。codex 側は日付なしのスラッグを使う。実際に選べる一覧はこれで確認する。
>
> ```bash
> docker compose exec open-webui-computer-<team> codex debug models
> ```
>
> 参考（v0.149.1 時点）: `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.5` / `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.2` / `codex-auto-review`

Gateway のモデル（優先順 1）は**空のままにしておくこと。** ここに値を入れると全ワークスペースを上書きしてしまい、使い分けができなくなる。

---

## 6. 動作確認

> **先に Open WebUI 側の「API Type」を確認する**
>
> Open WebUI の **Admin Settings → Connections** で Computer の接続を開き、**API Type** が **Chat Completions** になっていること。`Responses` になっていると `Method Not Allowed` で失敗する。**接続は 3 本**（`http://open-webui-computer-a:8000/v1` / `-b` / `-c`）あり、それぞれ個別に確認・設定する必要がある。
>
> Computer の gateway が実装しているのは次の 2 つだけで、Responses API は持っていない。
>
> | エンドポイント              | 用途                                     |
> | --------------------------- | ---------------------------------------- |
> | `GET /v1/models`            | ワークスペースをモデルとして列挙         |
> | `POST /v1/chat/completions` | ワークスペースでエージェントタスクを実行 |
>
> `Responses` にすると Open WebUI は `POST /v1/responses` を叩く（`backend/open_webui/routers/openai.py:1284` の `api_config.get('api_type') == 'responses'` で分岐）。gateway にそのルートは無いので 405 が返る。
>
> **添字 0 の OpenAI 本体は `Responses` のままでよい。** これは接続ごとの設定なので、揃える必要はない。

1. Open WebUI（`http://localhost:8080`）を開く
2. モデルピッカーで `cptr/<name>` を選ぶ
3. 「このリポジトリのファイル構成を教えて」など、ファイルを読ませる質問を投げる
4. `<name>` を作ったチームの Computer（`http://localhost:8001` / `:8002` / `:8003`）を開き、同じ会話がサイドバーに出ていることを確認する

gateway 経由の会話は Computer 側の実際のチャットとして残るので、**エージェントが何をしたかはここで全部見られる。**

---

## 注意点

### 承認プロンプトは出ない

Open WebUI から gateway 経由で呼ばれた場合、**ファイル編集もコマンド実行も全ツール自動承認**で走る。Open WebUI 側に「Allow / Deny」を出す仕組みが無いため。

1 つずつ承認したい場合は、Open WebUI ではなく **Computer の UI から直接使う**。Computer のチャットモードは Codex の approval policy と sandbox にマッピングされ、`/plan` でプランモードにもできる。

### 境界はマウントしたディレクトリだけ

Codex はコンテナの中で動くので、触れるのはそのチームの `open-webui-computer-<team>` にマウントした範囲だけ（他チームのコンテナ・ボリュームには触れない）。ホストの実プロジェクトを触らせたい場合は、`docker-compose.yml` の該当チームのサービス（`open-webui-computer-a` 等）の `volumes:` に bind mount を追記する（例: `- ./docs:/workspace/docs`）。3 台化の書き直しでコメントアウト済みのひな形は削除されているため、現状は都度追記する必要がある。**書き込み可でマウントする範囲は「エージェントに勝手に触られてよい」ものだけにする。**

### サンドボックスはコンテナ内では動かない

codex の Linux サンドボックスは **bubblewrap（bwrap）** で、非特権ユーザ名前空間を必要とする。コンテナ内ではこれを作れず、シェル実行がこうなる。

```
bwrap: No permissions to create a new namespace
```

**Docker 側を緩めても直らない。** 実測した結果:

| 設定                              | 結果                                                    |
| --------------------------------- | ------------------------------------------------------- |
| 既定                              | `No permissions to create a new namespace`              |
| `seccomp=unconfined`              | `Failed to make / slave: Permission denied`             |
| `apparmor=unconfined`             | `No permissions to create a new namespace`              |
| `seccomp` + `apparmor` unconfined | `loopback: Failed RTM_NEWADDR: Operation not permitted` |
| `--privileged`                    | `loopback: Failed RTM_NEWADDR: Operation not permitted` |

`--privileged` でも通らないので、コンテナの防御を落とす価値はない。`features.use_legacy_landlock=true` での回避も試したが、`permission profiles requiring direct runtime enforcement are incompatible with --use-legacy-landlock` で `workspace-write` には使えなかった。

**したがって Sandbox は `danger-full-access` にする。** これで実効的な安全性は下がらない。Computer 自身の `run_command` はもともとサンドボックスを使っておらず（cptr のパッケージ全体に `bwrap` / `landlock` / `seccomp` / `unshare` の参照が 1 つも無い）、境界は最初からコンテナそのものだからである。承認の挙動（`approvalPolicy`）も変わらないので、`ask` モードでは引き続き 1 コマンドずつ承認を求める。

> **cptr のバグに対するパッチが要る**
>
> cptr は Codex プロファイルの `sandbox_mode` を `utils/agents/models.py` で検証し、管理画面にも **Sandbox** フィールドを出す。**ところが `utils/agents/codex.py` はこの値を読んでいない。** チャットモードから計算した値（`read-only` か `workspace-write`）を app-server へ送るだけで、**UI の設定が効かない**。
>
> gateway 経由は `tool_approval_mode=full` だが、`full` も `workspace-write` になる（`routers/gateway.py:746` → `codex.py:225-228`）ため、どのモードでも必ず bwrap を通る。
>
> `docker/computer/Dockerfile` の「4. cptr のパッチ」がこの 1 行を直し、プロファイルの値を優先させている。upstream が該当行を変えたらビルドがその場で失敗するようにしてあるので、黙って無効化されることはない。

### サブスクリプションの利用上限

サブスクリプション経由の利用にはプランごとのレート上限がある。上限に当たると Codex 側がエラーを返し、Open WebUI にはそのまま伝わる。**恒常的に上限へ当たるようなら、そのワークスペースは `.cptr/model` を外して OpenAI API 接続（`gpt-5.4-2026-03-05`）に戻す**のが素直。

---

## トラブルシューティング

| 症状                                                           | 原因                                             | 対処                                                                                                                                                                                                                         |
| -------------------------------------------------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ビルドが codex の取得で失敗する                                | GitHub へ出られない / タグ名が誤り               | `CODEX_CLI_VERSION` を確認して `docker compose build` を再実行                                                                                                                                                               |
| node / pnpm / curl が入っていない                              | ビルドせずに `up -d` した                        | `docker compose build` してから `up -d`                                                                                                                                                                                      |
| プロファイルが `not found`                                     | Command が相対パス / イメージが古い              | `/usr/local/bin/codex` と絶対パスで指定。`docker compose build` を流したか確認                                                                                                                                               |
| `codex login` が完了しない                                     | `--device-auth` を付けていない                   | `--device-auth` を付ける（1455 番のコールバックはコンテナに届かない）                                                                                                                                                        |
| 再起動のたびにログインを求められる                             | `codex-home-<team>` ボリュームが消えている       | `docker volume ls` で確認。消えていれば該当チームだけ再ログイン                                                                                                                                                              |
| `codex login` が Permission denied                             | `codex-home-<team>` が root 所有（旧構成の残骸） | `docker volume rm short-video-creation_codex-home-<team>` して作り直す。イメージ側に cptr 所有のディレクトリを用意済み                                                                                                       |
| `mkdir /workspace/<name>` が Permission denied                 | `/workspace` が root 所有                        | `docker compose build` → `up -d --force-recreate open-webui-computer-<team>`。ワークスペース作成済みなら `docker compose run --rm --user root --entrypoint sh open-webui-computer-<team> -c 'chown -R cptr:cptr /workspace'` |
| ピッカーからフォルダを作れない                                 | 同上（UI の操作も cptr として走る）              | 同上                                                                                                                                                                                                                         |
| Open WebUI から使っても Codex が動かない                       | ステップ 5 をやっていない                        | `<workspace>/.cptr/model` を書く                                                                                                                                                                                             |
| ワークスペースを分けても全部 Codex になる                      | Gateway のモデルが設定されている                 | Settings → Admin → Gateway のモデルを空にする                                                                                                                                                                                |
| モデルを選ぶと `Method Not Allowed`                            | 接続の API Type が `Responses`                   | Admin Settings → Connections で Computer の接続を開き、API Type を `Chat Completions` に切り替える                                                                                                                           |
| シェル実行が `bwrap: No permissions to create a new namespace` | codex のサンドボックスがコンテナ内で動かない     | Agents プロファイルの **Sandbox** を `danger-full-access` にする。効かない場合は `docker compose build` でパッチを当て直す                                                                                                   |
| `agent:codex/gpt-5.4-2026-03-05` が見つからない                | API の ID を書いている                           | `codex debug models` の一覧から選ぶ（日付なし）                                                                                                                                                                              |

---

## 関連

- `docker/computer/Dockerfile` — 開発ツールチェーンと codex の導入手順（実装の正本）
- `.env.example` 16 節 — ビルド引数と手順の要約
- `docker-compose.yml` の `open-webui-computer-a` / `-b` / `-c` — 実際の配線
- `docs/Setup/multi_user_setup.md` — チーム別 3 台化を含むマルチユーザ構成の全体像
- [Use your coding agent subscription](https://docs.openwebui.com/ecosystem/computer/ai/coding-agents) — Open WebUI 公式
- [Codex Authentication](https://developers.openai.com/codex/auth) — OpenAI 公式
