#!/bin/bash
# ==============================================================================
# Xvfb → Chrome(headed) → x11vnc → websockify(noVNC) を立ち上げて監視する
# ==============================================================================
#
# ⚠️ 1 コンテナ 4 プロセスなので、どれか 1 つでも死んだらコンテナごと落とします
#    （`wait -n`）。restart: unless-stopped が拾って作り直します。
#    片肺のまま生き残ると「noVNC は開くが画面が更新されない」など、
#    原因の分かりにくい壊れ方になるためです。
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_WIDTH="${SCREEN_WIDTH:-1280}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-720}"
SCREEN_DEPTH="${SCREEN_DEPTH:-24}"
# CDP_PORT        … 他コンテナへ見せるポート（cdp-proxy が 0.0.0.0 で待つ）
# CDP_INTERNAL_PORT … Chrome 本体が待つポート（127.0.0.1 のみ。後述）
CDP_PORT="${CDP_PORT:-9222}"
CDP_INTERNAL_PORT="${CDP_INTERNAL_PORT:-9221}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_VIEW_ONLY="${VNC_VIEW_ONLY:-true}"
# 画面更新の応答性。x11vnc の既定は 20ms で、実測 36.4 件/秒でした。
# 5ms にすると 52.6 件/秒（+44%）まで上がり、x11vnc の CPU は 0.1% のまま
# 変わりません（測定: 常時アニメするページ・1280x720・Raw エンコード）。
# ⚠️ 帯域は 15.6 → 21.6 MB/s に増えます。localhost なら問題になりませんが、
#    LAN 越しに見る場合や回線が細い場合は 20 に戻してください。
VNC_DEFER_MS="${VNC_DEFER_MS:-5}"
VNC_WAIT_MS="${VNC_WAIT_MS:-5}"
CHROME_BIN="${CHROME_BIN:-/ms-playwright/chromium-1237/chrome-linux64/chrome}"
CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-/data/profile}"

export DISPLAY=":${DISPLAY_NUM}"

# ------------------------------------------------------------------
# パスワードは必須
# ------------------------------------------------------------------
# ⚠️ noVNC のポートはホストに公開されます。無認証だと、そこに到達できる者が
#    エージェントの操作画面を覗け、view-only でなければ操作も奪えます。
#    さらにこのブラウザは CDP を開いており、CDP は認証を持ちません。
if [ -z "${VNC_PASSWORD:-}" ]; then
  echo "chrome-novnc: VNC_PASSWORD が未設定です。起動を中止します。" >&2
  exit 1
fi

VNC_PASSWD_FILE=/tmp/.vncpasswd
x11vnc -storepasswd "${VNC_PASSWORD}" "${VNC_PASSWD_FILE}" >/dev/null 2>&1
chmod 600 "${VNC_PASSWD_FILE}"

echo "chrome-novnc: display=${DISPLAY} ${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" \
     "cdp=${CDP_PORT}(内部 ${CDP_INTERNAL_PORT}) novnc=${NOVNC_PORT} view_only=${VNC_VIEW_ONLY}"

# ------------------------------------------------------------------
# 1. 仮想ディスプレイ
# ------------------------------------------------------------------
# -nolisten tcp: X の TCP を開かない。画面の配信は x11vnc の役目。
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" -nolisten tcp &
XVFB_PID=$!

