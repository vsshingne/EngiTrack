from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")
    environment: str = "development"
    database_url: str = "sqlite:///./engitrack.db"
    jwt_secret: str
    token_minutes: int = 60
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    @model_validator(mode="after")
    def secure_config(self):
        if len(self.jwt_secret) < 32 or self.jwt_secret.startswith("replace-"):
            raise ValueError("JWT_SECRET must be a unique secret of at least 32 characters")
        if self.environment == "production" and not self.database_url.startswith("postgresql"):
            raise ValueError("Production requires PostgreSQL")
        return self


settings = Settings()
