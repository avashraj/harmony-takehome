"""Scheduler package: CP-SAT solver and KPI computation."""

from .kpis import compute_kpis
from .solver import solve

__all__ = ["solve", "compute_kpis"]
