"""Tiri CLI — thin wrapper over RoomManager and RoomEngine.

Mandated by CLAUDE.md. Not a separate architecture layer — uses the same
container wiring as the API server. All commands read config via
Config.load() (respects tiri.toml + env vars) and exit non-zero on error
with a human-readable message.

Commands:
  load-room <config.json>          create or update a room from a JSON config
  ask --room <id> "<question>"     ask a one-shot question
  benchmark --room <id>            run room benchmarks; exit non-zero if < 100%
  dump --room <id>                 print the current RoomConfig as JSON
  serve [--host H --port P]        start the FastAPI app via uvicorn
  import-genie --space-id <id> | --input <path>  --output <path>
                                   translate a Genie Space export to RoomConfig JSON

Invoke as: `python -m tiri.cli <command> [args...]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tiri.config import Config, ConfigurationError
from tiri.container import build_container
from tiri.data_models import RoomConfig
from tiri.engine.room_engine import (
    RoomEngine,
    RoomManager,
    RoomNotFoundError,
)


# ── command implementations ────────────────────────────────────────────────


async def _cmd_load_room(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.config).read_text())
    config = RoomConfig.from_dict(raw)
    cfg = Config.load()
    container = build_container(cfg)
    manager = _manager(container)

    # Idempotent: create on first run, update if already present.
    existing = await container["store"].get(f"room:{config.room_id}:config")
    if existing is None:
        await manager.create(config)
        verb = "created"
    else:
        await manager.update(config.room_id, raw)
        verb = "updated"

    print(
        f"Room {verb}: {config.title!r} ({config.room_id}). "
        f"Indexed {len(config.examples)} examples."
    )
    return 0


async def _cmd_ask(args: argparse.Namespace) -> int:
    import uuid

    cfg = Config.load()
    container = build_container(cfg)
    engine = _engine(cfg, container)
    conversation_id = uuid.uuid4().hex

    try:
        turn = await engine.chat(
            room_id=args.room,
            conversation_id=conversation_id,
            question=args.question,
        )
    except RoomNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if turn.error:
        print(f"Error: {turn.error}", file=sys.stderr)
        return 1
    if turn.clarification_question:
        print(f"Clarification needed: {turn.clarification_question}")
        return 0

    # Prose answer: prefer SynthesizedAnswer, fall back to viz summary.
    if turn.synthesized_answer is not None:
        sa = turn.synthesized_answer
        print(sa.answer)
        if sa.data_supports:
            print("\nData supports:")
            for b in sa.data_supports:
                print(f"  - {b}")
        if sa.data_does_not_support:
            print("\nData does not support:")
            for b in sa.data_does_not_support:
                print(f"  - {b}")
        if sa.would_need:
            print("\nWould need:")
            for b in sa.would_need:
                print(f"  - {b}")
        print(f"\nConfidence: {sa.confidence} ({sa.confidence_rationale})")
    elif turn.viz and turn.viz.summary:
        print(turn.viz.summary)

    if turn.sql:
        print(f"\nSQL used:\n  {turn.sql}")
    if turn.query_result is not None:
        print(
            f"\nRows: {turn.query_result.row_count}"
            f"{' (truncated)' if turn.query_result.truncated else ''}"
        )
    return 0


async def _cmd_benchmark(args: argparse.Namespace) -> int:
    from tiri.feedback.benchmark_runner import BenchmarkRunner

    cfg = Config.load()
    container = build_container(cfg)
    engine = _engine(cfg, container)
    runner = BenchmarkRunner(engine, store_query=container["query"])

    try:
        report = await runner.run(args.room)
    except RoomNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Room: {report.room_id}  ({report.run_at})")
    print(f"Score: {report.score:.0%} ({report.passed}/{report.total})\n")
    for r in report.results:
        marker = "PASS" if r.passed else "FAIL"
        print(f"  [{marker}] {r.benchmark_id}: {r.question}")
        if not r.passed:
            if r.error:
                print(f"          error: {r.error}")
            elif r.generated_sql:
                print(f"          generated: {r.generated_sql[:120]}")
                print(f"          expected:  {r.expected_sql[:120]}")

    # Exit non-zero on anything less than 100% — the CLAUDE.md DoD target.
    return 0 if report.score >= 1.0 else 1


async def _cmd_dump(args: argparse.Namespace) -> int:
    cfg = Config.load()
    container = build_container(cfg)
    manager = _manager(container)
    try:
        config = await manager.get(args.room)
    except RoomNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(config), indent=2, default=str))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # Imported lazily — uvicorn is a serve-only dep.
    import uvicorn

    uvicorn.run(
        "tiri.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def _cmd_import_genie(args: argparse.Namespace) -> int:
    """Translate a Genie Space export to a Tiri RoomConfig JSON file.

    Two input modes:
      --input <path>      read a local Genie wire-format JSON (testable, no network)
      --space-id <id>     fetch via Databricks Workspace API (requires
                          DATABRICKS_HOST + DATABRICKS_TOKEN in env)

    Output is written to --output. The user must add `warehouse_id` and
    `room_id` (if not derivable from the source) before running load-room.
    Translation rules per docs/roadmap.md R5.
    """
    if args.input:
        genie = json.loads(Path(args.input).read_text())
    elif args.space_id:
        cfg = Config.load()
        genie = _fetch_genie_space(args.space_id, cfg=cfg)
    else:
        print(
            "import-genie requires either --input <path> or --space-id <id>",
            file=sys.stderr,
        )
        return 1

    room = _genie_to_room_config(genie, override_room_id=args.room_id)
    Path(args.output).write_text(json.dumps(room, indent=2))
    print(f"Wrote {args.output}. Add `warehouse_id` before running load-room.")
    return 0


# ── Genie → RoomConfig translation (testable, pure) ────────────────────────


def _genie_to_room_config(
    genie: dict[str, Any], *, override_room_id: str | None = None
) -> dict[str, Any]:
    """Map Genie wire format to RoomConfig JSON. See docs/roadmap.md R5
    for the full field-by-field translation table."""
    instructions = genie.get("instructions") or {}
    config_block = genie.get("config") or {}
    data_sources = genie.get("data_sources") or []

    text_instructions = instructions.get("text_instructions") or []
    text_instruction = (
        text_instructions[0].get("content", "")
        if text_instructions and isinstance(text_instructions[0], dict)
        else ""
    )

    examples = []
    for ex in instructions.get("example_question_sqls") or []:
        if isinstance(ex, dict):
            examples.append(
                {
                    "id": ex.get("id", ""),
                    "question": ex.get("question", ""),
                    "sql": ex.get("sql", ""),
                }
            )

    joins = []
    for j in instructions.get("join_specs") or []:
        if not isinstance(j, dict):
            continue
        left = j.get("left") or {}
        right = j.get("right") or {}
        relationship_raw = j.get("relationship_type", "")
        relationship = relationship_raw.replace("FROM_RELATIONSHIP_TYPE_", "")
        joins.append(
            {
                "left_alias": left.get("alias", ""),
                "left_table": left.get("identifier", ""),
                "right_alias": right.get("alias", ""),
                "right_table": right.get("identifier", ""),
                "join_on": j.get("on", ""),
                "relationship_type": relationship,
                "instruction": j.get("instruction", ""),
            }
        )

    sql_filters, sql_expressions = [], []
    snippets_block = instructions.get("sql_snippets") or {}
    for s in snippets_block.get("filters") or []:
        if isinstance(s, dict):
            sql_filters.append(_snippet(s, kind="filter"))
    for s in snippets_block.get("expressions") or []:
        if isinstance(s, dict):
            sql_expressions.append(_snippet(s, kind="expression"))

    tables = []
    for ds in data_sources:
        if not isinstance(ds, dict):
            continue
        ref = ds.get("table_ref")
        if isinstance(ref, str) and ref:
            tables.append(ref)

    room_id = (
        override_room_id
        or genie.get("space_id")
        or genie.get("id")
        or "imported-room"
    )

    return {
        "room_id": str(room_id),
        "title": str(genie.get("title") or "Imported room"),
        "tables": tables,
        "warehouse_id": "",  # user MUST fill in before load-room
        "text_instruction": text_instruction,
        "examples": examples,
        "joins": joins,
        "sql_filters": sql_filters,
        "sql_expressions": sql_expressions,
        "sample_questions": list(config_block.get("sample_questions") or []),
    }


def _snippet(s: dict, *, kind: str) -> dict:
    return {
        "id": s.get("id", ""),
        "display_name": s.get("display_name", ""),
        "kind": kind,
        "sql": s.get("sql", ""),
        "instruction": s.get("instruction", ""),
        "synonyms": list(s.get("synonyms") or []),
    }


def _fetch_genie_space(space_id: str, *, cfg: Config) -> dict[str, Any]:
    """Fetch a Genie Space export via the Databricks Workspace API.

    Lazy import of httpx because the rest of the CLI shouldn't pay an
    import cost for the network path when the user passes --input instead.

    Credentials come from Config rather than direct environment reads,
    because only config.py is permitted to touch the environment or the
    TOML loader (enforced by a static scan in test_config.py).
    """
    import httpx

    host = (cfg.databricks_host or "").rstrip("/")
    token = cfg.databricks_token or ""
    if not host or not token:
        raise ConfigurationError(
            "import-genie --space-id requires DATABRICKS_HOST and "
            "DATABRICKS_TOKEN in the environment (or tiri.toml). "
            "Alternatively, dump the Genie Space JSON to a file and pass "
            "--input <path>."
        )
    url = (
        f"{host}/api/2.0/genie/spaces/{space_id}"
        "?include_serialized_space=true"
    )
    response = httpx.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    )
    response.raise_for_status()
    return response.json()


# ── Engine / manager wiring (mirrors api/routes) ───────────────────────────


def _engine(cfg: Config, container: dict[str, Any]) -> RoomEngine:
    return RoomEngine(
        llm=container["llm"],
        catalog=container["catalog"],
        metadata_providers=container["metadata_providers"],
        query=container["query"],
        vector=container["vector"],
        store=container["store"],
        mcp_providers=container.get("mcp_providers", {}),
        history_window=cfg.history_window,
        intent_threshold=cfg.intent_threshold,
        sql_max_retries=cfg.sql_max_retries,
        query_row_limit=cfg.query_row_limit,
    )


def _manager(container: dict[str, Any]) -> RoomManager:
    return RoomManager(
        store=container["store"],
        vector=container["vector"],
        llm=container["llm"],
    )


# ── argparse plumbing ──────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tiri", description="Tiri CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load-room", help="Create or update a room")
    p_load.add_argument("config", help="Path to room config JSON")

    p_ask = sub.add_parser("ask", help="Ask a question against a room")
    p_ask.add_argument("--room", required=True, help="Room ID")
    p_ask.add_argument("question", help="The natural-language question")

    p_bench = sub.add_parser("benchmark", help="Run room benchmarks")
    p_bench.add_argument("--room", required=True)

    p_dump = sub.add_parser("dump", help="Print room config as JSON")
    p_dump.add_argument("--room", required=True)

    p_serve = sub.add_parser("serve", help="Run the API server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    p_imp = sub.add_parser(
        "import-genie",
        help="Translate a Genie Space export to a Tiri RoomConfig JSON",
    )
    src = p_imp.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", help="Local Genie wire-format JSON file")
    src.add_argument("--space-id", help="Genie Space ID (fetches via API)")
    p_imp.add_argument("--output", required=True, help="Output config path")
    p_imp.add_argument(
        "--room-id",
        help="Override room_id (default: space_id from Genie payload)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "load-room":
            return asyncio.run(_cmd_load_room(args))
        if args.command == "ask":
            return asyncio.run(_cmd_ask(args))
        if args.command == "benchmark":
            return asyncio.run(_cmd_benchmark(args))
        if args.command == "dump":
            return asyncio.run(_cmd_dump(args))
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "import-genie":
            return _cmd_import_genie(args)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
