import pytest

import app.main as main
from app.tools.registry import ToolRunner


@pytest.fixture(autouse=True)
def _tools_without_database(monkeypatch):
    """The app starts the agents' tools with the real PG_DB_URL from dev.env; tests never touch that database."""

    async def start(agent_config, retriever):
        return ToolRunner(agent_config.tools, "", retriever)

    monkeypatch.setattr(main, "_start_tools", start)
