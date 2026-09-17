"""The workspace-membership contract names are public exports of
``sbxloop.api.collaboration``.

The type checker runs strict, so a name that the module only imports is not
exported: ``from sbxloop.api.collaboration import Role`` fails type checking
unless the module defines the name, re-imports it as ``Role as Role``, or
lists it in ``__all__``. This test applies the same rule to the module's
source so consumers of the contract keep type checking.
"""

from __future__ import annotations

import ast
import inspect

import pytest

import sbxloop.api.collaboration as collaboration

CONTRACT_NAMES = ("Member", "Role", "ROLES", "ROLE_CAPABILITIES")


def _explicit_exports() -> set[str]:
    tree = ast.parse(inspect.getsource(collaboration))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                    if target.id == "__all__" and isinstance(node.value, ast.List | ast.Tuple):
                        names.update(
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names if alias.asname == alias.name)
    return names


@pytest.mark.parametrize("name", CONTRACT_NAMES)
def test_contract_name_is_explicitly_exported(name: str) -> None:
    assert hasattr(collaboration, name)
    assert name in _explicit_exports()
