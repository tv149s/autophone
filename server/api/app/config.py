from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://apuser:apuser@db:5432/autophoneagent"
    redis_url: str = "redis://redis:6379/0"
    jwt_secret: str = "dev-secret-change-me"
    jwt_alg: str = "HS256"
    access_token_minutes: int = 60 * 2        # access 短期(2 小时);过期用 refresh 静默换新
    refresh_token_days: int = 30              # refresh 令牌有效期(aifindcar 同款 30 天)
    app_base_url: str = "https://app.autophoneagent.com"
    google_client_id: str = ""      # Google 网页 OAuth 客户端 ID(ID-token 校验用 audience)
    env: str = "prod"

    class Config:
        env_file = ".env"


settings = Settings()
