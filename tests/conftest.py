import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import Config  # noqa: E402

SUMMARY = """## Current focus
mapping the filesystem

## What I've done
- looked around

## What I know
- /workspace is empty

## Open threads
- thread A: check network

## Notes to self
- keep notes short"""


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.paths.workspace = str(tmp_path / "ws")
    c.paths.data_dir = str(tmp_path / "data")
    (tmp_path / "ws").mkdir()
    (tmp_path / "data").mkdir()
    c.loop.tick_interval_s = 0
    c.search.provider = "duckduckgo"
    c.dread.enabled = False  # tests that exercise dread turn it on
    return c
