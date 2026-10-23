from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_path: str = "data/remedi.db"
    operator_token: SecretStr
    owner_token: SecretStr
    provider: Literal["demo", "ollama", "api", "copilot"] = "copilot"
    copilot_model: str = "gpt-4.1"
    copilot_home: str = ".runtime/copilot"
    copilot_work_directory: str = ".runtime/work"
    copilot_github_token: SecretStr = SecretStr("")
    copilot_allow_egress: bool = False
    frontend_dist: str = "frontend/dist"
    ollama_url: str = "http://ollama:11434"
    local_model: str = "qwen2.5:7b"
    api_base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = SecretStr("")
    api_model: str = "gpt-4.1-mini"
    allow_api_egress: bool = False
    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    local_gpu_usd_per_hour: float = 0.0
    llm_timeout_seconds: float = 90.0
    max_agent_steps: int = 4
    max_active_incidents: int = 25
    requests_per_minute: int = 120
    confidence_threshold: float = 0.85

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        tokens = [self.operator_token.get_secret_value(), self.owner_token.get_secret_value()]
        if any(len(token) < 32 for token in tokens) or tokens[0] == tokens[1]:
            raise ValueError(
                "Distinct operator/owner tokens of at least 32 characters are required"
            )
        if self.provider == "api":
            if not self.allow_api_egress or not self.api_key.get_secret_value():
                raise ValueError("API mode requires explicit ALLOW_API_EGRESS and API_KEY")
        if self.provider == "copilot" and not self.copilot_allow_egress:
            raise ValueError("Copilot mode requires explicit COPILOT_ALLOW_EGRESS=true")
        if not self.copilot_model.strip():
            raise ValueError("COPILOT_MODEL must not be empty")
        parsed = urlparse(self.api_base_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("API_BASE_URL must be a credential-free HTTPS endpoint")
        if not 2 <= self.max_agent_steps <= 8:
            raise ValueError("MAX_AGENT_STEPS must be between 2 and 8")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("CONFIDENCE_THRESHOLD must be between 0 and 1")
        if (
            min(
                self.input_usd_per_million, self.output_usd_per_million, self.local_gpu_usd_per_hour
            )
            < 0
        ):
            raise ValueError("Cost rates cannot be negative")
        if min(self.llm_timeout_seconds, self.max_active_incidents, self.requests_per_minute) <= 0:
            raise ValueError("Timeout and capacity limits must be positive")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
