# syntax=docker/dockerfile:1
# ==============================================================================
# Open WebUI Computer — Python / TypeScript 開発環境イメージ
# ==============================================================================
#
# 公式イメージ ghcr.io/open-webui/computer:latest は意図的に最小構成で、
# 入っているのは git / openssl / tar / Python 3.12（+ pip, uv）だけです。
# node も npm も curl も gcc も無く、非 root ユーザ cptr（uid 1000）で動きます。
#
# エージェントに実際の開発をさせる面である以上それでは足りないため、
# ここで開発ツールチェーンを足します。
#
# ⚠️ apt パッケージは実行中のコンテナには足せません（cptr は非 root で sudo も
#    無い）。だからこのイメージが必要になります。
#
# ビルド:
#   docker compose build
#   （サービス名は省略可。open-webui-computer-a / -b / -c の 3 台とも同じ
#    image: svc/open-webui-computer:local タグを指すため、実質 1 回のビルドになる）
#
# 検証:
#   docker compose run --rm --entrypoint sh open-webui-computer-a -c 'node -v; pnpm -v'
# ==============================================================================

ARG COMPUTER_IMAGE=ghcr.io/open-webui/computer:latest
ARG NODE_VERSION=22

# ------------------------------------------------------------------------------
# Node.js を公式イメージから借りるだけのステージ
# ------------------------------------------------------------------------------
# ⚠️ computer イメージも node イメージも同じ Debian 12 (bookworm) がベースです。
#    glibc が一致するので、tarball を curl で取ってくる必要はなく、
#    公式ビルドのバイナリをそのままコピーできます。
#    ベースが変わったら（例: upstream が Alpine 化）ここが壊れるので、
#    そのときは tarball 方式に切り替えてください。
FROM node:${NODE_VERSION}-bookworm-slim AS node

# ------------------------------------------------------------------------------
# 本体
# ------------------------------------------------------------------------------
FROM ${COMPUTER_IMAGE}

# ⚠️ ARG は FROM をまたぐと消えるため、ここで宣言し直します。
ARG CODEX_CLI_VERSION=rust-v0.149.1
ARG CODEX_CLI_TARGET=x86_64-unknown-linux-musl
ARG PNPM_VERSION=11.24.0
ARG TYPESCRIPT_VERSION=7.0.2
ARG TSX_VERSION=4.23.12
# stdio ↔ Streamable HTTP を変換する薄いブリッジ。Playwright MCP を
# Computer から使うために必要です（理由は npm install -g のコメント参照）。
ARG MCP_REMOTE_VERSION=0.6.0

# ⚠️ ENV ではなく ARG にしています。ENV にすると実行時のコンテナにも
#    DEBIAN_FRONTEND=noninteractive が残り、後から誰かが apt を叩いたときの
#    挙動を静かに変えてしまいます。
ARG DEBIAN_FRONTEND=noninteractive

USER root

# ------------------------------------------------------------------------------
# 1. apt パッケージ
# ------------------------------------------------------------------------------
# ⚠️ イメージには apt のインデックスが残っていない（/var/lib/apt/lists が空）ため
#    update が必須です。
#
#   build-essential  … node-gyp を使う npm パッケージと、wheel が無い Python
#                      パッケージのビルドに必要。Python のヘッダは
#                      /usr/local/include/python3.12 に既にあるので、
#                      これを足すだけで C 拡張がビルドできるようになります
#   procps           … ps。プロセスの確認に要る（既定では入っていません）
#   ripgrep          … codex / エージェントが既定で使う検索コマンド
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git-lfs \
        jq \
        less \
        openssh-client \
        procps \
        ripgrep \
        unzip \
        vim \
        wget \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------------------
# 2. Node.js / npm / pnpm / TypeScript / mcp-remote
# ------------------------------------------------------------------------------
# ⚠️ Node は 22 系で固定しています。open-webui submodule の engines が
#    `>=18.13.0 <=22.x.x` なので、24 以上にすると submodule のフロントエンドが
#    インストールできなくなります（CLAUDE.md のバージョン制約を参照）。
#    Node 22 は Maintenance LTS（EOL 2027-04-30）。
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

