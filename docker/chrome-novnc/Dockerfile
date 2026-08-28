# ==============================================================================
# Chrome + noVNC — Playwright MCP が操作する画面をブラウザで見るためのイメージ
# ==============================================================================
#
# なぜ必要か:
#   Playwright MCP は既定で Chromium を**自分の内側でプロセス起動**し、CDP を
#   Playwright 内部のパイプで握ります。外から覗く口がありません。
#   そこで `--cdp-endpoint`（= PLAYWRIGHT_MCP_CDP_ENDPOINT）で立場を逆転させ、
#   「既にあるブラウザに接続する側」にします。ブラウザをこのイメージで外に出し、
#   Xvfb → x11vnc → websockify(noVNC) の順で画面をブラウザへ配信します。
#
#   Computer ──MCP──▶ Playwright MCP ──CDP:9222──▶ この Chrome ──:6080──▶ 人間
#
# ⚠️ ベースは Playwright MCP と**同じイメージ**にしています。理由は 3 つ:
#   1. Playwright は接続先 Chrome のバージョンに敏感。同一イメージなら
#      Playwright が期待する Chromium ビルドと厳密に一致する
#   2. Chromium が要求する共有ライブラリ一式と Xvfb が既に入っている
#      （実測: /usr/bin/Xvfb は存在、x11vnc / websockify / novnc のみ不足）
#   3. 既に pull 済みなので追加ダウンロードが発生しない
#   MCP サーバ本体は使いませんが、そのぶんの容量より上記の利点を取っています。

ARG PLAYWRIGHT_MCP_IMAGE=mcr.microsoft.com/playwright/mcp:latest
FROM ${PLAYWRIGHT_MCP_IMAGE}

USER root

# ------------------------------------------------------------------
# 1. 不足分だけ足す
# ------------------------------------------------------------------
# ⚠️ fonts-noto-cjk が無いと日本語ページが豆腐（□）になります。画面を目視で
#    確認するのが本イメージの目的なので必須です。
# ⚠️ procps はエントリポイントの死活監視と障害調査で使います。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        x11vnc \
        novnc \
        websockify \
        fonts-noto-cjk \
        procps \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------
# 2. 存在確認（レイアウトが変わったらここでビルドを落とす）
# ------------------------------------------------------------------
# ⚠️ 実行時に気づくと「noVNC は開くのに真っ黒」のように診断しづらい形で出ます。
ARG CHROME_BIN=/ms-playwright/chromium-1237/chrome-linux64/chrome
ENV CHROME_BIN=${CHROME_BIN}
RUN test -x "${CHROME_BIN}" \
    && test -x /usr/bin/Xvfb \
    && test -x /usr/bin/x11vnc \
    && test -x /usr/bin/websockify \
    && test -f /usr/share/novnc/vnc.html \
    && echo "chrome-novnc: 依存の存在確認 OK"

# ⚠️ CDP を他コンテナへ出すためのプロキシ。理由はスクリプト冒頭のコメント参照。
#    要約: この Chrome は --remote-debugging-address を無視して 127.0.0.1 にしか
#    bind せず、しかも Host ヘッダを検査するため TCP 転送では越えられない。
COPY cdp-proxy.js /usr/local/lib/cdp-proxy.js
COPY entrypoint.sh /usr/local/bin/chrome-novnc-entrypoint
RUN chmod +x /usr/local/bin/chrome-novnc-entrypoint \
    && node --check /usr/local/lib/cdp-proxy.js

# ⚠️ Chrome のプロファイル置き場。イメージに無いパスへ名前付きボリュームを
#    張ると Docker が root 所有で作るため、先に作っておきます
#    （このコンテナは root で動くので実害は出にくいですが、
#      docker/computer/Dockerfile と同じ方針に揃えています）。
RUN mkdir -p /data/profile

EXPOSE 6080 9222

ENTRYPOINT ["/usr/local/bin/chrome-novnc-entrypoint"]
