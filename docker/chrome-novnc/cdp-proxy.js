/*
 * CDP を他コンテナから到達可能にするための最小プロキシ
 * =============================================================================
 *
 * なぜ必要か（実測）:
 *   この Chrome（152）は `--remote-debugging-address=0.0.0.0` を**無視し**、
 *   CDP を常に 127.0.0.1 にしか bind します（headed / headless 双方で確認）。
 *   そのため playwright-mcp-* から `http://chrome-a:9222` を叩くと
 *   ECONNREFUSED になります。
 *
 * 単純な TCP 転送（socat）では足りません:
 *   Chrome は DNS リバインディング対策で Host ヘッダを検査し、
 *   localhost / IP アドレス以外を拒否します。実測の応答:
 *     HTTP 500 "Host header is specified and is not an IP address or localhost."
 *   TCP 層の転送では Host を書き換えられないため、HTTP 層で処理します。
 *
 * さらに応答本文の書き換えも要ります:
 *   /json/version が返す webSocketDebuggerUrl は `ws://127.0.0.1:<port>/...` で、
 *   **Playwright はこの URL をそのまま使います**（実測のエラー:
 *   `<ws connecting> ws://127.0.0.1:9222/devtools/browser/...` → ECONNREFUSED）。
 *   そこでリクエストの Host に差し替えて返します。クライアントが
 *   `chrome-a:9222` で来たなら `ws://chrome-a:9222/...` を返すので、
 *   接続先を設定で持つ必要がありません（自己適応）。
 *
 * ⚠️ このプロキシは認証しません。CDP そのものが無認証であり、境界は
 *    ネットワーク分離（svc-playwright-net-*）です。ホストにポートを
 *    公開しないでください。
 */
const http = require("http");
const net = require("net");

const LISTEN = Number(process.env.CDP_PORT || 9222);
const TARGET = Number(process.env.CDP_INTERNAL_PORT || 9221);
const UPSTREAM_HOST = `127.0.0.1:${TARGET}`;

// Chrome が返す内部アドレスを、クライアントが使ったホストへ差し替える。
function rewriteBody(body, clientHost) {
  return body
    .split(`127.0.0.1:${TARGET}`).join(clientHost)
    .split(`localhost:${TARGET}`).join(clientHost);
}

const server = http.createServer((req, res) => {
  const clientHost = req.headers.host || UPSTREAM_HOST;
  const up = http.request(
    {
      host: "127.0.0.1",
      port: TARGET,
      path: req.url,
      method: req.method,
      headers: { ...req.headers, host: UPSTREAM_HOST },
    },
    (r) => {
      const ct = String(r.headers["content-type"] || "");
      // JSON 以外（あれば）はそのまま流す。書き換える必要が無く、
      // バッファリングも避けたいため。
      if (!ct.includes("application/json")) {
        res.writeHead(r.statusCode, r.headers);
        r.pipe(res);
        return;
      }
      const chunks = [];
      r.on("data", (c) => chunks.push(c));
      r.on("end", () => {
        const body = rewriteBody(Buffer.concat(chunks).toString("utf8"), clientHost);
        const headers = { ...r.headers };
        headers["content-length"] = Buffer.byteLength(body);
        res.writeHead(r.statusCode, headers);
        res.end(body);
      });
      r.on("error", () => res.destroy());
    }
  );
  up.on("error", (e) => {
    res.writeHead(502, { "content-type": "text/plain" });
    res.end(`cdp-proxy: upstream error: ${e.message}`);
  });
  req.pipe(up);
});

// WebSocket（/devtools/...）は Upgrade を素通しする。ここでも Host を差し替える。
server.on("upgrade", (req, sock, head) => {
  const up = net.connect(TARGET, "127.0.0.1", () => {
    const lines = [`GET ${req.url} HTTP/1.1`];
    for (const [k, v] of Object.entries(req.headers)) {
      if (k.toLowerCase() === "host") continue;
      lines.push(`${k}: ${v}`);
    }
    lines.push(`Host: ${UPSTREAM_HOST}`);
    up.write(lines.join("\r\n") + "\r\n\r\n");
    if (head && head.length) up.write(head);
    sock.pipe(up);
    up.pipe(sock);
  });
  up.on("error", () => sock.destroy());
  sock.on("error", () => up.destroy());
});

server.listen(LISTEN, "0.0.0.0", () =>
  console.log(`cdp-proxy: 0.0.0.0:${LISTEN} -> 127.0.0.1:${TARGET}`)
);
