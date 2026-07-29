from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["ollama", "openai_compatible"]
EvalProfileName = Literal["local", "cloud"]
ExtractionStrategy = Literal["cheap_only", "expensive_only", "cascade"]


class EvalProviderConfig(BaseModel):
    profile: EvalProfileName
    provider: LLMProviderName
    base_url: str
    api_key: SecretStr | None = None
    model: str
    native_structured_output: bool
    timeout_seconds: float = Field(gt=0)
    input_price_per_million: Decimal = Field(ge=0)
    output_price_per_million: Decimal = Field(ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCSIFT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DocSift Agent"
    database_url: str = "postgresql+asyncpg://docsift:docsift-local@localhost:5432/docsift"
    storage_path: Path = Path("var/uploads")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    upload_chunk_bytes: int = Field(default=1024 * 1024, gt=0)
    llm_provider: LLMProviderName = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: SecretStr | None = None
    llm_model: str = "qwen2.5:7b-instruct"
    llm_native_structured_output: bool = True
    llm_timeout_seconds: float = Field(default=180.0, gt=0)
    llm_max_concurrency: int = Field(default=1, ge=1)
    llm_num_predict: int = Field(default=1024, ge=1)
    llm_num_ctx: int = Field(default=4096, ge=1)
    llm_warmup: bool = True
    llm_prompt_version: str = Field(default="v1", pattern=r"^[A-Za-z0-9._-]+$")
    eval_local_provider: LLMProviderName = "ollama"
    eval_local_base_url: str = "http://localhost:11434"
    eval_local_api_key: SecretStr | None = None
    eval_local_model: str = "qwen2.5-coder:7b"
    eval_local_native_structured_output: bool = True
    eval_local_timeout_seconds: float = Field(default=300.0, gt=0)
    eval_local_input_price_per_million: Decimal = Field(default=Decimal(0), ge=0)
    eval_local_output_price_per_million: Decimal = Field(default=Decimal(0), ge=0)
    eval_cloud_provider: LLMProviderName = "openai_compatible"
    eval_cloud_base_url: str = ""
    eval_cloud_api_key: SecretStr | None = None
    eval_cloud_model: str = ""
    eval_cloud_native_structured_output: bool = True
    eval_cloud_timeout_seconds: float = Field(default=120.0, gt=0)
    eval_cloud_input_price_per_million: Decimal = Field(default=Decimal(0), ge=0)
    eval_cloud_output_price_per_million: Decimal = Field(default=Decimal(0), ge=0)
    pdf_max_pages: int = Field(default=200, gt=0)
    pdf_max_render_megapixels: int = Field(default=40, gt=0)
    image_max_megapixels: int = Field(default=50, gt=0)
    guardrail_confidence_threshold: float = Field(default=0.80, ge=0, le=1)
    guardrail_document_max_age_days: int = Field(default=3650, ge=1)
    guardrail_document_future_tolerance_days: int = Field(default=1, ge=0)
    cascade_confidence_threshold: float = Field(default=0.85, ge=0, le=1)
    allowed_content_types: tuple[str, ...] = (
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
    )

    def eval_profile(self, profile: EvalProfileName) -> EvalProviderConfig:
        if profile == "local":
            return EvalProviderConfig(
                profile=profile,
                provider=self.eval_local_provider,
                base_url=self.eval_local_base_url,
                api_key=self.eval_local_api_key,
                model=self.eval_local_model,
                native_structured_output=self.eval_local_native_structured_output,
                timeout_seconds=self.eval_local_timeout_seconds,
                input_price_per_million=self.eval_local_input_price_per_million,
                output_price_per_million=self.eval_local_output_price_per_million,
            )
        return EvalProviderConfig(
            profile=profile,
            provider=self.eval_cloud_provider,
            base_url=self.eval_cloud_base_url,
            api_key=self.eval_cloud_api_key,
            model=self.eval_cloud_model,
            native_structured_output=self.eval_cloud_native_structured_output,
            timeout_seconds=self.eval_cloud_timeout_seconds,
            input_price_per_million=self.eval_cloud_input_price_per_million,
            output_price_per_million=self.eval_cloud_output_price_per_million,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()