/*
 * cptr Browser タブ（proxy モード）のパッチ — 実行時 root-relative URL の書き換え
 * =============================================================================
 *
 * ⚠️ これは upstream（cptr）の穴に対する回避策です。適用は
 *    docker/computer/Dockerfile の「4. cptr のパッチ」を参照。
 *
 * 何が起きるか:
 *   cptr のプロキシが URL を書き換えるのは 3 か所だけです
 *   （cptr/utils/browser/proxy.py）:
 *     1. 初期 HTML の属性        … _HtmlRewriter
 *     2. CSS の url() / @import  … rewrite_css
 *     3. JS の **import 文だけ** … rewrite_javascript
 *   実行時に JS が組み立てた URL はどこも通りません。Vite の dev サーバは
 *   アセットを必ず root-relative で返すため（実測:
 *   `GET /src/assets/hero.png?import` → `export default "/src/assets/hero.png"`）、
 *   この裸の文字列が React によって <img src> に入ります。
 *
 *   結果、ブラウザは iframe のオリジン（= cptr 自身）に対して解決し、
 *   cptr は SPA フォールバックで **200 / text/html**（自分の index.html）を
 *   返します。404 にすらならないので、画像が黙って壊れます。
 *   同じ理由で <use href="/icons.svg#id"> も壊れます。
 *
 *   アプリ側では回避できません。Vite 8 の dev は base: './' を無視し、
 *   new URL('./x.png', import.meta.url) も `new URL("/src/assets/x.png", ...)`
 *   に変換するため、どちらも root-relative のままです（実測）。
 *
 * 何をするか:
 *   フレーム文書に注入され、DOM に現れた root-relative な src / href /
 *   poster / srcset をプロキシのフレームパスへ書き換えます。初期スキャンに
 *   加えて MutationObserver を張り、React の再レンダリングで属性が戻されても
 *   追従します。
 *
 * ⚠️ cptr 自身の browser-runtime.js は fetch / XMLHttpRequest / WebSocket /
 *    EventSource / pushState しか差し替えておらず、DOM のアセット読み込みには
 *    触れていません（MutationObserver は <title> 監視の 1 個のみ）。
 *    つまりこのシムと責務は重複しません。
 *
 * ⚠️ 扱わないもの（既知の限界）:
 *    - インライン style / CSS-in-JS の url(/...)。React が style を頻繁に
 *      書き換えるため、監視するとコストに見合いません
 *    - Shadow DOM 内の要素
 *    - Web Worker / Service Worker が組み立てる URL
 */
(function () {
  "use strict";

  var info = window.__cptrBrowser;
  if (!info || !info.session || !info.url) return;

  var base;
  try {
    base = new URL(info.url);
  } catch (e) {
    return;
  }

  // proxy.py の proxy_path() が組み立てるものと同じ前置き。
  //   /api/browser/frame/<session>/<scheme>/<host>
  var PREFIX =
    "/api/browser/frame/" +
    info.session +
    "/" +
    base.protocol.replace(":", "") +
    "/" +
    base.host;

  var ATTRS = ["src", "href", "poster"];
  var SELECTOR = "[src],[href],[poster],[srcset]";
  var XLINK = "http://www.w3.org/1999/xlink";

  // 書き換え対象は「単一スラッシュで始まる」URL だけ。
  //   //example.com/x  … プロトコル相対。オリジンが変わるので触らない
  //   /api/browser/... … 書き換え済み。再適用すると多重前置きになる
  function needsFix(value) {
    return (
      typeof value === "string" &&
      value.charCodeAt(0) === 47 /* / */ &&
      value.charCodeAt(1) !== 47 &&
      value.lastIndexOf("/api/browser/", 0) !== 0
    );
  }

  // srcset は "URL 記述子, URL 記述子" の並び。URL は各項の先頭にある。
  function fixSrcset(value) {
    if (typeof value !== "string" || value.indexOf("/") === -1) return value;
    return value
      .split(",")
      .map(function (part) {
        // ⚠️ 戻り値は必ず trim 済みの item にすること。未 trim の part を
        //    返すと join(", ") が毎回スペースを 1 個足し、属性値が永久に
        //    変化し続けて MutationObserver が無限に再発火します
        //    （実測: Chromium がページを描画し終えずハングした）。
        //    「書き換え不要なら入力と同一を返す」= 冪等性が停止条件です。
        var item = part.trim();
        if (!item) return item;
        var space = item.indexOf(" ");
        var url = space === -1 ? item : item.slice(0, space);
        return needsFix(url) ? PREFIX + item : item;
      })
      .join(", ");
  }

  function fixElement(el) {
    if (!el || el.nodeType !== 1 || !el.getAttribute) return;

    for (var i = 0; i < ATTRS.length; i++) {
      var value = el.getAttribute(ATTRS[i]);
      if (needsFix(value)) el.setAttribute(ATTRS[i], PREFIX + value);
    }

    // SVG の <use> は href だけでなく、古い xlink:href も使われる。
    if (el.hasAttributeNS && el.hasAttributeNS(XLINK, "href")) {
      var xlinkValue = el.getAttributeNS(XLINK, "href");
      if (needsFix(xlinkValue)) {
        el.setAttributeNS(XLINK, "xlink:href", PREFIX + xlinkValue);
      }
    }

    var srcset = el.getAttribute("srcset");
    if (srcset) {
      var fixed = fixSrcset(srcset);
      if (fixed !== srcset) el.setAttribute("srcset", fixed);
    }
  }

  function scan(root) {
    if (!root) return;
    if (root.nodeType === 1) fixElement(root);
    if (root.querySelectorAll) {
      var found = root.querySelectorAll(SELECTOR);
      for (var i = 0; i < found.length; i++) fixElement(found[i]);
    }
  }

  // setAttribute 自体が observer を再発火させるが、needsFix() が
  // 書き換え済みの値を弾くのでループにはならない。
  new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var record = records[i];
      if (record.type === "attributes") {
        fixElement(record.target);
        continue;
      }
      for (var j = 0; j < record.addedNodes.length; j++) {
        scan(record.addedNodes[j]);
      }
    }
  }).observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    // xlink:href の localName は href なので、この 4 つで拾える。
    attributeFilter: ["src", "href", "poster", "srcset"],
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      scan(document);
    });
  } else {
    scan(document);
  }
})();
