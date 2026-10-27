from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://apuser:apuser@db:5432/autophoneagent"
    redis_url: str = "redis://redis:6379/0"
    jwt_secret: str = "dev-secret-change-me"
    jwt_alg: str = "HS256"
    access_token_minutes: int = 60 * 12
    app_base_url: str = "https://app.autophoneagent.com"
    env: str = "prod"

    class Config:
        env_file = ".env"


settings = Settings()
