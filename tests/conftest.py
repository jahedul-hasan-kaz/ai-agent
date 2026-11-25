import pytest

import app.main as main
from app.tools.registry import ToolRunner


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """The app starts the agents' tools with the real PG_DB_URL from dev.env; tests never touch that database."""

    async def start_tools(agent_config):
        return ToolRunner(agent_config.tools, "")

    monkeypatch.setattr(main, "_start_tools", start_tools)
