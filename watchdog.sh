#!/usr/bin/env bash
# 微信桥接健康看门狗：由 cron 每 5 分钟调用一次。
# 职责：serve 挂了就拉起来；NEEDS_RELOGIN 只通知一次。
# 未登录时静默退出，不做任何事。
set -uo pipefail

BRIDGE="$HOME/workspace/wechat-bridge"
SESSION="$BRIDGE/state/session.json"
PIDFILE="$BRIDGE/state/serve.pid"
NEED_RELOGIN="$BRIDGE/state/NEEDS_RELOGIN"
NOTIFIED="$BRIDGE/state/notified_relogin"
LOG="$BRIDGE/state/watchdog.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# 未登录：静默
[ -f "$SESSION" ] || { echo "NO_SESSION"; exit 0; }

# token 失效：只通知一次
if [ -f "$NEED_RELOGIN" ]; then
  if [ ! -f "$NOTIFIED" ]; then
    touch "$NOTIFIED"
    log "NEEDS_RELOGIN 首次发现，准备通知用户重新扫码"
    echo "NEEDS_RELOGIN_NOTIFY"
  else
    echo "NEEDS_RELOGIN_ALREADY_NOTIFIED"
  fi
  exit 0
fi

# serve 存活检查
alive=0
if [ -f "$PIDFILE" ]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    alive=1
  fi
fi

if [ "$alive" -eq 1 ]; then
  echo "SERVE_ALIVE"
  exit 0
fi

log "serve 未运行，尝试重启"
cd "$BRIDGE" && nohup python3 bridge.py serve >> "$BRIDGE/state/serve.log" 2>&1 &
disown  # 防止 bash 在退出时卡在 do_wait 等待后台任务
sleep 2
if [ -f "$PIDFILE" ]; then
  newpid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$newpid" ] && kill -0 "$newpid" 2>/dev/null; then
    log "serve 重启成功 pid=$newpid"
    echo "SERVE_RESTARTED"
    exit 0
  fi
fi
log "serve 重启失败"
echo "SERVE_RESTART_FAILED"
exit 0
