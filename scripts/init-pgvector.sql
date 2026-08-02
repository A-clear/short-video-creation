-- ============================================================================
-- pgvector 拡張の初期化
-- ============================================================================
--
-- postgres コンテナの初回起動時（データディレクトリが空のとき）にのみ実行される。
-- 既存のボリュームに対しては実行されないため、途中から pgvector に切り替える場合は
-- 手動で以下を実行すること:
--
--   docker compose exec postgres psql -U openwebui -d openwebui -c 'CREATE EXTENSION IF NOT EXISTS vector;'
--
-- Open WebUI 側の PGVECTOR_CREATE_EXTENSION=true でもアプリ起動時に作成されるが、
-- その場合は接続ユーザにスーパーユーザ権限が必要になる。
-- 本ファイルで先に作っておけば、アプリ側は PGVECTOR_CREATE_EXTENSION=false で
-- 非特権ユーザとして運用できる。
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
