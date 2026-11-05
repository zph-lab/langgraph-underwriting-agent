"""FastAPI 入口：Vue 客户端 + LangGraph 运行层（兼容原 SSE 协议）。

启动（在 airong_agent_test1 目录下）：
    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8712
"""
from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .file_extract import MAX_BYTES, ExtractError, extract_text
from project_paths import PROJECT_DIR, WEB_DIR
from langgraph_app.manager import RuntimeErrorBase, SessionManager

mgr = SessionManager()
RUNTIME_REVISION = 'conversion-20261028-1'


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await mgr.start()
    mgr.start_reaper()
    try:
        yield
    finally:
        await mgr.stop_all()


app = FastAPI(title="airong LangGraph web", lifespan=lifespan)


# ---------- API ----------

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "node": shutil.which("node"),
        "runtime": "langgraph",
        "runtimeRevision": RUNTIME_REVISION,
        "tools": ["bom_search", "rule_search"],
        "persistenceReady": mgr.saver is not None,
        "cwd": str(PROJECT_DIR),
        "sessions": {sid: b.alive() for sid, b in mgr.bridges.items()},
    }


@app.post("/api/prompt")
async def prompt(req: Request):
    body = await req.json()
    sid = str(body.get("sid") or "").strip()
    message = str(body.get("message") or "").strip()
    if not sid or not message:
        raise HTTPException(400, "sid 与 message 均不能为空")
    try:
        resp = await mgr.prompt(sid, message)
    except RuntimeErrorBase as exc:
        raise HTTPException(502, str(exc)) from exc
    except asyncio.TimeoutError:
        raise HTTPException(504, "等待运行层应答超时") from None
    return {"ok": bool(resp.get("success")), "response": resp}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """上传单文件并提取纯文本（内存操作，不落盘）。

    恒返回 200，用 ok 标志表达业务失败，前端只需分支 j.ok。
    """
    name = file.filename or "未命名"
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return {"ok": False, "name": name, "error": f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限"}
    try:
        text, truncated = await asyncio.to_thread(extract_text, name, data)
    except ExtractError as exc:
        return {"ok": False, "name": name, "error": str(exc)}
    return {"ok": True, "name": name, "text": text, "chars": len(text), "truncated": truncated}


@app.post("/api/abort")
async def abort(req: Request):
    body = await req.json()
    sid = str(body.get("sid") or "").strip()
    if not sid:
        raise HTTPException(400, "sid 不能为空")
    try:
        resp = await mgr.abort(sid)
    except RuntimeErrorBase as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": bool(resp.get("success")), "response": resp}


@app.post("/api/new_session")
async def new_session(req: Request):
    body = await req.json()
    sid = str(body.get("sid") or "").strip()
    if not sid:
        raise HTTPException(400, "sid 不能为空")
    try:
        resp = await mgr.new_session(sid)
    except RuntimeErrorBase as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": bool(resp.get("success")), "response": resp}


@app.post("/api/close")
async def close_session(req: Request):
    """释放运行会话；检查点保留，同 sid 可恢复。"""
    body = await req.json()
    sid = str(body.get("sid") or "").strip()
    if not sid:
        raise HTTPException(400, "sid 不能为空")
    await mgr.close(sid)
    return {"ok": True}


@app.get("/api/events")
async def events(sid: str):
    """SSE：发送兼容客户端的运行事件。"""
    sid = (sid or "").strip()
    if not sid:
        raise HTTPException(400, "sid 不能为空")
    try:
        bridge = await mgr.ensure(sid)
    except RuntimeErrorBase as exc:
        raise HTTPException(502, str(exc)) from exc

    async def stream():
        q = bridge.subscribe()
        try:
            yield "event: hello\ndata: " + json.dumps({"sid": sid}, separators=(",", ":")) + "\n\n"
            while True:
                try:
                    obj = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bridge.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ---------- 静态页面（放最后，兜住 "/" 及未匹配路径） ----------

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="client")
