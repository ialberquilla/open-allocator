"""Deterministic allocation strategy library.

A *strategy* maps scored vaults + params to a **desired weight vector** (before
caps). The allocator then runs that vector through the same caps waterfall and
USD-rounding used by every strategy, so cap semantics are identical everywhere
and ``check-policy`` runs on the materialized result regardless of how weights
were produced.

Strategies are vetted Python selected/parameterized by the agent — never
agent-authored executable code on the execution path.

Ported from ``yield-analysis/p2_05_allocation_optimizer.py`` and studies:
- ``score_weighted``  — default; today's behavior.
- ``equal_weight``    — 1/N over the top-N selected.
- ``risk_parity`` / ``inverse_vol`` — weight proportional to 1/volatility (037).
- ``core_satellite``  — core sleeve at ``core_weight``, satellite at the rest (034).
- ``sleeves`` / ``ladder`` — score-tiered buckets with target weights (040, 043).
"""

from __future__ import annotations

from open_allocator.core.strategies.library import (
    STRATEGIES,
    StrategyContext,
    StrategyError,
    available,
    desired_weights,
)

__all__ = [
    "STRATEGIES",
    "StrategyContext",
    "StrategyError",
    "available",
    "desired_weights",
]
