# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A FastMCP 3.x server exposing 12 Google Workspace services (Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Chat, Apps Script, Tasks, Contacts, Custom Search) to MCP clients. Python 3.10+, managed with `uv`.

## Commands

```bash
uv sync --group dev                       # install lint + test + release tooling
uv run ruff check .                       # lint (pre-push git hook also runs this — run before committing)
uv run pytest                             # full test suite (async fixtures need pytest-asyncio, already configured)
uv run pytest tests/gmail/test_draft_gmail_message.py            # single test file
uv run pytest tests/gmail/test_draft_gmail_message.py::test_name  # single test
uv run pytest -m "not integration"        # skip tests that hit real external services
uv run main.py --transport streamable-http   # run server against checked-out code for manual verification
uv run workspace-cli list                 # list registered tools (CLI / Code Mode entrypoint)
uv run workspace-cli call <tool> key=value...
```

`uv sync --group test` installs only the test stack if you need a slimmer env. The `integration` pytest marker gates tests requiring live services.

## Architecture

**Entry point flow.** `main.py` parses args/env, resolves which services and tool tiers to load, then *lazily imports* service modules from the `SERVICE_MODULES` map (`main.py:127`). Importing a service module is what registers its `@server.tool` functions — there is no central tool registry to edit. `core/server.py` owns the singleton `server` (FastMCP instance), middleware, and OAuth callback routes.

**Service modules** live in `g{service}/` directories (e.g. `gmail/gmail_tools.py`, `gdrive/drive_tools.py`). Each contains MCP tool functions plus a `_helpers.py` for non-tool logic. To add a service, add it to `SERVICE_MODULES` in `main.py` and `SERVICE_CONFIGS` in `auth/service_decorator.py`.

**Authentication is decorator-driven.** Tools never construct Google clients directly:

```python
from auth.service_decorator import require_google_service

@require_google_service("drive", "drive_read")   # service type + scope group name
async def your_tool(service, param: str):
    """One-sentence present-tense description."""
    return service.files().list().execute()        # service injected + cached (~30 min TTL)
```

`require_google_service` (`auth/service_decorator.py:682`) resolves scope groups, injects an authenticated cached client as the first `service` arg, and attaches required scopes to the wrapper for tier/permission filtering. Use `require_multiple_services()` for tools needing several Google APIs. Scopes and scope-group names are centralized in `auth/scopes.py` — never hardcode scope URLs in tools.

**Auth modes are mutually exclusive** (combining them is a startup error):
- Single-user (`--single-user`): legacy, user email passed in tool calls / `USER_GOOGLE_EMAIL`.
- OAuth 2.1 (`MCP_ENABLE_OAUTH21=true`): multi-user bearer tokens; reuses `GOOGLE_OAUTH_CLIENT_ID`/`SECRET`.
- Service account (`GOOGLE_SERVICE_ACCOUNT_KEY_FILE`): headless / domain-wide delegation.

**Tool tiers.** `core/tool_tiers.yaml` (packaged as package-data) maps every tool into `core` / `extended` / `complete` per service. `--tool-tier` / `WORKSPACE_MCP_TOOL_TIER` selects which load. **Any new tool must be added to `tool_tiers.yaml` or it won't be discoverable via tiers.** `--tools <service...>` filters by service; `--permissions service:level` and `--read-only` filter by granted scope (read-only requests only read-only scopes and disables write tools).

**Transports.** `stdio` (default, MCP-over-stdio — stray stdout corrupts the JSON-RPC handshake; see the macOS stdout-capture guard at the top of `main.py`) or `streamable-http` (FastAPI/Starlette, multi-user). `core/cli.py` provides the `workspace-cli` "Code Mode" client for local or remote instances.

## Conventions

- Tool names: imperative, ≤3 words. Descriptions: single present-tense sentence with parameter hints. Return native Python objects, not hand-built error strings — raise; the decorator/`core.utils.handle_http_errors` surface `ToolExecutionError`.
- Keep `strict_input_validation` **False** unless there's a compelling reason (brittle clients otherwise). Tool signatures must use primitive or Pydantic-v2 types only (LLM-friendly JSON schema).
- Blocking Google client calls should run async or via `run_in_executor`; batch API requests and paginate large result sets.
- File reads default to the managed attachment dir; `core.utils.validate_file_path()` blocks `.env*` and credential stores (`~/.ssh/`, `~/.aws/`) — don't bypass it.
- Tag experimental tools with `tags={"beta"}`. Mock Google APIs in tests; never hit live services in CI (use the `integration` marker for ones that must).
