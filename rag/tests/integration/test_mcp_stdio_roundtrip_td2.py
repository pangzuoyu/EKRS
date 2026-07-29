"""Phase 10 T10d Td.2 IMPROVE — MCP stdio round-trip for 4 tools.

Same wire-protocol harness as Td.1's ``test_mcp_stdio_server_roundtrip``
but extended to verify the Td.2 additions:

3. ``call_tool('ekrs_status')`` (already Td.1) — still works
4. ``call_tool('ekrs_search')`` with default CLI entrypoint (already Td.1)
   — error path because ``retriever=None``
5. ``call_tool('ekrs_query')`` with default CLI entrypoint — error path
   because ``solver=None``
6. ``call_tool('ekrs_get_block')`` with default CLI entrypoint — error path
   because ``qdrant=None``

All 4 tools must be discoverable via ``list_tools``. CLI passes None to
all deps (Td.2.5 zero-config), so every tool call surfaces the
exception-isolation path through real stdio transport.

Runs as ``integration`` marker so it's not in the unit fast-loop.
"""
from __future__ import annotations

import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent


def _as_text(content_block: object) -> TextContent:
    """Narrow MCP content union to TextContent (the only kind our
    ekrs_* tools emit). Raises if a future tool emits non-text content
    — that's a wire-contract violation worth surfacing.
    """
    assert isinstance(content_block, TextContent), (
        f"expected TextContent, got {type(content_block).__name__}"
    )
    return content_block


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_stdio_server_td2_roundtrip() -> None:
    """Spawn the stdio server, verify 4 tools over real wire."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ekrs_rag.mcp.server"],
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # 1. Initialize handshake
            init_result = await session.initialize()
            assert init_result.serverInfo.name == "ekrs"

            # 2. List tools — must report exactly 4 tools now
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            assert tool_names == {
                "ekrs_search",
                "ekrs_status",
                "ekrs_query",
                "ekrs_get_block",
            }, f"unexpected tools: {tool_names}"

            # 3. ekrs_status round-trip — no deps needed
            status_result = await session.call_tool("ekrs_status", {})
            assert not status_result.isError
            status_payload = json.loads(_as_text(status_result.content[0]).text)
            assert status_payload == {"status": "starting"}

            # 4. ekrs_search — retriever=None → error content
            search_result = await session.call_tool(
                "ekrs_search", {"query": "Q345", "top_k": 5},
            )
            assert not search_result.isError
            err_payload = json.loads(_as_text(search_result.content[0]).text)
            assert "error" in err_payload

            # 5. ekrs_query — solver=None → error content
            query_result = await session.call_tool(
                "ekrs_query",
                {
                    "query": "Q345 温度",
                    "context": {},
                    "strict": False,
                    "top_k": 5,
                },
            )
            assert not query_result.isError
            err_payload = json.loads(_as_text(query_result.content[0]).text)
            assert "error" in err_payload

            # 6. ekrs_get_block — qdrant=None → error content
            block_result = await session.call_tool(
                "ekrs_get_block", {"block_id": "any-block-id"},
            )
            assert not block_result.isError
            err_payload = json.loads(_as_text(block_result.content[0]).text)
            assert "error" in err_payload