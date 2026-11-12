"""Server-only model settings. Never include keys in repr, responses or logs."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values


OLLAMA_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
GROQ_MODEL = "openai/gpt-oss-20b"
ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


class ModelConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSettings:
    provider: Literal["ollama", "groq"] = "ollama"
    api_key: str | None = field(default=None, repr=False)

    @property
    def model(self) -> str:
        return GROQ_MODEL if self.provider == "groq" else OLLAMA_MODEL


def load_model_settings(*, env_file: Path | None = None) -> ModelSettings:
    path = ENV_FILE if env_file is None else env_file
    try:
        values = dotenv_values(path, interpolate=False, encoding="utf-8-sig") if path.is_file() else {}
    except OSError:
        raise ModelConfigurationError("The model configuration file could not be read.") from None

    def setting(name):
        return os.environ[name] if name in os.environ else values.get(name)

    provider = setting("APEX_LLM_PROVIDER") or "ollama"
    if provider not in {"ollama", "groq"}:
        raise ModelConfigurationError("APEX_LLM_PROVIDER must be ollama or groq.")
    if provider == "ollama":
        return ModelSettings()
    key = setting("GROQ_API_KEY")
    if not key or not key.isascii() or any(character.isspace() for character in key):
        raise ModelConfigurationError("GROQ_API_KEY is missing or invalid. Set it in the server environment or .env.local.")
    return ModelSettings(provider="groq", api_key=key)
