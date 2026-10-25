import asyncio
import getpass
import json
import os
from typing import Any

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

# These are the two MCP endpoints from the current Docker Compose setup.
MCP_SERVERS = {
    "asis": os.getenv("ARCHIMATE_ASIS_MCP_URL", "http://localhost:8001/mcp"),
    "tobe": os.getenv("ARCHIMATE_TOBE_MCP_URL", "http://localhost:8002/mcp"),
}


def json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


async def main() -> None:
    token = os.getenv("MCP_TOKEN")
    if not token:
        token = getpass.getpass(
            "MCP bearer token (the same token configured for Open WebUI): "
        ).strip()
    if not token:
        raise SystemExit("No token provided. Set MCP_TOKEN or run the script again.")

    headers = {"Authorization": f"Bearer {token}"}
    sessions: dict[str, ClientSession] = {}
    streams: list[Any] = []
    tool_routes: dict[str, tuple[str, str]] = {}
    ollama_tools: list[dict[str, Any]] = []

    try:
        # Connect to each configured MCP server and discover its real tool schemas.
        for server_name, url in MCP_SERVERS.items():
            try:
                stream_context = streamablehttp_client(url, headers=headers)
                read_stream, write_stream, _ = await stream_context.__aenter__()
                streams.append(stream_context)

                session = ClientSession(read_stream, write_stream)
                await session.__aenter__()
                sessions[server_name] = session
                await session.initialize()

                listed = await session.list_tools()
                print(f"\nConnected to {server_name.upper()} MCP: {url}")
                print("Tools found:", len(listed.tools))

                for tool in listed.tools:
                    # Prefix names to avoid collisions between AS-IS and TO-BE.
                    exposed_name = f"{server_name}__{tool.name}"
                    tool_routes[exposed_name] = (server_name, tool.name)
                    schema = tool.inputSchema or {
                        "type": "object",
                        "properties": {},
                    }
                    ollama_tools.append({
                        "type": "function",
                        "function": {
                            "name": exposed_name,
                            "description": (
                                f"[{server_name.upper()} model] "
                                f"{tool.description or tool.name}"
                            ),
                            "parameters": schema,
                        },
                    })
            except Exception as exc:
                print(f"\nWARNING: Could not connect to {server_name.upper()} at {url}")
                print(f"Reason: {exc}")

        if not sessions or not ollama_tools:
            raise SystemExit(
                "\nNo MCP tools were discovered. Check that Docker is running, "
                "the endpoints are correct, and the bearer token is valid."
            )

        print("\nReady. Type a question; type /exit to quit.")
        print(f"Ollama model: {OLLAMA_MODEL}")
        print("Example: List the views in the AS-IS model.")

        messages: list[dict[str, Any]] = [{
            "role": "system",
            "content": (
                "You are an ArchiMate architecture assistant. Use the available "
                "MCP tools whenever a question asks about the actual models. "
                "Do not invent model names, view names, counts, or tool results. "
                "Clearly distinguish AS-IS from TO-BE. If a tool fails, report "
                "the error instead of guessing."
            ),
        }]

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

            messages.append({"role": "user", "content": question})

            # Let Ollama request tools; execute them through the MCP sessions;
            # then give the real tool results back to Ollama.
            for _ in range(6):
                response = requests.post(
                    OLLAMA_URL,
                    json={
                        "model": OLLAMA_MODEL,
                        "messages": messages,
                        "tools": ollama_tools,
                        "stream": False,
                    },
                    timeout=180,
                )
                response.raise_for_status()
                assistant_message = response.json().get("message", {})
                messages.append(assistant_message)

                calls = assistant_message.get("tool_calls") or []
                if not calls:
                    print(f"\nAssistant: {assistant_message.get('content', '').strip()}")
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

                    route = tool_routes.get(exposed_name)
                    if not route:
                        result_text = f"Unknown tool requested: {exposed_name}"
                    else:
                        server_name, original_tool_name = route
                        try:
                            result = await sessions[server_name].call_tool(
                                original_tool_name,
                                arguments=arguments,
                            )
                            result_text = json_text(result)
                        except Exception as exc:
                            result_text = f"Tool call failed: {type(exc).__name__}: {exc}"

                    print(f"\n[Tool executed: {exposed_name}]")
                    print(result_text[:5000])
                    messages.append({
                        "role": "tool",
                        "tool_name": exposed_name,
                        "content": result_text[:12000],
                    })
            else:
                print("\nStopped after 6 tool-call rounds to avoid an infinite loop.")

    finally:
        for session in reversed(list(sessions.values())):
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
        for stream_context in reversed(streams):
            try:
                await stream_context.__aexit__(None, None, None)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
