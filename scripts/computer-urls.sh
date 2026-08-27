#!/usr/bin/env bash
# Open WebUI Computer 3 台のアクセス URL を、ホスト側から開ける形で出力する。
#
# なぜこのスクリプトが要るのか:
#
#   cptr は起動時に `➜ http://localhost:8000/?token=...` を print するが、
#   この URL はコンテナ内部の視点で組み立てられており、ホストからは開けない。
#   cptr/cli.py の該当箇所（v0.9.21）:
#
#       display_host = "localhost" if host == "0.0.0.0" else host
#       url = f"http://{display_host}:{port}/?token={token}"
#
#   コンテナでは --host 0.0.0.0 が必須なので display_host は無条件に "localhost"
#   へ潰れ、port もコンテナ内部の 8000 のままになる。ホスト側のポート公開
#   （8001 / 8002 / 8003）を cptr に伝える環境変数は存在しない。
#   結果として 3 台のログが完全に同じ URL を表示し、どれがどの Computer か
#   区別できない。実際に違うのはトークンだけである。
#
#   さらに token は run() の中で secrets.token_hex(32) により生成されるため、
#   コンテナを restart するたびに変わる。トークンを控えておく運用は必ず壊れる。
#   そのつど最新の起動行から引くのが唯一正しい取り方であり、それがこのスクリプト。
#
# ポート番号はハードコードしない。.env の OPEN_WEBUI_COMPUTER_PORT_A などで
# 変更できるため、docker compose port から実際のマッピングを引く。
set -euo pipefail

cd "$(dirname "$0")/.."

# サービス名 → チーム表示名
services=(
  "open-webui-computer-a:team-a"
  "open-webui-computer-b:team-b"
  "open-webui-computer-c:team-c"
)

# JSON の 1 フィールドを取り出す。jq の有無に依存させない。
json_field() {
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\([^,}]*\).*/\1/p" <<<"$2" | tr -d '"'
}

echo
echo "Open WebUI Computer アクセス URL"
echo "==============================================================="

for entry in "${services[@]}"; do
  service="${entry%%:*}"
  team="${entry##*:}"
  container="svc-${service}"

  printf '\n[%s] %s\n' "$team" "$container"

  if ! docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -q true; then
    echo "  状態: 起動していません（docker compose up -d が必要）"
    continue
  fi

  # ホスト側ポート。"0.0.0.0:8001" の形で返るので後半を取る。
  mapping="$(docker compose port "$service" 8000 2>/dev/null || true)"
  port="${mapping##*:}"
  if [ -z "$port" ]; then
    echo "  状態: ポート公開が見つかりません（compose の ports: を確認してください）"
    continue
  fi

  config="$(curl -s --max-time 5 "http://localhost:${port}/api/config" || true)"
  if [ -z "$config" ]; then
    echo "  状態: http://localhost:${port} に応答がありません"
    continue
  fi

  needs_setup="$(json_field needs_setup "$config")"
  auth_mode="$(json_field auth_mode "$config")"
  version="$(json_field version "$config")"

  # 起動時の print 行だけを拾う。アクセスログ側の "GET /?token=..." を
  # 拾わないよう ➜ で始まる行に限定し、最新の起動分（tail -1）を採る。
  token="$(docker logs "$container" 2>&1 \
    | grep -E '^[[:space:]]*➜[[:space:]]+http' \
    | tail -1 \
    | grep -oE 'token=[0-9a-f]+' \
    | cut -d= -f2 || true)"

  echo "  cptr ${version} / 認証モード: ${auth_mode}"

  if [ "$needs_setup" = "true" ]; then
    if [ -n "$token" ]; then
      echo "  状態: 未セットアップ（初回の管理者作成が必要）"
      echo "  URL : http://localhost:${port}/?token=${token}"
    else
      echo "  状態: 未セットアップだが、ログにトークンが見つかりません"
      echo "        docker compose restart ${service} で再発行してください"
    fi
  else
    echo "  状態: セットアップ済み"
    echo "  URL : http://localhost:${port}/"
    echo "        （設定済みのユーザー名とパスワードでログイン）"
  fi
done

echo
echo "==============================================================="
echo "⚠️  トークンは restart のたびに変わります。控えずにこのスクリプトを再実行してください。"
echo
