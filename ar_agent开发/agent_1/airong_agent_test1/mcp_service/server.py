"""Optional stdio MCP service: python -m mcp_service.server (cwd=project root)."""
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from tools.bridge import NodeTools

mcp = FastMCP('airong-readonly-tools')


async def execute(name, args):
    # Per-call worker makes cancellation and lifespan explicit for external clients.
    tools = NodeTools()
    try:
        result = await tools.call(name, args)
        return CallToolResult(content=[TextContent(type='text', text=part['text']) for part in result['content']],
                              structuredContent=result, isError=bool(result.get('isError')))
    finally:
        await tools.close()


@mcp.tool()
async def bom_search(keyword: str, limit: int = 10, includeMethods: bool = False,
                     dataType: str | None = None) -> CallToolResult:
    """检索核保 BOM 字段和方法，保留完整父链；只读。"""
    return await execute('bom_search', {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
async def rule_search(keyword: str, limit: int = 6) -> CallToolResult:
    """在管理员配置的范围内检索完整四段式规则；只读。"""
    return await execute('rule_search', {'keyword': keyword, 'limit': limit})


if __name__ == '__main__':
    mcp.run(transport='stdio')
