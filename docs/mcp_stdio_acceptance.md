<!--
MCP stdio acceptance evidence
Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
-->

# MCP Stdio Acceptance

## Verified Scope

On 2026-09-30, the local MCP server was exercised through a real Python MCP
client session against the running `dq_runner` container. The check covered
protocol initialization, tool discovery, and one read-only ClickHouse-backed
tool call. It did not invoke an LLM, write a report, request approval, or execute
remediation.

## Evidence

| Check | Result |
| --- | --- |
| Transport | `stdio` |
| Negotiated protocol | `2025-11-25` |
| Server | `agentic-data-quality-triage` |
| Registered tools | 15 |
| Invoked tool | `get_alert` |
| Data source | Running local ClickHouse service |
| Returned Alert Ref | `DQ-20260610-9C37F7` |
| JSON-RPC parse errors | None after logging fix |
| External LLM calls | None |
| Remediation side effects | None |

The accepted tool registry included alert lookup, guarded SQL, metadata search,
lineage and blast radius, DQ history, pipeline runs, quality summaries, triage,
skill discovery, and report artifact reading.

## Defect Found And Fixed

The first protocol attempt exposed a transport defect: normal project logging
was written to stdout, which contaminated the MCP JSON-RPC stream. The shared
logging module now supports `AGENT_LOG_STREAM`, and the MCP server sets it to
`stderr` before importing project modules. A subprocess regression test verifies
that `--list-tools` emits JSON on stdout while logs remain on stderr.

## Architecture Boundary

MCP is an external client adapter around the same guarded tools used by the
platform. Airflow and the internal LangGraph workflow do not call the MCP server
to perform normal investigations; they use the Python tool layer directly. This
avoids adding a transport dependency inside the runtime while preserving one
auditable integration surface for compatible clients.

This acceptance does not prove OpenClaw compatibility, remote MCP transport,
client-specific configuration, or mutation safety beyond the existing tool and
approval policies. Any client that can invoke triage may incur provider usage
when external LLM execution is deliberately enabled.
