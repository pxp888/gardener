"""Configuration loading and validation for Gardener Proxy."""

import os
from pathlib import Path
from typing import Literal, Optional
import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class UpstreamConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: Optional[str] = None


class GardenerSettings(BaseModel):
    model: Optional[str] = None
    turns: int = Field(default=3, ge=1, description="Garden every N turns")
    protected_turns: int = Field(default=5, ge=1, description="Last K assistant turns to keep verbatim")
    min_tokens_to_garden: int = Field(default=100, ge=0)
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    state_injection_mode: Literal["system", "user_assistant"] = "system"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    upstream: UpstreamConfig = Field(default_factory=UpstreamConfig)
    gardener: GardenerSettings = Field(default_factory=GardenerSettings)


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load config from YAML file with environment variable fallbacks."""
    data = {}

    candidate_paths = [
        config_path,
        os.environ.get("GARDENER_CONFIG"),
        "config.yaml",
        "config.yml",
    ]

    for p in candidate_paths:
        if p and Path(p).is_file():
            with open(p, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, dict):
                    data = loaded
            break

    cfg = AppConfig(**data)

    # Environment variable overrides
    if os.environ.get("GARDENER_HOST"):
        cfg.server.host = os.environ["GARDENER_HOST"]
    if os.environ.get("GARDENER_PORT"):
        cfg.server.port = int(os.environ["GARDENER_PORT"])

    if os.environ.get("OPENAI_BASE_URL"):
        cfg.upstream.base_url = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("OPENAI_API_KEY") and not cfg.upstream.api_key:
        cfg.upstream.api_key = os.environ["OPENAI_API_KEY"]

    if os.environ.get("PI_GARDENER_TURNS") or os.environ.get("GARDENER_TURNS"):
        val = os.environ.get("PI_GARDENER_TURNS") or os.environ.get("GARDENER_TURNS")
        cfg.gardener.turns = int(val)

    if os.environ.get("PI_GARDENER_PROTECTED_TURNS") or os.environ.get("GARDENER_PROTECTED_TURNS"):
        val = os.environ.get("PI_GARDENER_PROTECTED_TURNS") or os.environ.get("GARDENER_PROTECTED_TURNS")
        cfg.gardener.protected_turns = int(val)

    if os.environ.get("GARDENER_MODEL"):
        cfg.gardener.model = os.environ["GARDENER_MODEL"]

    return cfg
