"""FastAPI 应用入口。

启动：
    python -m rule_editor.api.main
或：
    uvicorn rule_editor.api.main:app --reload
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import get, project_root
from . import auth
from .routes import router

# 安全（2026-09-17）：不再挂 CORSMiddleware。前端页面与本后端同源部署
# （iframe 文档即 http://host:8010/，app.js 全部 fetch 走 /api 相对路径），
# 合法流量从不用 CORS；此前的 allow_origins="*" 只会让任意网页跨源读
# BOM 词典/白嫖本地求值接口，属纯暴露面，故直接移除。
app = FastAPI(title="adp 智能规则编辑器", version="0.1.0")

app.include_router(router, prefix="/api")


# ---------- 临时登录系统（2026-09-30）：未登录拦截业务 API ----------
# 首页/静态资源不拦（前端登录遮罩负责视觉门禁），数据接口一律要求会话
# Cookie；login.enabled=false 时整体放行。
_PUBLIC_API = {"/api/login", "/api/logout", "/api/auth_check",
               "/api/health", "/api/boot_id"}


@app.middleware("http")
async def login_gate(request: Request, call_next):
    path = request.url.path
    if (auth.enabled() and path.startswith("/api/")
            and path not in _PUBLIC_API
            and not auth.valid(request.cookies.get(auth.COOKIE))):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return await call_next(request)

# 静态前端
_web_dir = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(_web_dir):
    app.mount("/static", StaticFiles(directory=_web_dir), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_web_dir, "index.html"))


def main() -> None:
    import uvicorn
    host = get("server", "host", "127.0.0.1")
    port = int(get("server", "port", 8000))
    reload_ = bool(get("server", "reload", False))
    uvicorn.run("rule_editor.api.main:app", host=host, port=port, reload=reload_)


if __name__ == "__main__":
    main()
