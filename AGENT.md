# AGENTS.md
Strict guidance for Code Agents working on the `astrbot_plugin_pulse` repository.

## Project Overview
`astrbot_plugin_pulse` is a daily information push plugin built on the Astrbot QQ robot framework. 
Core features: Automated daily broadcasting of Epic Games free promotions and LLM-synthesized AI industry daily briefs.

## CRITICAL ARCHITECTURE RULES (High-Voltage Lines)
1. **Edge-Computation Data Fetching:** NEVER write complex web scrapers or raw RSS parsers inside the Python plugin. All foreign AI media streams MUST be pre-fetched and cleaned via a remote Cloudflare Worker endpoint, returning a standardized, clean JSON string.
2. **Framework-Native LLM Context:** When synthesizing AI daily summaries, MUST invoke Astrbot's built-in/configured LLM provider context. DO NOT hardcode external OpenAI/Gemini API keys inside the codebase.
3. **Pure Asynchronous I/O:** All network operations (fetching Epic API or Cloudflare Worker) MUST utilize `async/await` with non-blocking libraries like `httpx` or `aiohttp`. Synchronous `requests` or `time.sleep` are strictly prohibited to prevent freezing the robot's main thread.

## Directory Structure & References
- `main.py` / `__init__.py`: Plugin entry point and lifecycle event registrations.
- `docs/dev/`: Official Astrbot framework documentation. Always inspect API specs here before writing code.
- **Scheduling:** Leverage Astrbot's native scheduler/cron mechanisms for the morning broadcast trigger. Do not introduce custom background looping threads.

## Code Style & Compliance
- **Language:** Code comments and logging statements SHOULD prefer Chinese for easier debugging by the owner.
- **Error Handling:** Wrap all network and parsing logic in robust `try-except` blocks. Log errors gracefully via the framework's logger instead of crashing the plugin.
- **Formatting:** Keep output messages visually optimized for QQ users. Epic games must include thumbnail images while adhering to QQ message length boundaries.

## AI Collaboration Rules
- **Look Before You Leap:** Before altering any Astrbot event listener hooks, analyze the official lifecycle documentation in `./docs/dev/` to ensure no conflict with other active plugins.
- **Incremental Refactoring:** When changing schema formats, provide backward compatibility logic for the current state cache.