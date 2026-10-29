from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_bucket: str = "documents"
    s3_force_path_style: bool = False

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expires_in: int = 3600

    @field_validator("s3_endpoint_url", "s3_access_key_id", "s3_secret_access_key", mode="before")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        return value or None


@lru_cache
def get_settings() -> Settings:
    return Settings()
