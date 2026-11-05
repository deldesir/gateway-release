# AI Gateway

The LLM gateway for a WhatsApp/email-first personal assistant + CRM. It fronts
every conversation the box has: a deterministic router answers known intents
for zero tokens, and anything else goes to an agent (Hermes with a native provider, Gemini or Anthropic)
with a per-caller-scoped toolset. FastAPI, runs as systemd `ai-gateway` on
`127.0.0.1:8086`, executed straight from this tree with `uv` — no build step.

> For contributor/agent orientation see [`CLAUDE.md`](CLAUDE.md). This repo is
> **public**: no real phone numbers, personal contact details, fleet domains,
> or business names in anything committed.

## What it does

- **Channels.** WhatsApp (via wuzapi → RapidPro flows) and email (via the
  email-bridge). RapidPro's `call_llm` hits the OpenAI-compatible endpoint in
  `app/api/adapters/openai.py`.
- **Two-hop conversation.** RiveBot (`:8087`) matches deterministic intents
  first; unmatched turns fall through to the Hermes agent, which calls tools.
- **Personas.** Channel/slug → persona (`app/services/channel.py`), each with
  a scoped toolset. `DEFAULT_PERSONA` is `general` (chat-only); the privileged
  `assistant` persona is bound to the operator's URNs only.
- **Memory.** MemPalace gives each caller a private wing (keyed by URN, with
  operator identities unified); the knowledge graph and palace-wide status are
  operator-only.

## Access control (RBAC)

Access is granted from chat by the operator, through one tool:

```
access grant <phone|email> <member|staff|teacher|admin> [name]
access revoke <phone|email>
access list
```

This is the **only** promotion path (`app/graph/tools/access.py`). It writes
`data/access_grants.json`, syncs the caller's RapidPro tier group, and flushes
permission caches immediately.

| Tier | Adds |
|---|---|
| `member` | talk to the assistant (stranger silence lifted) |
| `staff` / `teacher` | + CRM lookup/ops (`rapidpro` toolset) |
| `admin` | + talkprep, upload, social, forms + RapidPro **Admins** group |

Two enforcement layers, both fail-closed:

1. **Chat gate** (`openai.py`): a caller must be in `AUTHORIZED_USERS` (the
   operator bootstrap) or the grants file — everyone else is silently dropped.
2. **Tool authorization** (`app/api/adapters/tools.py`, the final authority):
   every tool call is checked against the caller's toolsets (persona
   `allowed_urns` grants ∪ tier toolsets). RiveBot's macro-bridge check is only
   the first, advisory layer.

**Operator-only, never grantable:** `terminal`, `code_execution`, `config`,
`system`, `email`, `siyuan`, `file`, `organized`, and the knowledge graph.

## Layout

| Path | Purpose |
|---|---|
| `app/api/adapters/` | `openai.py` (chat + gate), `tools.py` (`/v1/tools/*` + authz) |
| `app/graph/tools/` | tool implementations (`access`, `config`, `system`, `organized`, `rapidpro`, `email`, …) |
| `app/hermes/` | agent engine, tool registry (`tools.py`), schemas |
| `app/services/channel.py` | channel → persona routing |
| `app/seed.py` | persona seeds (privileged personas re-seeded from `ADMIN_PHONE` at startup) |
| `app/jobs/` | timer jobs (`ai-gateway-*`: daily/weekly synthesis, kg_extract, radar) |

## Run & verify

```bash
uv run uvicorn app.api.app:create_app --factory   # (systemd does this)
systemctl restart ai-gateway && systemctl is-active ai-gateway
curl -s localhost:8086/health
```

The LLM provider is one of Hermes' native providers, pinned by `HERMES_PROVIDER`
(`gemini` + `GEMINI_API_KEY`, or `anthropic` + `ANTHROPIC_API_KEY`) with the model
in `LLM_MODEL` — **LiteLLM was removed**, don't reintroduce it. State lives in `.env`/`.env.local` (gitignored) and
`data/`; both are covered by the nightly `/usr/local/sbin/second-brain-backup`.

## Attribution

Originally seeded from the [Scranton Agents](https://github.com/ayushtiwari134/scranton-agents)
architecture; since rewritten into this two-hop RBAC gateway.
