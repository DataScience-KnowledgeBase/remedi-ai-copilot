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
    kubernetes_enabled: bool = False
    kubernetes_namespace: str = "remedi-demo"
    kubernetes_api_url: str = "https://kubernetes.docker.internal:6443"
    kubernetes_tls_server_name: str = ""
    kubernetes_ca_file: str = "/run/kubernetes/ca.crt"
    kubernetes_token_file: str = "/run/kubernetes/token"
    kubernetes_rollout_timeout_seconds: int = 120
    kubernetes_probe_interval_seconds: float = 2.0
    github_gitops_enabled: bool = False
    github_repository: str = "DataScience-KnowledgeBase/remedi-ai-copilot"
    github_app_id: int = 0
    github_installation_id: int = 0
    github_private_key_file: str = "/run/github/app.pem"

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.github_gitops_enabled:
            if not self.kubernetes_enabled:
                raise ValueError("GitHub manifest deployment requires Kubernetes integration")
            if min(self.github_app_id, self.github_installation_id) <= 0:
                raise ValueError("GitHub App and installation IDs must be configured")
            if self.github_repository != "DataScience-KnowledgeBase/remedi-ai-copilot":
                raise ValueError("GitHub automation is restricted to the approved repository")
        if self.kubernetes_enabled:
            import re

            if self.provider == "demo":
                raise ValueError("Real Kubernetes integration requires a real inference provider")
            if not re.fullmatch(
                r"remedi-[a-z0-9](?:[-a-z0-9]{0,48}[a-z0-9])?",
                self.kubernetes_namespace,
            ):
                raise ValueError("Kubernetes namespace must be a dedicated remedi-* namespace")
            endpoint = urlparse(self.kubernetes_api_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            ):
                raise ValueError("Kubernetes API must be a credential-free HTTPS origin")
            if self.kubernetes_tls_server_name and not re.fullmatch(
                r"[a-zA-Z0-9.:-]+",
                self.kubernetes_tls_server_name,
            ):
                raise ValueError("Kubernetes TLS server name must be a hostname or IP")
            if not 10 <= self.kubernetes_rollout_timeout_seconds <= 600:
                raise ValueError("Kubernetes rollout timeout must be between 10 and 600 seconds")
            if not 0.1 <= self.kubernetes_probe_interval_seconds <= 10:
                raise ValueError("Kubernetes probe interval must be between 0.1 and 10 seconds")
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
