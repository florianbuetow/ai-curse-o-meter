# AI Curse-O-Meter: How Often Do You Swear at Your Coding Agents?

![Made with AI](https://img.shields.io/badge/Made%20with-AI-333333?labelColor=f00) ![Verified by Humans](https://img.shields.io/badge/Verified%20by-Humans-333333?labelColor=brightgreen)

Scans your local Claude, Codex, Gemini / Antigravity, and Grok session histories for curse words in your own prompts and charts the daily counts per tool. Everything runs offline against files already on your machine.

## Quick Start

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), [just](https://github.com/casey/just#installation)

```bash
just init
just run
```

`just run` discovers the session logs, updates the per-log cache, writes the daily totals, and renders the chart. Progress updates in place per source, followed by a per-source summary.

## Outputs

| File | Contents |
| --- | --- |
| `data/output/daily_counts.json` | Curse-word counts per calendar day and source |
| `data/output/daily_counts.svg` | Chart with one colored line per source |
| `data/output/daily_counts.png` | Rasterized copy of the chart |

A zero appears on the chart only when you interacted with that tool on that day without a match. Days with no interaction are omitted.

## Supported Sources

| Source | Session store |
| --- | --- |
| Claude | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/thread_history_*.sqlite` |
| Gemini / Antigravity | `~/.gemini/**/.system_generated/logs/transcript.jsonl` |
| Grok | `~/.grok/sessions/**/chat_history.jsonl` |

Only direct user prompts are counted, never model output or tool results. Each count is assigned to the day the prompt was captured.

## How Matching Works

- Terms come from [`config/curse_words.json`](config/curse_words.json); edit the list to change what counts.
- Matching is case-insensitive and whole-word aware.
- When terms overlap, the longest match wins, so a phrase is counted once and not again for a shorter term inside it.

## Caching

Each analyzed log gets one cache file under `data/cache/`. A cached log is reused while its source file size is unchanged; if the size changes, the log is rescanned and its entry replaced. Delete `data/cache/` to force a full rescan.

## Commands

| Command | Description |
| --- | --- |
| `just` | Show all commands |
| `just init` | Install the locked environment |
| `just run` | Scan sessions and generate outputs |
| `just test` | Run the test suite |
| `just code-style` | Check formatting and lint rules |
| `just code-typecheck` | Run mypy |
| `just code-lspchecks` | Run Pyright |
| `just code-audit` | Scan dependencies for known vulnerabilities |
| `just score` | Score the codebase against the guardrail rules |
| `just ci` | Run the full validation pipeline |

## Development

All Python runs through `uv`. Development rules and quality gates are described in [AGENTS.md](AGENTS.md).
