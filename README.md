# ArchiMate + free AI agent (Docker)

One read-only ArchiMate MCP server on your Vrio project + Ollama (free local model) + Open WebUI (chat/agent).

## 1. Put your project in `models/`
In Archi, open your Vrio project, then File > Export > Model to Open Exchange File...
Save it as `models/vrio.xml` (this exact name). Archi's normal save format cannot be read by the
server; the Open Exchange export can. AS-IS and TO-BE stay as two views of this one model.
After editing the project, re-export and run `docker compose restart archimate-vrio`.

(Optional: `convert_archi.py` does the same conversion from a native file:
`pip install archimate-mcp` then `python convert_archi.py Vrio.archimate models/vrio.xml`.)

## 2. Create the token
    cp .env.example .env
    python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste the result in .env as ARCHI_TOKEN

## 3. Start
    docker compose up -d --build
    docker compose exec ollama ollama pull qwen3:8b      # free model with tool calling; ~5 GB

## 4. Connect the two servers in Open WebUI (http://localhost:3000)
Create the local admin account, then Settings → Admin Settings → External Tools → Add (type: MCP, Streamable HTTP):

| Name | URL | Auth |
|------|-----|------|
| ArchiMate Vrio | http://archimate-vrio:8000/mcp | Bearer + the ARCHI_TOKEN value |

(Menu names can differ between Open WebUI versions. If there is no MCP option, update the image.)
Open a new chat, pick the model, enable the tool, then paste the prompts from `prompts.md` in order.

## Troubleshooting
- "Rate limit exceeded": the server throttles calls. Wait a few seconds and ask the agent to retry.
- The agent invents data or ignores tools: use a larger model, or a free hosted API (Gemini free tier) in Open WebUI.
- The servers are READ-ONLY by design (models mounted `:ro`). The agent analyses; it does not edit your models.
- Changed the project in Archi? Re-export it (step 1), then `docker compose restart archimate-vrio`.

## What was tested
In a sandbox, with archimate-mcp 2.6.3 and a converted copy of the Vrio project: token auth (401 without a token),
and queries on elements, relationships and views (the AS-IS view shows "Service de facturation" realized by both
billing apps). NOT tested: Open WebUI / Ollama (could not run them here).
# vrio-agent
