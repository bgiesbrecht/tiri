"""CLI tests — focused on the Genie translation (the pure, testable piece).

The other CLI commands (load-room, ask, benchmark, dump, serve) are thin
wrappers over RoomManager / RoomEngine / BenchmarkRunner — those are
already covered by their respective test suites. Re-testing them through
the CLI would duplicate coverage.

Genie → RoomConfig translation (R5 / roadmap.md) is the only piece with
non-trivial logic that's purely a CLI concern. It's also the piece a
customer-facing migration depends on, so it gets dedicated tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiri.cli import _build_parser, _genie_to_room_config, _snippet
from tiri.data_models import RoomConfig


def _full_genie_payload() -> dict:
    """A reasonably complete Genie wire-format payload covering every
    field the translator needs to handle. Field shapes match the
    Databricks Genie API spec — DO NOT simplify unless you've checked."""
    return {
        "id": "space_abc",
        "title": "Sales Genie",
        "instructions": {
            "text_instructions": [
                {"content": "Tables are in the sales catalog. Use surrogate keys for joins."}
            ],
            "example_question_sqls": [
                {"id": "ex1", "question": "Total revenue?", "sql": "SELECT SUM(revenue) FROM s"},
                {"id": "ex2", "question": "Top customer?", "sql": "SELECT name FROM c ORDER BY r DESC LIMIT 1"},
            ],
            "join_specs": [
                {
                    "left": {"alias": "o", "identifier": "orders"},
                    "right": {"alias": "c", "identifier": "customers"},
                    "on": "o.customer_id = c.id",
                    "relationship_type": "FROM_RELATIONSHIP_TYPE_MANY_TO_ONE",
                    "instruction": "Customer lookup",
                }
            ],
            "sql_snippets": {
                "filters": [
                    {
                        "id": "f1",
                        "display_name": "active customers only",
                        "sql": "c.active = true",
                        "instruction": "Apply by default.",
                        "synonyms": ["live customers", "current"],
                    }
                ],
                "expressions": [
                    {
                        "id": "e1",
                        "display_name": "net revenue",
                        "sql": "SUM(price * (1 - discount))",
                        "instruction": "Standard revenue calc.",
                        "synonyms": [],
                    }
                ],
            },
        },
        "config": {
            "sample_questions": [
                "How much revenue did we have last quarter?",
                "Which region grew the most?",
            ]
        },
        "data_sources": [
            {"table_ref": "main.sales.orders"},
            {"table_ref": "main.sales.customers"},
        ],
    }


def test_translation_round_trips_through_roomconfig_from_dict() -> None:
    """The translator's output MUST be loadable by RoomConfig.from_dict.
    This catches schema-level drift between the translator and the
    dataclass without needing to enumerate every field."""
    out = _genie_to_room_config(_full_genie_payload())
    out["warehouse_id"] = "wh-1"  # user fills this in before load-room
    # If this raises, the translator emitted a shape RoomConfig can't accept.
    config = RoomConfig.from_dict(out)
    assert config.room_id == "space_abc"
    assert config.title == "Sales Genie"


def test_translation_unwraps_text_instructions_list_to_string() -> None:
    """Genie wraps text_instruction in a list; RoomConfig wants a string."""
    out = _genie_to_room_config(_full_genie_payload())
    assert out["text_instruction"].startswith("Tables are in the sales catalog")


def test_translation_extracts_table_refs_from_data_sources() -> None:
    out = _genie_to_room_config(_full_genie_payload())
    assert out["tables"] == ["main.sales.orders", "main.sales.customers"]


def test_translation_strips_relationship_type_prefix() -> None:
    """Genie's relationship_type field is verbose ('FROM_RELATIONSHIP_TYPE_MANY_TO_ONE');
    RoomConfig stores the suffix only ('MANY_TO_ONE')."""
    out = _genie_to_room_config(_full_genie_payload())
    assert len(out["joins"]) == 1
    assert out["joins"][0]["relationship_type"] == "MANY_TO_ONE"
    assert out["joins"][0]["left_table"] == "orders"
    assert out["joins"][0]["right_alias"] == "c"


