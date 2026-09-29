"""Architecture test: imports must point inward (api -> services -> domain <- infra)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

# For each layer, the app modules it must never import.
FORBIDDEN = {
    "domain": ("app.services", "app.api", "app.infra", "app.observability", "app.bootstrap"),
    "services": ("app.api", "app.infra", "app.bootstrap"),
    "api": ("app.infra", "app.bootstrap"),
    "infra": ("app.services", "app.api", "app.bootstrap"),
}


def imported_modules(path: Path) -> set[str]:
    """Return every module name imported by a Python file."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_does_not_import_outer_layers(layer: str) -> None:
    # Scan every file in the layer; any forbidden import fails the test with its location.
    violations = [
        f"{path.relative_to(APP)} imports {name}"
        for path in (APP / layer).rglob("*.py")
        for name in imported_modules(path)
        if name.startswith(FORBIDDEN[layer])
    ]
    assert violations == []