# ⚠️ pnpm は corepack ではなく npm でグローバル導入しています。
#    corepack のシムは初回実行時にネットワークから pnpm を取りに行くため、
#    「ビルドは通ったのに実行時に落ちる」が起きます。npm で入れておけば
#    イメージの中で完結します。
#    packageManager フィールドを持つプロジェクトでは corepack も使えます
#    （corepack のシムも下で用意しています）。
# ⚠️ mcp-remote も同じ理由でイメージに焼き込みます。Playwright MCP は
#    別コンテナで Streamable HTTP を喋りますが、cptr の `type: mcp` 経路は
#    ツール呼び出しのたびに接続し直すため（utils/tools.py:2931-2940）、
#    browser_navigate の次の browser_snapshot が about:blank になります。
#    セッションを保つには長寿命プロセスを使う `mcp_stdio` 経路が要り、
#    その stdio ↔ HTTP 変換が mcp-remote です。npx 実行にすると初回の
#    ツール呼び出しでネットワーク取得が走るため、ここで入れておきます。
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && npm install -g --no-fund --no-audit \
        "pnpm@${PNPM_VERSION}" \
        "typescript@${TYPESCRIPT_VERSION}" \
        "tsx@${TSX_VERSION}" \
        "mcp-remote@${MCP_REMOTE_VERSION}" \
    && npm cache clean --force

# ------------------------------------------------------------------------------
# 3. codex CLI（coding agent subscription）
# ------------------------------------------------------------------------------
# ⚠️ 単体バイナリの codex-*.tar.gz ではなく codex-package-* を使います。
#    後者には bin/codex に加えて bin/codex-code-mode-host / codex-path/rg /
#    codex-resources/{bwrap,zsh} が同梱され、codex はこれらを自分の実行ファイル
#    からの相対パスで解決します。レイアウトを崩すと機能が黙って落ちます。
#
# ⚠️ tarball の中身は uid 1001（GitHub Actions の runner）所有で展開されるため、
#    chown / chmod を通さないと cptr（uid 1000）が実行できません。
#
# ⚠️ /usr/local/bin/codex は symlink ですが、codex は /proc/self/exe で
#    実体パスを解決するので resources は正しく見つかります（検証済み）。
ADD https://github.com/openai/codex/releases/download/${CODEX_CLI_VERSION}/codex-package-${CODEX_CLI_TARGET}.tar.gz /tmp/codex.tar.gz
RUN mkdir -p /opt/codex \
    && tar xzf /tmp/codex.tar.gz -C /opt/codex \
    && rm -f /tmp/codex.tar.gz \
    && chown -R root:root /opt/codex \
    && chmod -R a+rX /opt/codex \
    && ln -sf /opt/codex/bin/codex /usr/local/bin/codex \
    && echo "${CODEX_CLI_VERSION}" > /opt/codex/.version

