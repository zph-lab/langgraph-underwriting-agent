from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from project_paths import PROJECT_DIR


class BomArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keyword: StrictStr = Field(min_length=1, max_length=500)
    limit: StrictInt = Field(default=10, ge=1, le=50)
    includeMethods: StrictBool = False
    dataType: Literal['field', 'object', 'list', 'method'] | None = None


class RuleArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keyword: StrictStr = Field(min_length=1, max_length=500)
    limit: StrictInt = Field(default=6, ge=1, le=30)
    # Scope is configured by the operator, never chosen by the model.


SCHEMAS = {'bom_search': BomArgs, 'rule_search': RuleArgs}
DESCRIPTIONS = {
    'bom_search': '按业务关键词检索 BOM 字段或方法，返回完整父链名称、类型及路径。关键词按整段连续匹配，不会将空格拆词；每次用一个短字段名（如“保单类型”“保额”），勿拼接对象、条件或多个字段；再按完整父链筛选。方法检索须 includeMethods=true。',
    'rule_search': '在配置的业务范围内检索已有规则，空格分词为且关系。返回预定义/如果/那么/否则，动作已填参，须原样展示。',
}


def tool_schemas():
    return [{'type': 'function', 'function': {'name': name, 'description': DESCRIPTIONS[name],
             'parameters': schema.model_json_schema()}} for name, schema in SCHEMAS.items()]


class NodeTools:
    """One serial worker per session; cancellation terminates active DB work."""
    def __init__(self):
        self.proc = None
        self.lock = asyncio.Lock()
        self.stderr_task = None

    async def _drain(self):
        while await self.proc.stderr.read(4096):
            pass

    async def close(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None

    async def call(self, name: str, arguments: dict):
        if name not in SCHEMAS:
            raise ValueError('工具不在业务白名单中')
        args = SCHEMAS[name].model_validate(arguments).model_dump(exclude_none=True)
        async with self.lock:
            try:
                if not self.proc or self.proc.returncode is not None:
                    self.proc = await asyncio.create_subprocess_exec(
                        os.environ.get('AGENT_NODE', 'node'), str(PROJECT_DIR / 'tools/runtime-tools.mjs'),
                        cwd=PROJECT_DIR, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024)
                    self.stderr_task = asyncio.create_task(self._drain())
                cid = uuid.uuid4().hex
                self.proc.stdin.write((json.dumps({'id': cid, 'name': name, 'arguments': args}) + '\n').encode())
                await self.proc.stdin.drain()
                line = await asyncio.wait_for(self.proc.stdout.readline(), 60)
                result = json.loads(line)
                if result.get('id') != cid:
                    raise ValueError('工具响应不匹配')
                return result
            except BaseException:
                await self.close()
                raise
