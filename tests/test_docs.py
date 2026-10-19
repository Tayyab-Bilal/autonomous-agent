"""Every ```python block in README.md and docs/*.md is executed, so the docs cannot rot.

Blocks in one file share a namespace and run top to bottom, like cells in a notebook. They may use
top-level `await`."""
import ast
import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
BLOCK = re.compile(r"^```python\n(.*?)^```", re.S | re.M)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
async def test_python_blocks_in_the_docs_run(path):
    blocks = BLOCK.findall(path.read_text())
    assert blocks, f"{path.name} has no python examples"
    namespace: dict = {"__name__": "__docs__"}
    for n, source in enumerate(blocks, start=1):
        code = compile(source, f"{path.name} block {n}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        result = eval(code, namespace)
        if inspect.iscoroutine(result):
            await result