# ------------------------------------------------------------------------------
# 4. cptr のパッチ — Codex プロファイルの sandbox_mode を実際に効かせる
# ------------------------------------------------------------------------------
# ⚠️ これは upstream（cptr）のバグに対する回避策です。
#
# 何が起きるか:
#   codex の Linux サンドボックスは bubblewrap（bwrap）で、非特権ユーザ名前空間を
#   必要とします。コンテナ内ではこれが作れず、
#     bwrap: No permissions to create a new namespace
#   でシェル実行が失敗します。**--privileged でも直りません**（loopback の
#   RTM_NEWADDR で落ちる）ので、Docker 側を緩めても解決しません。
#
# なぜパッチが要るか:
#   cptr は Codex プロファイルに sandbox_mode フィールドを持ち、
#   utils/agents/models.py で
#     {"read-only", "workspace-write", "danger-full-access"}
#   と検証までしていて、管理画面にも設定 UI が出ます。
#   ところが utils/agents/codex.py はこの値を一切読まず、チャットモードから
#   計算した _sandbox_for_approval() の結果（read-only か workspace-write）を
#   app-server へ送ります。**UI の設定が効かない**状態です。
#   gateway 経由は tool_approval_mode=full ですが、full も workspace-write に
#   なるため（routers/gateway.py:746 → codex.py:225-228）、必ず bwrap を通ります。
#
# 何をするか:
#   プロファイルの値を優先させ、UI の設定を機能させます。未設定なら従来どおり。
#   承認の挙動（approvalPolicy）は変更しません。ask モードでは引き続き
#   1 コマンドずつ承認を求めます。緩むのは、この環境では動作しない
#   OS レベルの強制だけです。
#
# ⚠️ Codex のサンドボックスを外しても、実効的な安全性は下がりません。
#    cptr 自身の run_command は元からサンドボックスを使っておらず
#    （パッケージ全体に bwrap / landlock / seccomp / unshare の参照が無い）、
#    境界は最初からコンテナそのものだからです。
#
# ⚠️ upstream が該当行を変えたら、このビルドは**その場で失敗**します
#    （出現回数を 1 で検証しているため）。黙って無効化されることはありません。
RUN CODEX_PY="$(python3 -c 'import cptr, os; print(os.path.join(os.path.dirname(cptr.__file__), "utils", "agents", "codex.py"))')" \
    && test -f "$CODEX_PY" \
    && test "$(grep -c '"sandbox": _sandbox_for_approval(approval_mode),' "$CODEX_PY")" = 1 \
    && sed -i 's|"sandbox": _sandbox_for_approval(approval_mode),|"sandbox": profile.get("sandbox_mode") or _sandbox_for_approval(approval_mode),|' "$CODEX_PY" \
    && python3 -c "import ast; ast.parse(open('$CODEX_PY').read())" \
    && echo "patched: $CODEX_PY"

# ------------------------------------------------------------------------------
# 4b. cptr のパッチ — Browser タブ（proxy）で実行時 root-relative URL を直す
# ------------------------------------------------------------------------------
# ⚠️ これも upstream（cptr）の穴に対する回避策です。
#
# 何が起きるか:
#   cptr のプロキシが URL を書き換えるのは 3 か所だけです
#   （utils/browser/proxy.py）: 初期 HTML の属性 / CSS の url() /
#   **JS の import 文だけ**。実行時に JS が組み立てた URL は素通しになります。
#   Vite の dev サーバはアセットを必ず root-relative で返すため
#   （実測: `GET /src/assets/hero.png?import` →
#    `export default "/src/assets/hero.png"` という**裸の文字列**）、
#   rewrite_javascript の正規表現（from / import が前置される場合のみ）に
#   当たらず、そのまま React が <img src> に入れます。
#   ブラウザは iframe のオリジン（= cptr 自身）に対して解決し、cptr は
#   SPA フォールバックで **200 / text/html**（自分の index.html）を返すため、
#   404 にすらならず画像が黙って壊れます。<use href="/icons.svg#id"> も同様。
#
# なぜアプリ側で直せないか:
#   Vite 8 の dev は base: './' を無視し、
#   new URL('./x.png', import.meta.url) も `new URL("/src/assets/x.png", ...)`
#   に変換します（どちらも実測）。dev サーバは設計として root-relative しか
#   出さないので、「こう書けば見える」という運用ルールにはできません。
#
# なぜ chrome モードに逃げないか:
#   実 Chromium なら書き換えを通らないので直りますが、cptr は
#   `auth.role != "admin"` で弾きます（routers/browser.py:253
#   "Personal Chrome is available to administrators only"）。
#   マルチユーザ前提の本構成では**非管理者が Browser タブを使えなくなる**ため、
#   全員が使える proxy 側を直します。
#
# 何をするか:
#   rewrite_html が注入する runtime の直後にシムを 1 本足します。シムは
#   DOM に現れた root-relative な src / href / poster / srcset を
#   プロキシのフレームパスへ書き換え、MutationObserver で React の
#   再レンダリングにも追従します。中身は browser-root-relative-shim.js を参照。
#
# ⚠️ シムは**冪等**でなければなりません。書き換え不要な入力に対して
#    1 文字でも違う値を返すと、setAttribute → MutationObserver →
#    setAttribute … と無限に再発火し、ページが描画を終えません
#    （実測で Chromium がハングしました）。詳細は JS 側のコメント。
#
# ⚠️ upstream が該当行を変えたら、このビルドは**その場で失敗**します
#    （出現回数を 1 で検証しているため）。黙って無効化されることはありません。
COPY browser-root-relative-shim.js /opt/cptr-patch/browser-root-relative-shim.js

