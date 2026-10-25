import asyncio
import getpass
import json
import os
from pathlib import Path
from typing import Any

import httpx2
import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
MCP_URL = os.getenv("ARCHIMATE_MCP_URL", "http://localhost:8001/mcp")


def load_env_file() -> None:
    """Load environment variables from the .env beside this script."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def json_text(value: Any) -> str:
    """Return the text content of an MCP result."""
    content = getattr(value, "content", None)
    if content:
        return "\n".join(c.text for c in content if hasattr(c, "text"))
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


async def main() -> None:
    load_env_file()

    token = (
        os.getenv("ARCHI_TOKEN")
        or os.getenv("MCP_TOKEN")
        or ""
    ).strip()

    if not token:
        token = getpass.getpass("MCP bearer token: ").strip()

    if not token:
        raise SystemExit(
            "No token provided. Set ARCHI_TOKEN in .env."
        )

    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
    )

    stream_context = None
    session_context = None

    try:
        # MCP SDK 2.3.0: use streamable_http_client.
        stream_context = streamable_http_client(
            MCP_URL,
            http_client=http_client,
        )

        # This SDK returns two streams.
        read_stream, write_stream = await stream_context.__aenter__()

        session_context = ClientSession(read_stream, write_stream)
        session = await session_context.__aenter__()
        await session.initialize()

        listed = await session.list_tools()

        print(f"\nConnected to Vrio MCP: {MCP_URL}")
        print(f"Tools found: {len(listed.tools)}")

        for tool in listed.tools:
            print(f"  - {tool.name}")

        if not listed.tools:
            raise SystemExit(
                "No tools returned. Check the MCP server configuration."
            )

        tool_routes: dict[str, str] = {}
        ollama_tools: list[dict[str, Any]] = []

        for tool in listed.tools:
            exposed_name = f"vrio__{tool.name}"
            tool_routes[exposed_name] = tool.name

            ollama_tools.append({
                "type": "function",
                "function": {
                    "name": exposed_name,
                    "description": tool.description or tool.name,
                    "parameters": tool.input_schema or {
                        "type": "object",
                        "properties": {},
                    },
                },
            })

        messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": (
                "You are an ArchiMate architecture assistant working with one "
                "model named Vrio. The model may contain AS-IS and TO-BE views. "
                "Use MCP tools to inspect actual model data before answering "
                "questions about views, elements, relationships, or comparisons. "
                "Do not invent model contents. Distinguish retrieved facts from "
                "recommendations. If the available tools do not provide enough "
                "information, say so."
            ),
        }]

        print(f"\nOllama model: {OLLAMA_MODEL}")
        print("Ask questions about the AS-IS and TO-BE architecture.")
        print("Type /exit to quit.")

        while True:
            try:
                question = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break

            if not question:
                continue

            if question.lower() in {"/exit", "/bye", "exit", "quit"}:
                break

            messages.append({
                "role": "user",
                "content": question,
            })

            for _ in range(8):
                response = requests.post(
                    OLLAMA_URL,
                    json={
                        "model": OLLAMA_MODEL,
                        "messages": messages,
                        "tools": ollama_tools,
                        "stream": False,
                    },
                    timeout=300,
                )
                response.raise_for_status()

                assistant_message = response.json().get("message", {})
                messages.append(assistant_message)

                calls = assistant_message.get("tool_calls") or []

                if not calls:
                    answer = (
                        assistant_message.get("content") or ""
                    ).strip()
                    print(
                        f"\nAssistant: "
                        f"{answer or '[No text response returned]'}"
                    )
                    break

                for call in calls:
                    function = call.get("function", {})
                    exposed_name = function.get("name", "")
                    arguments = function.get("arguments", {})

                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}

                    original_name = tool_routes.get(exposed_name)

                    if not original_name:
                        result_text = (
                            f"Unknown tool requested: {exposed_name}"
                        )
                    else:
                        try:
                            result = await session.call_tool(
                                original_name,
                                arguments=arguments,
                            )
                            result_text = json_text(result)
                        except Exception as exc:
                            result_text = (
                                f"Tool call failed: "
                                f"{type(exc).__name__}: {exc}"
                            )

                    print(
                        f"\n[Tool executed: "
                        f"{original_name or exposed_name}]"
                    )
                    print(result_text[:5000])

                    messages.append({
                        "role": "tool",
                        "tool_name": exposed_name,
                        "content": result_text[:12000],
                    })
            else:
                print(
                    "\nStopped after 8 tool-call rounds "
                    "to prevent an infinite loop."
                )

    finally:
        # Close each resource even if another cleanup step fails.
        if session_context is not None:
            try:
                await session_context.__aexit__(None, None, None)
            except Exception:
                pass

        if stream_context is not None:
            try:
                await stream_context.__aexit__(None, None, None)
            except Exception:
                pass

        try:
            await http_client.aclose()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())