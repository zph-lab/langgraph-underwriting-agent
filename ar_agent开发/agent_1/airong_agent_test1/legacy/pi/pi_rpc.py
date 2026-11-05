"""pi RPC 子进程桥。

以 airong_agent_test1 为 cwd 启动 `node --import <source-resolver.ts> cli.ts --mode rpc`
（逐字复刻 pi-test.ps1:62-64 的启动口径，保证 .pi/extensions 业务工具与人设加载一致），
按 JSONL 行协议双向通信：
- stdin 下发命令 {"id","type",...}；
- stdout 上行 type=="response" 按 id 回填给待答命令，其余事件广播给 SSE 订阅队列；
- 扩展对话框请求（select/confirm/input）自动回 cancelled，防止流程挂起。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from project_paths import PROJECT_DIR, WEB_DIR
PI_DIR = PROJECT_DIR.parent / "pi-main"                 # .../pi-main
CLI_TS = PI_DIR / "packages" / "coding-agent" / "src" / "cli.ts"
RESOLVER_TS = PI_DIR / "packages" / "coding-agent" / "src" / "experimental" / "source-resolver.ts"

# 裸起会落全局 openai key（401），必须显式指定 provider/model；PI_EXTRA_ARGS 可覆盖。
DEFAULT_EXTRA_ARGS = ["--provider", "qwen-maas", "--model", "qwen3.8-flash"]

LINE_LIMIT = 64 * 1024 * 1024          # 单行 JSON 上限（消息快照可能很大）
RESPONSE_TIMEOUT = 60.0                # 命令等待 response 的超时
MAX_BRIDGES = 10                       # 常驻 pi 子进程上限（超出按 LRU 回收无订阅者的）

# pi 的 --session-id 约束（core/session-manager.ts assertValidSessionId）：
# 首尾字母数字，中间允许 [A-Za-z0-9._-]。前端 sid 用 crypto.randomUUID()，天然满足。
SID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _extra_args() -> list[str]:
    raw = os.environ.get("PI_EXTRA_ARGS")
    if raw is None:
        return list(DEFAULT_EXTRA_ARGS)
    return raw.split()


class PiBridgeError(RuntimeError):
    pass


class PiBridge:
    """一个浏览器会话对应一个常驻 pi 子进程。"""

    def __init__(self, sid: str):
        self.sid = sid
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._subs: set[asyncio.Queue] = set()
        self._stderr_tail = ""
        self._dead_reason: str | None = None
        self.last_active = time.time()

    # ---------- 生命周期 ----------

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if self.alive():
            return
        node = shutil.which("node")
        if not node:
            raise PiBridgeError("未找到 node，请确认 Node.js 已安装并在 PATH 中")
        if not CLI_TS.exists():
            raise PiBridgeError(f"未找到 pi CLI：{CLI_TS}")
        if not RESOLVER_TS.exists():
            raise PiBridgeError(f"未找到 source-resolver：{RESOLVER_TS}")
        if not SID_RE.match(self.sid):
            raise PiBridgeError(f"非法会话 id：{self.sid!r}")
        self._dead_reason = None
        self.proc = await asyncio.create_subprocess_exec(
            node,
            "--import", RESOLVER_TS.as_uri(),   # --import 需要 file:// URL（Windows 路径不是 specifier）
            str(CLI_TS),
            "--mode", "rpc",
            # 会话持久化：sid 即 pi 的 session id，空闲回收/重启后按 id 从磁盘恢复上下文
            "--session-id", self.sid,
            *_extra_args(),
            cwd=str(PROJECT_DIR),               # 决定 .pi/extensions / APPEND_SYSTEM.md 加载
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = self._stderr_task = None
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 5)
            except (TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        self._fail_all_pending("bridge 已停止")

    def _fail_all_pending(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(PiBridgeError(reason))
        self._pending.clear()

    # ---------- 上行 / 下行 ----------

    async def _write(self, obj: dict) -> None:
        if not self.alive():
            raise PiBridgeError("pi 子进程未运行")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()

    async def send(self, cmd_type: str, timeout: float = RESPONSE_TIMEOUT, **fields) -> dict:
        """下发一条 RPC 命令并等待对应 response（{"type":"response", "success", ...}）。"""
        await self.start()
        cid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        try:
            await self._write({"id": cid, "type": cmd_type, **fields})
            self.last_active = time.time()
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cid, None)

    async def _read_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue  # 非 JSON 杂项输出直接忽略
                if not isinstance(obj, dict):
                    continue
                self._route(obj)
        except (asyncio.CancelledError, ValueError):
            return
        except Exception as exc:  # noqa: BLE001
            self._dead_reason = f"读取子进程输出失败：{exc}"
        finally:
            code = proc.returncode
            self._fail_all_pending(self._dead_reason or "pi 子进程已退出")
            if self.alive() or code is None:
                self._dead_reason = self._dead_reason or f"pi 子进程已退出(code={code})"
            self.proc = None
            self._broadcast({"type": "bridge_dead", "reason": self._dead_reason or "pi 子进程已退出",
                             "stderr": self._stderr_tail[-500:]})

    def _route(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "response":
            fut = self._pending.get(obj.get("id"))
            if fut is not None and not fut.done():
                fut.set_result(obj)
            return
        if t == "extension_ui_request":
            method = obj.get("method")
            if method in ("select", "confirm", "input") and obj.get("id"):
                # 无 TUI 可弹，自动取消，防止 agent 挂起
                asyncio.create_task(self._safe_cancel_ui(obj["id"]))
                return
            # notify / setStatus / setWidget 等被动消息继续转发给前端
        self._broadcast(obj)

    async def _safe_cancel_ui(self, rid: str) -> None:
        try:
            await self._write({"type": "extension_ui_response", "id": rid, "cancelled": True})
        except Exception:  # noqa: BLE001
            pass

    async def _drain_stderr(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail = (self._stderr_tail + chunk.decode("utf-8", errors="replace"))[-4000:]
        except (asyncio.CancelledError, ValueError):
            return

    # ---------- SSE 订阅 ----------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def subscriber_count(self) -> int:
        return len(self._subs)

    def _broadcast(self, obj: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(obj)
            except asyncio.QueueFull:
                try:  # 溢出丢最旧，保证推流不断
                    q.get_nowait()
                    q.put_nowait(obj)
                except asyncio.QueueEmpty:
                    pass


class PiSessionManager:
    """sid -> PiBridge，带空闲回收。"""

    def __init__(self, idle_seconds: float = 30 * 60):
        self.bridges: dict[str, PiBridge] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self.idle_seconds = idle_seconds

    async def ensure(self, sid: str) -> PiBridge:
        async with self._lock:
            bridge = self.bridges.get(sid)
            if bridge is None or not bridge.alive():
                if bridge is not None:
                    await bridge.stop()
                    self.bridges.pop(sid, None)
                await self._evict_for_capacity()
                bridge = PiBridge(sid)
                self.bridges[sid] = bridge
                await bridge.start()
            return bridge

    async def _evict_for_capacity(self) -> None:
        """子进程总数达上限时，先回收最久未活动且无 SSE 订阅者的会话（有订阅者的不动，避免掐断在线用户）。"""
        while len(self.bridges) >= MAX_BRIDGES:
            cands = [b for b in self.bridges.values() if b.subscriber_count() == 0]
            if not cands:
                raise PiBridgeError(f"会话数已达上限（{MAX_BRIDGES}），请稍后再试")
            victim = min(cands, key=lambda b: b.last_active)
            self.bridges.pop(victim.sid, None)
            await victim.stop()

    async def prompt(self, sid: str, message: str) -> dict:
        bridge = await self.ensure(sid)
        return await bridge.send("prompt", message=message)

    async def abort(self, sid: str) -> dict:
        bridge = self.bridges.get(sid)
        if bridge is None or not bridge.alive():
            raise PiBridgeError("会话未运行")
        return await bridge.send("abort")

    async def new_session(self, sid: str) -> dict:
        bridge = await self.ensure(sid)
        return await bridge.send("new_session")

    async def close(self, sid: str) -> None:
        """删除会话时杀掉该 sid 的子进程（会话文件保留在磁盘，sid 不变仍可恢复）。"""
        async with self._lock:
            bridge = self.bridges.pop(sid, None)
            if bridge is not None:
                await bridge.stop()

    async def stop_all(self) -> None:
        if self._reaper and not self._reaper.done():
            self._reaper.cancel()
        async with self._lock:
            for bridge in list(self.bridges.values()):
                await bridge.stop()
            self.bridges.clear()

    def start_reaper(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                for sid, bridge in list(self.bridges.items()):
                    if (bridge.alive() and bridge.subscriber_count() == 0
                            and now - bridge.last_active > self.idle_seconds):
                        await bridge.stop()
                        self.bridges.pop(sid, None)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                continue