RUN python3 <<'PATCH'
import ast
import pathlib

import cptr

path = pathlib.Path(cptr.__file__).parent / "utils" / "browser" / "proxy.py"
source = path.read_text(encoding="utf-8")

anchor = "        '<script src=\"/api/browser/runtime.js\"></script>'\n"
assert source.count(anchor) == 1, f"anchor not found exactly once in {path}"

source = source.replace(anchor, anchor.rstrip("\n") + " + _ROOT_RELATIVE_SHIM\n")
source += '''

# --- パッチ: 実行時 root-relative URL のシム（docker/computer/Dockerfile 4b）---
# ⚠️ ファイルが無ければ import 時に落とします。黙ってシム無しで動くと
#    「画像だけ壊れる」という診断しづらい状態に戻るためです。
def _load_root_relative_shim() -> str:
    from pathlib import Path as _Path

    js = _Path("/opt/cptr-patch/browser-root-relative-shim.js").read_text(encoding="utf-8")
    return "<script>" + js + "</script>"


_ROOT_RELATIVE_SHIM = _load_root_relative_shim()
'''

ast.parse(source)
path.write_text(source, encoding="utf-8")
print("patched:", path)
PATCH

# ------------------------------------------------------------------------------
# 5. ボリュームで被せるディレクトリを先に作っておく
# ------------------------------------------------------------------------------
# ⚠️ ここが最も踏みやすい落とし穴です。
#    イメージ側に存在しないパスへ名前付きボリュームをマウントすると、
#    Docker が **root 所有**でそのディレクトリを作ります。cptr は書き込めず、
#    `codex login` が Permission denied、pnpm がキャッシュを書けない、で
#    静かに壊れます。イメージ側に cptr 所有で作っておけば、ボリュームは
#    その所有者を引き継ぎます。
#
#   /workspace     … ワークスペースの置き場。⚠️ ベースイメージには存在せず、
#                    VOLUME 宣言も /data だけなので、ここを作らないと root 所有に
#                    なります。そうなるとワークスペースピッカーからフォルダを
#                    作れず（UI 側の操作も cptr として走るため）、
#                    `mkdir /workspace/<name>` も Permission denied になります
#   .codex         … codex のログイン情報（auth.json）。CODEX_HOME の既定値
#   .cache         … uv / pip / npm のキャッシュ
#   .local         … pnpm のストアとグローバル bin（~/.local/share/pnpm）
RUN install -d -o cptr -g cptr \
        /workspace \
        /home/cptr/.codex \
        /home/cptr/.cache \
        /home/cptr/.local

USER cptr
WORKDIR /home/cptr

# ------------------------------------------------------------------------------
# 6. ビルド時の自己検証
# ------------------------------------------------------------------------------
# 壊れたイメージが起動してから気づくのを防ぎます。cptr として実行することで
# 「非 root で本当に使えるか」まで含めて確認しています。
RUN set -eux; \
    python3 -V; \
    uv --version; \
    node -v; \
    npm -v; \
    pnpm -v; \
    tsc -v; \
    tsx --version; \
    mcp-remote --help >/dev/null 2>&1 || test -x /usr/local/bin/mcp-remote; \
    gcc --version | head -1; \
    make --version | head -1; \
    rg --version | head -1; \
    curl --version | head -1; \
    jq --version; \
    git --version; \
    codex --version; \
    test -w /home/cptr/.codex; \
    test -w /workspace; \
    test -r /opt/cptr-patch/browser-root-relative-shim.js; \
    python3 -c "from cptr.utils.browser.proxy import _ROOT_RELATIVE_SHIM as s; assert 'MutationObserver' in s, 'shim not injected'; print('shim ok:', len(s), 'bytes')"

# ⚠️ ENTRYPOINT / CMD / ENV / EXPOSE はベースイメージから継承しています。
#    （tini -- cptr run --host 0.0.0.0 --port 8000 --headless / CPTR_DATA_DIR=/data）
#    ここで上書きしないこと。
