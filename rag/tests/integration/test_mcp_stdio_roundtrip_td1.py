"""Phase 10 T10d Td.1 IMPROVE — MCP stdio wire-protocol round-trip integration test.

Spawns ``python -m ekrs_rag.mcp.server`` as a subprocess and uses the
official ``mcp.client.stdio`` + ``ClientSession`` to verify:

1. ``initialize`` handshake completes
2. ``list_tools`` reports exactly 2 tools: ``ekrs_search`` + ``ekrs_status``
3. ``call_tool('ekrs_status')`` returns the dependencies dict as
   ``[TextContent(type='text', text=<JSON>)]``
4. ``call_tool('ekrs_search')`` with the default CLI entrypoint (where
   ``retriever=None``) returns ``{"error": "..."}`` content rather than
   crashing the server process

The CLI uses ``retriever=None`` to keep the entrypoint zero-config; this
test then asserts the error-path wire behavior — the production wiring
injects a real retriever before ``server.run()`` is called.

Runs as ``integration`` marker so it's not in the unit fast-loop.
"""
from __future__ import annotations

import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_stdio_server_roundtrip() -> None:
    """Spawn the stdio server, full MCP wire-protocol round-trip."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ekrs_rag.mcp.server"],
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # 1. Initialize handshake
            init_result = await session.initialize()
            assert init_result.serverInfo.name == "ekrs"

            # 2. List tools
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            assert tool_names == {"ekrs_search", "ekrs_status"}, (
                f"unexpected tools: {tool_names}"
            )

            # 3. ekrs_status round-trip — no retriever needed
            status_result = await session.call_tool("ekrs_status", {})
            assert not status_result.isError
            assert len(status_result.content) == 1
            content_block = status_result.content[0]
            assert content_block.type == "text"
            status_payload = json.loads(content_block.text)
            assert status_payload == {"status": "starting"}

            # 4. ekrs_search with default CLI entrypoint (retriever=None)
            #    — must return error content, NOT crash the server.
            search_result = await session.call_tool(
                "ekrs_search",
                {"query": "Q345 钢板", "top_k": 5},
            )
            assert not search_result.isError
            assert len(search_result.content) == 1
            err_payload = json.loads(search_result.content[0].text)
            assert "error" in err_payload, f"missing error field: {err_payload}"
            # The error message will mention "'NoneType' object" — that's
            # the AttributeError raised by None.retrieve() captured by
            # the wrapper. We don't pin the exact text (fragile across
            # Python versions) — just the structural contract.
