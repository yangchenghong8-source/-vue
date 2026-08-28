#!/usr/bin/env bash
# ============================================================
# yang 项目统一开发启动脚本
# 一键启动：111 后端(MySQL + API) + kb-app 知识库 + 前端(Vue/Vite)
#
# 设计原则：
#   - 后端复用 /home/ta/yang 的 docker-compose（不复制旧服务代码）
#   - MySQL 数据卷复用 yang_mysql_data（当前空库 admin/admin123）
#   - 不改动 yang 任何文件、不覆盖任何 .env 环境变量
#   - 幂等：已在运行的服务自动跳过
#   - 前端端口 8501（原旧前端 nginx 8501 已释放，交给新前端）
#
# 用法: bash dev.sh [--build]
#   --build  强制重建 API 镜像（111 依赖变化时使用，默认用现有镜像）
# ============================================================
set -uo pipefail

COMPOSE_FILE="/home/ta/yang/docker-compose.yml"
COMPOSE_PROJECT="yang"
KB_RUN_SH="/home/ta/kb-app/run.sh"
WEB_DIR="/home/ta/yang/web"
WEB_PORT="8501"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }

BUILD_FLAG=""
[ "${1:-}" = "--build" ] && BUILD_FLAG="--build"

# 等待 http 接口就绪
wait_http() {
  local url="$1" name="$2" tries="${3:-30}" delay="${4:-2}"
  local i
  for i in $(seq 1 "$tries"); do
    if curl -sf -o /dev/null "$url" 2>/dev/null; then
      log "$name 健康 ($url)"
      return 0
    fi
    sleep "$delay"
  done
  fail "$name 未在 $((tries * delay))s 内就绪 ($url)"
  return 1
}

# 等待 docker 容器健康
wait_healthy() {
  local cname="$1" name="$2" tries="${3:-40}" delay="${4:-2}"
  local i st
  for i in $(seq 1 "$tries"); do
    st=$(docker inspect -f '{{.State.Health.Status}}' "$cname" 2>/dev/null || echo "none")
    [ "$st" = "healthy" ] && { log "$name 健康"; return 0; }
    sleep "$delay"
  done
  fail "$name 未在 $((tries * delay))s 内就绪"
  return 1
}

echo "============================================"
echo "  yang 统一开发启动"
echo "============================================"

# ---- 1. 清理上一轮遗留容器，释放 8088/8502/3307 端口 ----
echo "==> [1/5] 清理旧容器（释放端口）..."
docker compose -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" down 2>/dev/null \
  && log "已停掉旧容器" || warn "无旧容器，跳过"

# ---- 2. 启动后端 MySQL + API ----
echo "==> [2/5] 启动后端 MySQL + API（代码=yang，数据卷=yang_mysql_data）..."
docker compose -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" up -d $BUILD_FLAG mysql api
log "compose up 已提交，等待就绪..."

wait_healthy "mpt-mysql" "MySQL" 40 2 || exit 1
wait_http "http://127.0.0.1:8088/docs" "yang-API" 40 2 \
  || { fail "API 启动失败，查看日志: docker logs moneyprinterturbo-api"; exit 1; }

# ---- 3. 启动 kb-app 知识库 ----
echo "==> [3/5] 启动 kb-app（知识库，端口 3001）..."
if curl -sf -o /dev/null "http://127.0.0.1:3001/docs" 2>/dev/null; then
  warn "kb-app 已在运行，跳过"
else
  ( nohup bash "$KB_RUN_SH" > /tmp/kb-app.log 2>&1 & )
  wait_http "http://127.0.0.1:3001/docs" "kb-app" 30 2 \
    || { fail "kb-app 启动失败，查看日志: tail -f /tmp/kb-app.log"; exit 1; }
fi

# ---- 4. 检查 Redis ----
echo "==> [4/5] 检查 Redis（缓存）..."
if redis-cli ping >/dev/null 2>&1; then
  log "Redis 运行中 (127.0.0.1:6379)"
else
  warn "Redis 未运行（API 未启用 redis 时不影响核心功能）"
fi

# ---- 5. 启动前端 ----
echo "==> [5/5] 启动前端（Vite，端口 $WEB_PORT）..."
WEB_PID=$(lsof -ti:"$WEB_PORT" 2>/dev/null || true)
if [ -n "$WEB_PID" ]; then
  if ps -p "$WEB_PID" -o cmd= 2>/dev/null | grep -q "$WEB_DIR"; then
    warn "yang 前端已在运行 ($WEB_PORT)，跳过"
  else
    fail "端口 $WEB_PORT 已被其他进程占用 (PID: $WEB_PID)"
    ps -p "$WEB_PID" -o pid,cmd= 2>/dev/null
    exit 1
  fi
else
  ( cd "$WEB_DIR" && nohup npm run dev -- --host 0.0.0.0 --port "$WEB_PORT" > /tmp/mpt-web.log 2>&1 & )
  wait_http "http://127.0.0.1:$WEB_PORT" "前端" 30 2 \
    || { fail "前端启动失败，查看日志: tail -f /tmp/mpt-web.log"; exit 1; }
fi

# ---- 状态汇总 ----
echo ""
echo "============================================"
echo "            服务状态汇总"
echo "============================================"
mt_st=$(docker inspect -f '{{.State.Health.Status}}' mpt-mysql 2>/dev/null || echo "down")
if [ "$mt_st" = "healthy" ]; then
  printf "  ${GREEN}%-9s${NC} %s\n" "[RUNNING]" "MySQL    (3307, 数据卷 yang_mysql_data)"
else
  printf "  ${RED}%-9s${NC} %s\n" "[DOWN]" "MySQL"
fi
curl -sf -o /dev/null http://127.0.0.1:8088/docs 2>/dev/null \
  && printf "  ${GREEN}%-9s${NC} %s\n" "[RUNNING]" "API      (8088 -> /api/v1)" \
  || printf "  ${RED}%-9s${NC} %s\n" "[DOWN]" "API"
curl -sf -o /dev/null http://127.0.0.1:3001/docs 2>/dev/null \
  && printf "  ${GREEN}%-9s${NC} %s\n" "[RUNNING]" "kb-app   (3001 知识库)" \
  || printf "  ${RED}%-9s${NC} %s\n" "[DOWN]" "kb-app"
redis-cli ping >/dev/null 2>&1 \
  && printf "  ${GREEN}%-9s${NC} %s\n" "[RUNNING]" "Redis    (6379 缓存)" \
  || printf "  ${RED}%-9s${NC} %s\n" "[DOWN]" "Redis"
curl -sf -o /dev/null "http://127.0.0.1:$WEB_PORT" 2>/dev/null \
  && printf "  ${GREEN}%-9s${NC} %s\n" "[RUNNING]" "前端      ($WEB_PORT Vue/Vite)" \
  || printf "  ${RED}%-9s${NC} %s\n" "[DOWN]" "前端"
echo "============================================"
echo "  前端:     http://127.0.0.1:$WEB_PORT"
echo "  API 文档: http://127.0.0.1:8088/docs"
echo "  前端日志: tail -f /tmp/mpt-web.log"
echo "  kb-app 日志: tail -f /tmp/kb-app.log"
echo "============================================"
