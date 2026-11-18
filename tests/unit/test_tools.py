"""The bench scripts in tools/ need a live switcher, so the suite never runs
them. They are exercised nowhere else, so it keeps them parseable, and checks that each that opens
an ATEM session installs ``_exit_signals`` (a signal unwinds into the
goodbye instead of abandoning the session on the switcher)."""
import ast
from pathlib import Path

import pytest

TOOLS = sorted((Path(__file__).resolve().parents[2] / 'tools').glob('*.py'))
BENCH = [p for p in TOOLS if 'execute_upload(' in p.read_text() or 'mps.acquire(' in p.read_text()]


@pytest.mark.parametrize('path', TOOLS, ids=[p.name for p in TOOLS])
def test_bench_script_parses(path):
    ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _main_block_installs(tree):
    for node in tree.body:
        if isinstance(node, ast.If) and '__main__' in ast.unparse(node.test):
            return '_exit_signals.install()' in ast.unparse(node)
    return False


@pytest.mark.parametrize('path', BENCH, ids=[p.name for p in BENCH])
def test_a_script_that_opens_a_session_installs_the_signal_handler(path):
    assert _main_block_installs(ast.parse(path.read_text(encoding='utf-8')))
