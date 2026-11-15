"""Guard against blocking I/O inside ``async def`` bodies.

A blocking ``requests`` call in a coroutine stalls the whole event loop: the
music glitches and the web panel stops answering for as long as the remote
server takes (Deezer used to do exactly that with a ten second timeout).
These tests walk the source instead of running it, so they are instant and do
not need the network.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "playify"

BLOCKING_MODULES = {"requests", "urllib3"}
BLOCKING_CALLS = {("time", "sleep")}


def _iter_async_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def _call_name(node: ast.Call) -> tuple[str, str] | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        target = func.value
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            return target.id, func.attr
    return None


def _blocking_calls_in(tree: ast.AST) -> list[str]:
    """Return the blocking calls found in ``tree`` outside of any lambda."""
    found: list[str] = []

    def walk(node: ast.AST, in_lambda: bool = False) -> None:
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name is not None:
                module, attribute = name
                if not in_lambda and (
                    module in BLOCKING_MODULES or (module, attribute) in BLOCKING_CALLS
                ):
                    found.append(f"{module}.{attribute}()")
        for child in ast.iter_child_nodes(node):
            walk(child, in_lambda or isinstance(child, ast.Lambda))

    walk(tree)
    return found


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_async_functions_do_not_block_the_event_loop(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for function in _iter_async_functions(tree):
        for call in _blocking_calls_in(function):
            problems.append(f"{path.relative_to(SRC.parent.parent)}:{function.lineno} {function.name}() -> {call}")
    assert not problems, "Blocking calls inside a coroutine:\n" + "\n".join(problems)


def test_the_guard_catches_a_known_bad_body():
    tree = ast.parse(
        "async def f():\n"
        "    return requests.get('http://example.com', timeout=1)\n"
    )
    assert _blocking_calls_in(tree) == ["requests.get()"]


def test_a_call_handed_to_to_thread_is_allowed():
    tree = ast.parse(
        "async def f():\n"
        "    return await asyncio.to_thread(lambda: requests.get('http://x', timeout=1))\n"
    )
    assert _blocking_calls_in(tree) == []
