"""Simulated cross-venue paper execution engine (no live orders or credentials)."""

from backend.execution.paper_simulator import (
    PaperExecutionEngine,
    PaperExecutionRequest,
    simulate_ask_fill_up_to_limit,
    simulate_bid_unwind,
)

__all__ = [
    "PaperExecutionEngine",
    "PaperExecutionRequest",
    "simulate_ask_fill_up_to_limit",
    "simulate_bid_unwind",
]
