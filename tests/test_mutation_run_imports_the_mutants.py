from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.skipif(
    "MUTANT_UNDER_TEST" not in os.environ,
    reason="only meaningful inside a mutmut run",
)
def test_mutmut_run_imports_the_mutated_copy() -> None:
    import agentbus_client

    location = Path(agentbus_client.__file__).resolve()
    assert "mutants" in location.parts, f"tests imported {location}, not the mutated copy"
