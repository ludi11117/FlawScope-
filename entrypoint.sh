#!/bin/sh
set -e

# 只启动 FastAPI 一个进程。
#
# 这里不再拉起 Streamlit 旧前端（app.py / 8501）：它的页面已全部迁到 React
# （最后一个统计页在 c8a5540 迁完），容器再起它就等于把默认入口指向一个
# 已经没人维护的界面——README 说"已不是日常入口"，容器却在起它，两边对不上。
#
# 用 exec 而不是 `& ... wait`：单进程时 exec 让 uvicorn 直接成为 PID 1，
# SIGTERM/SIGINT 由它自己接管，优雅关闭比原来"trap + kill 子进程"更可靠。
# 原来之所以要后台 + trap，是因为有两个进程需要一起收尾。
echo "[FlawScope] 启动 FastAPI (端口 8000)..."
exec uvicorn api:app --host 0.0.0.0 --port 8000