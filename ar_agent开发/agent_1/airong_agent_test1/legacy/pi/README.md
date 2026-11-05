# Pi 回退模块

- `main.py`：原 Pi 网页后端。
- `pi_rpc.py`：Pi 子进程 JSONL 桥和会话管理。
- `.venv/`：原 Python 3.15 环境归档，不作为当前启动环境。

回退网页请在项目根目录使用 `run-web-pi.ps1`；它使用根目录 Python 3.12 环境运行 `legacy.pi.main:app`，仍读取原 `config/pi-agent` 和 `.pi` 资源。端口占用时报错。应先停止当前服务，再启动回退服务。

Pi 终端入口仍为根目录 `run-pi.bat`，依赖相邻 `pi-main` 底座。Pi JSONL 与 LangGraph 检查点分开保存，后续 LangGraph 对话不会自动写回旧历史。
