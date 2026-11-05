import os
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv() # Ensure .env is loaded for local development
# Machine-local overrides: .env is Ansible-templated and gets rewritten on
# every role re-run, so any secret that lives only there is one provisioning
# away from being lost. .env.local is never touched by Ansible and loads
# last (override=True), matching the systemd units' EnvironmentFile order.
load_dotenv(".env.local", override=True)
from typing import Literal, Dict

LLMRole = Literal["default", "summarizer"]


class LLMConfig(BaseModel):
    model: str = "gemini/gemini-2.5-flash"
    temperature: float = 0.3
    api_key: str


class EmbedderConfig(BaseModel):
    model: str = "text-embedding-3-small"
    provider: str = Literal["local", "remote"]


class AppConfig(BaseModel):
    llms: Dict[LLMRole, LLMConfig]
    embeddings: EmbedderConfig

    @property
    def llm(self) -> LLMConfig:
        """Main chat / agent LLM"""
        return self.llms["default"]

    @property
    def summarizer_llm(self) -> LLMConfig:
        """Summarization-specific LLM"""
        return self.llms["summarizer"]


def load_config() -> AppConfig:
    """
    Loads application configuration from environment variables.

    Returns:
        AppConfig: Parsed application configuration.
    """
    return AppConfig(
        llms={
            "default": LLMConfig(
                model=os.getenv("LLM_MODEL", ""),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
                api_key=os.getenv("OPENAI_API_KEY", ""),
            ),
            "summarizer": LLMConfig(
                model=os.getenv("LLM_MODEL_SUMMARIZE", ""),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
                api_key=os.getenv("OPENAI_API_KEY", ""),
            ),
        },
        embeddings=EmbedderConfig(
            model=os.getenv("EMBEDDING_MODEL", ""),
            provider=os.getenv("EMBEDDING_PROVIDER", "local"),
        ),
    )