for _ in $(seq 1 30); do
  [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
  sleep 0.2
done
[ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] || { echo "chrome-novnc: Xvfb が起動しません" >&2; exit 1; }

# ------------------------------------------------------------------
# 2. Chrome（headed）
# ------------------------------------------------------------------
# ⚠️ --headless を付けてはいけません。付けるとウィンドウが X 上に描かれず、
#    noVNC には灰色の空画面しか出ません（操作自体は成功するので気づきにくい）。
# ⚠️ **--remote-debugging-address は効きません。** この Chrome（152）は
#    このフラグを無視し、CDP を常に 127.0.0.1 にしか bind します
#    （headed / headless の双方で実測）。そのため Chrome は内部ポートで
#    待たせ、外向きは後段の cdp-proxy が担当します。
#    CDP には認証がないので、このポートは**ホストに公開しないでください**
#    （compose 側で公開していません）。
# ⚠️ --disable-dev-shm-usage は付けていません。compose で shm_size を
#    与えているため不要で、付けると /tmp へ退避して遅くなります。
# ⚠️ 下の --disable-* は、**このコンテナのログで実際に無駄な処理が観測されたもの**
#    だけを止めています（思いつきで並べると、必要な機能まで殺して原因が分からなく
#    なるため）。観測された内訳:
#      DEPRECATED_ENDPOINT ×4        … GCM の登録リトライ → --disable-background-networking
#      What's New のゴミタブ ×3      … → --disable-features=ChromeWhatsNewUI
#        ⚠️ これが最大の効果でした。3 枚閉じた瞬間にコンテナ CPU が
#           120% → 17% へ落ちています（実測）。放置すると上限に張り付き、
#           docker exec すら応答しなくなります。
#      Failed to connect to the bus ×16 … 音声/dbus 由来 → --mute-audio
#
# ⚠️ --disable-gpu は入れていません。gpu-process は実測 10.6% を使いますが、
#    無効化するとソフトウェア描画がブラウザプロセス側へ移るだけの可能性があり、
#    ホスト負荷が高くて A/B 測定を完了できませんでした。**未検証のものは足しません。**
"${CHROME_BIN}" \
  --no-sandbox \
  --no-first-run \
  --no-default-browser-check \
  --disable-search-engine-choice-screen \
  --disable-background-networking \
  --disable-component-update \
  --disable-sync \
  --disable-features=ChromeWhatsNewUI \
  --mute-audio \
  --disable-breakpad \
  --metrics-recording-only \
  --window-position=0,0 \
  --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
  --user-data-dir="${CHROME_USER_DATA_DIR}" \
  --remote-debugging-port="${CDP_INTERNAL_PORT}" \
  about:blank &
CHROME_PID=$!

for _ in $(seq 1 60); do
  if node -e "fetch('http://127.0.0.1:${CDP_INTERNAL_PORT}/json/version').then(()=>process.exit(0)).catch(()=>process.exit(1))" 2>/dev/null; then
    break
  fi
  sleep 0.5
done

# ------------------------------------------------------------------
# 3. CDP を外へ出す
# ------------------------------------------------------------------
# Host ヘッダの付け替えと webSocketDebuggerUrl の書き換えを行います。
# 詳細は cdp-proxy.js 冒頭のコメント参照。
CDP_PORT="${CDP_PORT}" CDP_INTERNAL_PORT="${CDP_INTERNAL_PORT}" \
  node /usr/local/lib/cdp-proxy.js &
CDP_PROXY_PID=$!

# ------------------------------------------------------------------
# 4. 画面の配信
# ------------------------------------------------------------------
# -forever : 最後のクライアントが切れても終了しない
# -shared  : 複数人が同時に見られる
# -viewonly: 既定。見るだけにして、エージェントの操作と喧嘩させない
VIEWONLY_FLAG=()
if [ "${VNC_VIEW_ONLY}" = "true" ]; then
  VIEWONLY_FLAG=(-viewonly)
fi

# ⚠️ -noxdamage は付けません。Xvfb は DAMAGE 拡張を持っており（実測: xdpyinfo に
#    DAMAGE あり）、x11vnc 自身も "X DAMAGE available on display, using it for
#    polling hints." と報告します。付けるとそのヒントを捨てることになります。
#    （ただし実測では更新レートに差は出ませんでした: 35.4 vs 35.2 件/秒。
#      「効くはず」ではなく「無効化する理由が無い」ので外しています）
x11vnc \
  -display "${DISPLAY}" \
  -rfbport "${VNC_PORT}" \
  -rfbauth "${VNC_PASSWD_FILE}" \
  -forever \
  -shared \
  -defer "${VNC_DEFER_MS}" \
  -wait "${VNC_WAIT_MS}" \
  -quiet \
  "${VIEWONLY_FLAG[@]}" &
X11VNC_PID=$!

websockify --web=/usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
WEBSOCKIFY_PID=$!

echo "chrome-novnc: 起動完了 (xvfb=${XVFB_PID} chrome=${CHROME_PID}"\
     " cdp-proxy=${CDP_PROXY_PID} x11vnc=${X11VNC_PID} websockify=${WEBSOCKIFY_PID})"

# どれか 1 つでも終了したらコンテナを落とす
wait -n
echo "chrome-novnc: いずれかのプロセスが終了しました。コンテナを終了します。" >&2
exit 1
