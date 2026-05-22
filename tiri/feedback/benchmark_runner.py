"""BenchmarkRunner — runs every benchmark through the full pipeline.

For each `Benchmark`, calls `engine.chat()` with a synthetic conversation
id (`benchmark-{id}`), compares the generated SQL to the expected SQL via
normalized exact match, and (if `expected_row_count` is set) runs both
SQLs and compares row counts.

`Proposer` filters out conversations whose id starts with `benchmark-` so
these synthetic runs do not pollute example proposals.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING

from tiri.data_models import (
    Benchmark,
    BenchmarkReport,
    BenchmarkResult,
    RoomConfig,
)
from tiri.feedback.sql_normalize import normalize_sql
from tiri.providers.base import QueryProvider


if TYPE_CHECKING:
    from tiri.engine.room_engine import RoomEngine


_log = logging.getLogger("tiri.feedback.benchmark_runner")
_BENCHMARK_CONV_PREFIX = "benchmark-"


class BenchmarkRunner:
    def __init__(
        self,
        engine: "RoomEngine",
        store_query: QueryProvider | None = None,
    ) -> None:
        """Construct with the RoomEngine that will run each benchmark.

        `store_query` is an optional `QueryProvider` used for the row-count
        comparison when `Benchmark.expected_row_count` is set. If omitted,
        the row-count comparison is skipped and `BenchmarkResult.result_match`
        stays None for those benchmarks.
        """
        self._engine = engine
        self._store_query = store_query

    async def run(self, room_id: str) -> BenchmarkReport:
        config = await self._load_config(room_id)
        benchmarks: list[Benchmark] = list(config.benchmarks)

        results: list[BenchmarkResult] = []
        for bench in benchmarks:
            results.append(await self._run_one(room_id, bench))

        passed = sum(1 for r in results if r.passed)
        total = len(results)
        score = (passed / total) if total else 0.0
        return BenchmarkReport(
            room_id=room_id,
            run_at=_now_iso(),
            total=total,
            passed=passed,
            failed=total - passed,
            score=score,
            results=results,
        )

    async def _run_one(
        self, room_id: str, bench: Benchmark
    ) -> BenchmarkResult:
        conv_id = f"{_BENCHMARK_CONV_PREFIX}{bench.id}"
        try:
            turn = await self._engine.chat(
                room_id=room_id,
                conversation_id=conv_id,
                question=bench.question,
            )
        except Exception as e:
            _log.exception(
                "BenchmarkRunner.chat failed for benchmark %s", bench.id
            )
            return BenchmarkResult(
                benchmark_id=bench.id,
                question=bench.question,
                expected_sql=bench.expected_sql,
                generated_sql=None,
                sql_match=False,
                result_match=None,
                passed=False,
                error=str(e),
            )

        if turn.error is not None or not turn.sql:
            return BenchmarkResult(
                benchmark_id=bench.id,
                question=bench.question,
                expected_sql=bench.expected_sql,
                generated_sql=turn.sql,
                sql_match=False,
                result_match=None,
                passed=False,
                error=turn.error or "no SQL produced",
            )

        sql_match = normalize_sql(turn.sql) == normalize_sql(bench.expected_sql)
        result_match: bool | None = None
        if bench.expected_row_count is not None and self._store_query is not None:
            result_match = await self._compare_row_counts(
                generated=turn.sql,
                expected=bench.expected_sql,
            )

        passed = sql_match or bool(result_match)

        return BenchmarkResult(
            benchmark_id=bench.id,
            question=bench.question,
            expected_sql=bench.expected_sql,
            generated_sql=turn.sql,
            sql_match=sql_match,
            result_match=result_match,
            passed=passed,
            error=None,
        )

    async def _compare_row_counts(
        self, generated: str, expected: str
    ) -> bool:
        try:
            gen_result = await self._store_query.execute(generated)
            exp_result = await self._store_query.execute(expected)
        except Exception as e:
            _log.warning("Row-count comparison failed: %s", e)
            return False
        return gen_result.row_count == exp_result.row_count

    async def _load_config(self, room_id: str) -> RoomConfig:
        # Reuse RoomEngine's loader via a private bridge — keeps the single
        # source of truth for store-key conventions.
        return await self._engine._load_room_config(room_id)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
