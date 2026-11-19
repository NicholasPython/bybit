#!/usr/bin/env bash
# NAS 发布脚本：一次 SSH 往返完成 备份→回滚点→构建→迁移→切换→重启→核验。
# 由本地调用：bash deploy/release-nas.sh <release-tag>
# 只做发布与只读核验；不启用策略、不确认接管、不建立风险基线。
set -euo pipefail

TAG="${1:?用法：bash deploy/release-nas.sh <release-tag，如 release-20261118>}"
SSH="ssh -o BatchMode=yes nas"
BUILD='.local/share/bybitbot/build'

echo "==> 1/6 打回滚点标签"
$SSH "podman tag localhost/bybitbot:0.1.0 localhost/bybitbot:rollback-before-$TAG"
$SSH "podman images --format '{{.Repository}}:{{.Tag}} {{.Id}}' | grep rollback-before-$TAG"

echo "==> 2/6 同步构建上下文"
rsync -azR --exclude '__pycache__/' --exclude '.DS_Store' \
    src/ alembic/ alembic.ini config/config.example.yaml \
    deploy/Containerfile.nas deploy/Containerfile.nas-runtime deploy/requirements.lock \
    nas:$BUILD/

echo "==> 3/6 构建镜像"
$SSH "podman build --pull=never -f \"\$HOME/$BUILD/deploy/Containerfile.nas\" -t localhost/bybitbot:$TAG \"\$HOME/$BUILD\" 2>&1 | tail -4"

echo "==> 4/6 配置检查"
$SSH "podman run --rm --network=host -v \"\$HOME/.config/bybitbot:/run/bybitbot:ro,z\" localhost/bybitbot:$TAG --config /run/bybitbot/config.yaml check"

echo "==> 5/6 应用迁移（worker 停着，失败不会留下半启动状态）"
$SSH "systemctl --user stop bybitbot-worker.service; sleep 2"
$SSH "podman run --rm --network=host -v \"\$HOME/.config/bybitbot:/run/bybitbot:ro,z\" localhost/bybitbot:$TAG --config /run/bybitbot/config.yaml migrate 2>&1 | tail -8"

echo "==> 6/6 切换标签并重启"
$SSH "podman tag localhost/bybitbot:$TAG localhost/bybitbot:0.1.0 && systemctl --user restart bybitbot-worker.service bybitbot-web.service; sleep 20; systemctl --user is-active bybitbot-worker.service bybitbot-web.service"
$SSH "podman inspect bybitbot-worker --format '{{.Image}}'"

echo "==> 核验"
$SSH 'podman exec -i bybitbot-pg psql -U bybitbot -d bybitbot_paper -At' <<'SQL'
\echo 'schema head:'
SELECT version_num FROM alembic_version;
\echo 'tick_at | last_error | state | lease | valuation_complete | baseline_available:'
SELECT value->>'tick_at', value->>'last_error', value->>'state', value->>'lease',
       value->>'valuation_complete', value->>'baseline_available'
  FROM runtime_state WHERE key='status';
\echo 'orders | executions | intents | inventory<>0:'
SELECT (SELECT count(*) FROM "order"), (SELECT count(*) FROM execution),
       (SELECT count(*) FROM trade_intent), (SELECT count(*) FROM inventory WHERE qty<>0);
\echo 'active pauses:'
SELECT reason_code FROM risk_pause WHERE active=true ORDER BY reason_code;
\echo 'strategies (都应 enabled=f):'
SELECT name, enabled FROM strategy ORDER BY name;
SQL

echo
echo "完成。回滚命令："
echo "  ssh nas 'podman tag localhost/bybitbot:rollback-before-$TAG localhost/bybitbot:0.1.0 && systemctl --user restart bybitbot-worker.service bybitbot-web.service'"
