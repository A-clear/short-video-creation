# ==============================================================================
# Docling（Basic RAG のコンテンツ抽出エンジン）— 日本語 OCR 対応イメージ
# ==============================================================================
#
# 公式イメージ quay.io/docling-project/docling-serve に **tesseract の日本語
# 言語パックだけ**を足したものです。変換モデル（layout / tableformer /
# picture_classifier / rapidocr / easyocr）は公式イメージに焼き込み済みなので
# ここでは触りません。
#
# なぜ必要か:
#   公式イメージが入れている言語パックは英語だけです
#   （docling-serve の os-packages.txt に tesseract-langpack-eng のみ）。
#   日本語のスキャン PDF にそのまま OCR をかけると文字化けした本文が
#   ベクトル DB に入り、検索結果を汚染します。
#
# 入手元:
#   tesseract-langpack-jpn / -jpn_vert は CentOS Stream 9 の **AppStream**
#   にあります（4.1.0-3.el9）。EPEL は不要で、ベースイメージ
#   quay.io/sclorg/python-312-c9s:c9s の既定リポジトリで解決します。
#   公式イメージが tesseract-langpack-eng を入れているのと同じ経路です。
#
# ⚠️ docker compose up -d だけでは反映されません。docker compose build を
#    明示的に流してください（Computer のイメージと同じ扱い）。

ARG DOCLING_SERVE_IMAGE=quay.io/docling-project/docling-serve:latest
FROM ${DOCLING_SERVE_IMAGE}

# ------------------------------------------------------------------------------
# 日本語の言語パック
# ------------------------------------------------------------------------------
#
# ⚠️ 公式イメージは USER 1001 で終わっているため、dnf を使うには一度 root に
#    戻す必要があります。最後に必ず 1001 へ戻してください。戻し忘れると
#    docling-serve が root で動きます。
#
# ⚠️ 末尾の test は「入ったつもりで入っていない」を防ぐための検証です。
#    TESSDATA_PREFIX はベースイメージの ENV（/usr/share/tesseract/tessdata/）。
#    upstream がベースディストリを変えてパスやパッケージ名が変わったら、
#    実行時に黙って OCR が効かなくなるのではなく **ビルドがその場で失敗**します。
USER 0

RUN set -eux; \
    dnf install -y --best --nodocs --setopt=install_weak_deps=False \
        tesseract-langpack-jpn \
        tesseract-langpack-jpn_vert; \
    dnf -y clean all; \
    rm -rf /var/cache/dnf; \
    test -f "${TESSDATA_PREFIX}/eng.traineddata"; \
    test -f "${TESSDATA_PREFIX}/jpn.traineddata"; \
    test -f "${TESSDATA_PREFIX}/jpn_vert.traineddata"; \
    tesseract --list-langs

USER 1001
