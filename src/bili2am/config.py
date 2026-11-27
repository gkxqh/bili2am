"""Configuration management for bili2am."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DEFAULT_CONFIG_DIR = Path.home() / ".config" / "bili2am"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "bili2am"


class CookieConfig(BaseModel):
    """Browser cookie reading configuration."""

    enabled: bool = True
    priority: list[str] = Field(default_factory=lambda: ["safari", "edge", "chrome"])


class OCRConfig(BaseModel):
    """Frame extraction and Apple Vision OCR settings."""

    interval_sec: int = 15
    max_frames: int = 240
    languages: list[str] = Field(default_factory=lambda: ["zh-Hans", "zh-Hant", "en-US", "ja-JP"])
    video_height_limit: int = 720
    roi: str | None = None  # Optional ffmpeg crop string e.g. "iw:ih*0.35:0:ih*0.65"


class ShazamConfig(BaseModel):
    """Audio fallback recognition settings."""

    sample_duration_sec: int = 12
    fallback_interval_sec: int = 60
    max_segments: int = 80
    concurrency: int = 2


class LLMConfig(BaseModel):
    """Optional LLM normalization settings (disabled by default)."""

    provider: Literal["none", "ollama", "openai"] = "none"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_api_key: str = ""


class AppConfig(BaseModel):
    """Root application configuration."""

    storefront: str = "cn"
    cache_dir: str = str(DEFAULT_CACHE_DIR)
    cookie: CookieConfig = Field(default_factory=CookieConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    shazam: ShazamConfig = Field(default_factory=ShazamConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


def get_config_path() -> Path:
    env_path = os.environ.get("BILI2AM_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_DIR / "config.toml"


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from TOML file if present, otherwise return defaults."""
    cfg_path = path or get_config_path()
    if not cfg_path.exists():
        return AppConfig()
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)
    return AppConfig.model_validate(raw)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        items = ", ".join(f'"{str(v)}"' for v in value)
        return f"[{items}]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    """Persist configuration as clean TOML."""
    cfg_path = path or get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"storefront = {_toml_value(cfg.storefront)}",
        f"cache_dir = {_toml_value(cfg.cache_dir)}",
        "",
        "[cookie]",
        f"enabled = {_toml_value(cfg.cookie.enabled)}",
        f"priority = {_toml_value(cfg.cookie.priority)}",
        "",
        "[ocr]",
        f"interval_sec = {_toml_value(cfg.ocr.interval_sec)}",
        f"max_frames = {_toml_value(cfg.ocr.max_frames)}",
        f"languages = {_toml_value(cfg.ocr.languages)}",
        f"video_height_limit = {_toml_value(cfg.ocr.video_height_limit)}",
    ]
    if cfg.ocr.roi:
        lines.append(f"roi = {_toml_value(cfg.ocr.roi)}")
    lines.extend(
        [
            "",
            "[shazam]",
            f"sample_duration_sec = {_toml_value(cfg.shazam.sample_duration_sec)}",
            f"fallback_interval_sec = {_toml_value(cfg.shazam.fallback_interval_sec)}",
            f"max_segments = {_toml_value(cfg.shazam.max_segments)}",
            f"concurrency = {_toml_value(cfg.shazam.concurrency)}",
            "",
            "[llm]",
            f"provider = {_toml_value(cfg.llm.provider)}",
            f"ollama_base_url = {_toml_value(cfg.llm.ollama_base_url)}",
            f"ollama_model = {_toml_value(cfg.llm.ollama_model)}",
            f"openai_base_url = {_toml_value(cfg.llm.openai_base_url)}",
            f"openai_model = {_toml_value(cfg.llm.openai_model)}",
            f"openai_api_key = {_toml_value(cfg.llm.openai_api_key)}",
            "",
        ]
    )
    cfg_path.write_text("\n".join(lines), encoding="utf-8")
    return cfg_path