def test_translation_adds_kind_to_snippets() -> None:
    out = _genie_to_room_config(_full_genie_payload())
    assert all(s["kind"] == "filter" for s in out["sql_filters"])
    assert all(s["kind"] == "expression" for s in out["sql_expressions"])
    assert out["sql_filters"][0]["synonyms"] == ["live customers", "current"]


def test_translation_preserves_examples_and_sample_questions() -> None:
    out = _genie_to_room_config(_full_genie_payload())
    assert len(out["examples"]) == 2
    assert out["examples"][0]["id"] == "ex1"
    assert out["sample_questions"][0].startswith("How much revenue")


def test_translation_room_id_override() -> None:
    out = _genie_to_room_config(_full_genie_payload(), override_room_id="custom-id")
    assert out["room_id"] == "custom-id"


def test_translation_falls_back_to_imported_room_when_no_id() -> None:
    """If the Genie payload has neither space_id nor id, the translator
    uses a placeholder room_id rather than emitting an empty string
    (which would fail RoomConfig validation)."""
    payload = {"title": "X", "instructions": {}, "config": {}, "data_sources": []}
    out = _genie_to_room_config(payload)
    assert out["room_id"] == "imported-room"


def test_translation_handles_missing_optional_blocks() -> None:
    """Real Genie payloads vary — older spaces may lack join_specs,
    sql_snippets, etc. Translation MUST NOT crash on missing keys."""
    minimal = {"id": "x", "title": "x"}
    out = _genie_to_room_config(minimal)
    assert out["examples"] == []
    assert out["joins"] == []
    assert out["sql_filters"] == []
    assert out["sql_expressions"] == []
    assert out["tables"] == []


def test_translation_warehouse_id_is_empty_for_user_to_fill() -> None:
    """The user MUST add warehouse_id before running load-room. Documented
    in the CLI help text and in roadmap.md R5. Test that the translator
    emits an empty string (not a placeholder that might be mistaken for
    a real warehouse)."""
    out = _genie_to_room_config(_full_genie_payload())
    assert out["warehouse_id"] == ""


# ── argparse plumbing ─────────────────────────────────────────────────────


def test_parser_accepts_all_six_subcommands() -> None:
    """Smoke test: each command surface is reachable via the parser."""
    parser = _build_parser()
    parser.parse_args(["load-room", "demo/x.json"])
    parser.parse_args(["ask", "--room", "r1", "what?"])
    parser.parse_args(["benchmark", "--room", "r1"])
    parser.parse_args(["dump", "--room", "r1"])
    parser.parse_args(["serve", "--port", "9000"])
    parser.parse_args(["import-genie", "--input", "x.json", "--output", "y.json"])
    parser.parse_args(["import-genie", "--space-id", "abc", "--output", "y.json"])


def test_parser_rejects_import_genie_without_source() -> None:
    """import-genie requires exactly one of --input or --space-id."""
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["import-genie", "--output", "y.json"])


def test_parser_rejects_import_genie_with_both_sources() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["import-genie", "--input", "x.json", "--space-id", "abc", "--output", "y.json"]
        )


def test_import_genie_writes_translated_file(tmp_path: Path) -> None:
    """End-to-end test: dump a Genie payload to disk, run import-genie
    with --input, verify the output file is valid RoomConfig JSON."""
    from tiri.cli import _cmd_import_genie

    genie_path = tmp_path / "genie.json"
    out_path = tmp_path / "room.json"
    genie_path.write_text(json.dumps(_full_genie_payload()))

    class _Args:
        input = str(genie_path)
        space_id = None
        output = str(out_path)
        room_id = None

    rc = _cmd_import_genie(_Args())
    assert rc == 0
    out_data = json.loads(out_path.read_text())
    out_data["warehouse_id"] = "wh-1"
    config = RoomConfig.from_dict(out_data)
    assert config.title == "Sales Genie"
