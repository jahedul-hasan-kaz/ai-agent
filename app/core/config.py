"""
Service configuration. Same split as ai-agent-management:

- dev   `AI_AGENT_CONFIG` holds the JSON itself (dev.env, or the environment).
- prod  `AI_AGENT_CONFIG_NAME` names the GCP Secret Manager secret holding the
        JSON (default `ai-agent-config`), read with `PROJECT_NUMBER`.

The JSON schema lives in app/llm/settings.py.

The `docs` knowledge base (RAG) is read from the ai-agent-management Postgres,
named the same way: `PG_DB_URL` in dev, the `PG_DB_URL_NAME` secret (default
`pg-db-url`) in prod. That is the transaction pooler (:6543) the backend's API
uses; the session-mode PG_DB_URL_DIRECT is kept for LISTEN/NOTIFY and not needed here.
"""
import json
import os
from enum import Enum

from dotenv import load_dotenv
from loguru import logger
from pydantic import ValidationError

from app.llm.settings import AgentConfig


class EnvEnum(str, Enum):
    prod = "prod"
    dev = "dev"


class ConfigError(RuntimeError):
    """ai-agent-config is missing or invalid. Raised at startup so a bad deploy fails fast."""


class Config:
    def __init__(self, env: EnvEnum) -> None:
        self.ENV = env
        self.PROJECT_NAME = os.getenv("PROJECT_NAME", "inride-ai-agent")
        self.API_V1_STR = "/v1"
        self.PROJECT_NUMBER = os.getenv("PROJECT_NUMBER", "")
        self.AI_AGENT_CONFIG_NAME = os.getenv("AI_AGENT_CONFIG_NAME", "ai-agent-config")
        self.PG_DB_URL_NAME = os.getenv("PG_DB_URL_NAME", "pg-db-url")

    def load_pg_db_url(self) -> str:
        """Postgres URL holding the `docs` table, or "" when none is configured."""
        if self.ENV == EnvEnum.prod:
            from app.core.secret_manager import access_secret_version

            url = access_secret_version(self.PG_DB_URL_NAME, self.PROJECT_NUMBER)
        else:
            url = os.getenv("PG_DB_URL", "")
        # The backend stores SQLAlchemy-style URLs; asyncpg wants the plain scheme.
        return url.strip().replace("postgresql+psycopg2://", "postgresql://", 1).replace("postgresql+asyncpg://", "postgresql://", 1)

    def load_agent_config(self) -> AgentConfig:
        if self.ENV == EnvEnum.prod:
            from app.core.secret_manager import access_secret_version

            source = f"secret '{self.AI_AGENT_CONFIG_NAME}'"
            raw = access_secret_version(self.AI_AGENT_CONFIG_NAME, self.PROJECT_NUMBER)
        else:
            source = "AI_AGENT_CONFIG"
            raw = os.getenv("AI_AGENT_CONFIG", "")
            if not raw.strip():
                raise ConfigError(
                    "AI_AGENT_CONFIG is empty. Put the JSON in dev.env (see ai-agent-config.example.json)."
                )

        try:
            return AgentConfig.model_validate(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{source} is not valid JSON: {exc}") from exc
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
            )
            raise ConfigError(f"{source} is invalid — {problems}") from exc


def get_config() -> Config:
    try:
        env = EnvEnum(os.getenv("ENV") or "dev")
    except ValueError:
        env = EnvEnum.dev

    if env == EnvEnum.dev:
        load_dotenv("dev.env")
        load_dotenv(".env", override=False)
    else:
        load_dotenv(".env")
    logger.info("Initializing {} config...", env.value)
    return Config(env)


config = get_config()
