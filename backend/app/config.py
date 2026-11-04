"""Runtime settings, model names and pipeline thresholds.

Everything tunable lives here so the pipeline modules stay free of magic numbers.
Values come from environment variables or a `.env` file (see `.env.example`).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Meta Model API -------------------------------------------------
    meta_api_key: str = ""
    meta_base_url: str = "https://api.meta.ai/v1"
    model_spark: str = "muse-spark-1.3"
    model_sam: str = "sam-3.1"
    llm_client: Literal["meta", "mock"] = "meta"
    request_timeout_s: float = 120.0

    # Reasoning effort per Spark call type (Chat Completions `reasoning_effort`)
    effort_transcribe: Effort = "low"
    effort_interpret: Effort = "low"
    effort_reread: Effort = "medium"
    effort_locate: Effort = "low"

    # --- Prices (USD) used for cost-per-sheet reporting -----------------
    # https://dev.meta.ai/docs/pricing-rate-limits
    price_spark_input_per_m: float = 1.25
    price_spark_cached_input_per_m: float = 0.15
    price_spark_output_per_m: float = 4.25
    price_sam_per_image: float = 2.50 / 1000

    # --- Storage --------------------------------------------------------
    data_dir: Path = REPO_ROOT / "data"
    # Optional separate cache location (the evaluation shares one cache across ablation runs)
    cache_dir_override: Path | None = Field(default=None, validation_alias="CACHE_DIR")

    # --- Segmentation ---------------------------------------------------
    sam_max_side: int = 1600  # sheets are downscaled to this before SAM
    sam_min_score: float = 0.35  # drop detections below this SAM confidence
    crop_padding: float = 0.02  # fraction of sheet size added around text crops
    crop_max_side: int = 2048  # text crops are downscaled to this before Spark

    # --- Verification & confidence -------------------------------------
    max_rereads_per_field: int = 2
    confidence_high: float = 0.85
    confidence_medium: float = 0.60
    gbif_min_match_confidence: int = 90
    earliest_plausible_year: int = 1700

    # --- API -----------------------------------------------------------
    # No accounts: mutating requests (which spend API credit) are accepted from localhost only
    allow_remote_writes: bool = False

    # --- Optional georeferencing ---------------------------------------
    geocode_enabled: bool = False
    geocode_user_agent: str = "herbarium-label-transcriber/0.1"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "herbarium.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def crops_dir(self) -> Path:
        return self.data_dir / "crops"

    @property
    def cache_dir(self) -> Path:
        return self.cache_dir_override or self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.images_dir, self.crops_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
