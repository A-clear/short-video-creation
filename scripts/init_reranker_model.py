#!/usr/bin/env python3
"""リランクモデルの事前取得とロード検証。

docker-compose.yml の reranker-model-init サービスから、Open WebUI 本体と
**同じイメージ**で実行される。役割は 2 つある。

1. 事前取得 — モデルを open-webui-data ボリューム上の HuggingFace キャッシュへ
   入れる。これが無いと 4 つの uvicorn ワーカーが起動時に同時ダウンロードを始める
   （main.py:624 の get_rf は lifespan の中で走るため、遅延ロードではない）。

2. ロード検証 — 実際に CrossEncoder を構築して predict まで通す。
   Open WebUI 本体はモデルのロードに失敗しても HTTP エラーを返さず、
   ENABLE_RAG_HYBRID_SEARCH を黙って false へ戻すだけである
   （routers/retrieval.py:1185-1187）。管理者から見えるのは
   「保存したのにトグルが off に戻っている」という症状だけで、原因は
   log.error の 1 行にしか出ない。ここで先に失敗させることで、
   それを docker compose up 時点の明示的なエラーへ変える。

⚠️ Open WebUI 本体と同じ条件でロードすること。trust_remote_code や backend が
   違うと検証の意味が無くなる。参照元は routers/retrieval.py:204-222。

⚠️ device は "cpu" 固定。本体は env.py:45-65 で検出した DEVICE_TYPE を使うが、
   これは環境変数ではなく実行時の検出結果であり、外から同じ値を渡せない。
   本構成は CUDA を持たないため cpu で一致する。GPU を導入する場合はここも直すこと。

⚠️ 本体は model_kwargs=SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS も渡すが
   （routers/retrieval.py:213）、このスクリプトは渡していない。上流の既定が None で
   （env.py:1070-1077）本プロジェクトの .env.example にも項目が無いため現状は等価。
   この変数を使い始めたら、ここにも渡すこと。渡さないと検証の意味が失われる。
"""

from __future__ import annotations

import os
import sys


def _env_flag(name: str, default: str) -> bool:
    """Open WebUI と同じ真偽値の読み方（config.py / env.py 全体で共通）。"""
    return os.getenv(name, default).strip().lower() == "true"


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(
            f"[init-reranker] {name} が空です。docker-compose.yml の "
            f"reranker-model-init サービスの environment を確認してください。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


def main() -> int:
    model_id = _require("RERANKER_MODEL_ID")
    cache_dir = _require("SENTENCE_TRANSFORMERS_HOME")

    # 本体と同じ既定値に揃える（env.py:1065-1067 / env.py:1082-1083 / config.py:1026）。
    backend = os.getenv("SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND", "").strip() or "torch"
    sigmoid = _env_flag("SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION", "True")
    trust_remote_code = _env_flag("RAG_RERANKING_MODEL_TRUST_REMOTE_CODE", "True")

    print(
        f"[init-reranker] model={model_id} backend={backend} "
        f"sigmoid={sigmoid} trust_remote_code={trust_remote_code}",
        flush=True,
    )

    from huggingface_hub import snapshot_download

    # 1. キャッシュを先に見る。2 回目以降は HuggingFace が落ちていても成功する。
    #    ここを local_files_only=True から始めるのが肝で、compose を上げ直すたびに
    #    ネットワークへ出る構成にしてはいけない。
    try:
        path = snapshot_download(repo_id=model_id, cache_dir=cache_dir, local_files_only=True)
        print(f"[init-reranker] cache hit: {path}", flush=True)
    except Exception:
        print("[init-reranker] cache miss. downloading from HuggingFace ...", flush=True)
        path = snapshot_download(repo_id=model_id, cache_dir=cache_dir)
        print(f"[init-reranker] downloaded: {path}", flush=True)

    # 2. 本体と同じ条件で CrossEncoder を構築する（routers/retrieval.py:204-222）。
    #    get_model_path はスナップショットの絶対パスを本体へ渡すので、ここでも path を使う。
    import sentence_transformers
    import torch

    model = sentence_transformers.CrossEncoder(
        path,
        device="cpu",
        trust_remote_code=trust_remote_code,
        backend=backend,
        activation_fn=torch.nn.Sigmoid() if sigmoid else None,
    )

    scores = model.predict([("動作確認のクエリ", "これは動作確認用のダミー文書です。")])
    score = float(scores[0])
    print(f"[init-reranker] reranker ready. sample score={score:.4f}", flush=True)

    # 3. トークナイザも同じスナップショットから読めることを確かめる。
    #    RAG_TOKENIZER_MODEL は同じモデル ID を指し、本体は
    #    AutoTokenizer.from_pretrained で読む（routers/retrieval.py:1589-1594）。
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        path,
        local_files_only=True,
        trust_remote_code=trust_remote_code,
    )
    token_count = len(tokenizer.encode("トークナイザの動作確認"))
    print(f"[init-reranker] tokenizer ready. sample={token_count} tokens", flush=True)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 失敗理由をそのまま compose のログへ出す
        print(f"[init-reranker] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
