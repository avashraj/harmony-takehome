from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.models import ChangeoverMatrix, ObjectiveMode, TimeWindow


def test_time_window_rejects_start_after_end() -> None:
    with pytest.raises(ValidationError):
        TimeWindow(
            start=datetime.fromisoformat("2025-11-03T12:00:00"),
            end=datetime.fromisoformat("2025-11-03T08:00:00"),
        )


def test_time_window_duration_minutes() -> None:
    window = TimeWindow(
        start=datetime.fromisoformat("2025-11-03T08:00:00"),
        end=datetime.fromisoformat("2025-11-03T12:30:00"),
    )
    assert window.duration_minutes == 270


def test_changeover_matrix_returns_configured_and_default_values() -> None:
    matrix = ChangeoverMatrix(
        entries={
            "standard": {"standard": 0, "premium": 20},
            "premium": {"standard": 20, "premium": 0},
        }
    )
    assert matrix.get_minutes("standard", "premium") == 20
    assert matrix.get_minutes("unknown", "premium") == 0
    assert matrix.get_minutes("standard", "unknown") == 0


def test_objective_mode_parses_enum_value() -> None:
    parsed = ObjectiveMode("min_tardiness")
    assert parsed is ObjectiveMode.MIN_TARDINESS
