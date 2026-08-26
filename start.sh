#!/bin/bash
# ===========================================
# 111 (MoneyPrinterTurbo) + kb-app 一键启动脚本
# 启动顺序: Redis → kb-app → 111 API → 111 WebUI
# 所有服务均带崩溃自动重启（最大重试 5 次）
# ===========================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[FAIL]${NC} $1"; }

# 加载 KB 配置（地址 + 认证）
if [ -f /home/ta/111/.env.kb ]; then
    set -a; source /home/ta/111/.env.kb; set +a
    echo "==> 已加载 KB 配置: $KB_API_BASE"
fi

# ---- 1. Redis ----
echo "==> 检查 Redis..."
if redis-cli ping &>/dev/null; then
    log "Redis 已在运行"
else
    warn "Redis 未运行，尝试启动..."
    sudo systemctl start redis-server 2>/dev/null || {
        err "Redis 启动失败，请手动检查"
        exit 1
    }
    sleep 1
    redis-cli ping &>/dev/null && log "Redis 启动成功" || { err "Redis 无法连通"; exit 1; }
fi

# ---- 2. kb-app (知识库后端) ----
echo "==> 启动 kb-app (端口 3001)..."
KB_PID=$(lsof -ti:3001 2>/dev/null)
if [ -n "$KB_PID" ]; then
    warn "端口 3001 已被占用 (PID $KB_PID)，跳过"
else
    cd /home/ta/kb-app/backend
    nohup bash -c '
        RESTART_COUNT=0
        MAX_RESTARTS=5
        while true; do
            echo "[$(date)] kb-app starting (restart count: $RESTART_COUNT)..."
            venv/bin/python main.py
            RESTART_COUNT=$((RESTART_COUNT + 1))
            if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
                echo "[$(date)] kb-app crashed $MAX_RESTARTS times, stopping restart"
                break
            fi
            echo "[$(date)] kb-app crashed, restarting in 3s..."
            sleep 3
        done
    ' > /tmp/kb-app.log 2>&1 &
    sleep 5
    curl -sf -o /dev/null http://127.0.0.1:3001/docs && log "kb-app 启动成功 (PID $!)" || err "kb-app 启动失败，查看 /tmp/kb-app.log"
fi

# ---- 3. 111 API (FastAPI + ASGI) ----
echo "==> 启动 111 API (端口 8088)..."
API_PID=$(lsof -ti:8088 2>/dev/null)
if [ -n "$API_PID" ]; then
    warn "端口 8088 已被占用 (PID $API_PID)，跳过"
else
    cd /home/ta/111
    nohup bash -c '
        RESTART_COUNT=0
        MAX_RESTARTS=5
        while true; do
            echo "[$(date)] 111 API starting (restart count: $RESTART_COUNT)..."
            .venv/bin/python3 -c "import uvicorn; uvicorn.run(app=\"app.asgi:app\", host=\"127.0.0.1\", port=8088, reload=False, log_level=\"warning\")"
            RESTART_COUNT=$((RESTART_COUNT + 1))
            if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
                echo "[$(date)] 111 API crashed $MAX_RESTARTS times, stopping restart"
                break
            fi
            echo "[$(date)] 111 API crashed, restarting in 5s..."
            sleep 5
        done
    ' > /tmp/mpt-api.log 2>&1 &
    sleep 5
    curl -sf -o /dev/null http://127.0.0.1:8088/docs && log "111 API 启动成功 (PID $!)" || err "111 API 启动失败，查看 /tmp/mpt-api.log"
fi

# ---- 4. 111 WebUI (Streamlit) ----
echo "==> 启动 111 WebUI (端口 8502)..."
UI_PID=$(lsof -ti:8502 2>/dev/null)
if [ -n "$UI_PID" ]; then
    warn "端口 8502 已被占用 (PID $UI_PID)，跳过"
else
    cd /home/ta/111
    nohup bash -c '
        RESTART_COUNT=0
        MAX_RESTARTS=5
        while true; do
            echo "[$(date)] 111 WebUI starting (restart count: $RESTART_COUNT)..."
            .venv/bin/python .venv/bin/streamlit run webui/Main.py \
                --server.port 8502 \
                --server.headless true \
                --browser.gatherUsageStats false \
                --server.address 127.0.0.1
            RESTART_COUNT=$((RESTART_COUNT + 1))
            if [ $RESTART_COUNT -ge $MAX_RESTARTS ]; then
                echo "[$(date)] 111 WebUI crashed $MAX_RESTARTS times, stopping restart"
                break
            fi
            echo "[$(date)] 111 WebUI crashed, restarting in 5s..."
            sleep 5
        done
    ' > /tmp/mpt-webui.log 2>&1 &
    sleep 8
    curl -sf -o /dev/null http://127.0.0.1:8502 && log "111 WebUI 启动成功 (PID $!)" || err "111 WebUI 启动失败，查看 /tmp/mpt-webui.log"
fi

# ---- 5. 状态汇总 ----
echo ""
echo "========================================"
echo "          服务状态"
echo "========================================"
echo "  本地服务:"
redis-cli ping &>/dev/null && printf "  ${GREEN}%-10s${NC} %s\n" "[RUNNING]" "Redis" || printf "  ${RED}%-10s${NC} %s\n" "[DOWN]" "Redis"
curl -sf -o /dev/null http://127.0.0.1:3001/docs && printf "  ${GREEN}%-10s${NC} %s\n" "[RUNNING]" "kb-app" || printf "  ${RED}%-10s${NC} %s\n" "[DOWN]" "kb-app"
curl -sf -o /dev/null http://127.0.0.1:8088/docs && printf "  ${GREEN}%-10s${NC} %s\n" "[RUNNING]" "111-API" || printf "  ${RED}%-10s${NC} %s\n" "[DOWN]" "111-API"
curl -sf -o /dev/null http://127.0.0.1:8502 && printf "  ${GREEN}%-10s${NC} %s\n" "[RUNNING]" "111-WebUI" || printf "  ${RED}%-10s${NC} %s\n" "[DOWN]" "111-WebUI"
echo ""
echo "  对外入口 (Nginx):"
curl -sf -o /dev/null http://127.0.0.1:8501 && printf "  ${GREEN}%-10s${NC} %s\n" "[RUNNING]" "WebUI" || printf "  ${RED}%-10s${NC} %s\n" "[DOWN]" "WebUI"
curl -sf -o /dev/null http://127.0.0.1:8080/docs && printf "  ${GREEN}%-10s${NC} %s\n" "[RUNNING]" "API" || printf "  ${RED}%-10s${NC} %s\n" "[DOWN]" "API"
echo "========================================"
