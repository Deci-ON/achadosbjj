from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://achados:achados@localhost:5432/achados"

    shopee_app_id: str = ""
    shopee_secret: str = ""
    shopee_api_url: str = "https://open-api.affiliate.shopee.com.br/graphql"

    ml_client_id: str = ""
    ml_client_secret: str = ""
    ml_proxy_url: str | None = None

    request_timeout: float = 20.0
    log_level: str = "INFO"

    # Serviço (achados.service): coleta agendada e API.
    collect_enabled: bool = True
    collect_interval_seconds: float = 21600.0  # 6h
    collect_startup_delay_seconds: float = 30.0
    collect_preset: str = "bjj"
    static_dir: str = ""  # pasta do front compilado (Vite); vazia = só a API
    admin_token: str = ""  # sem ele, os endpoints /api/* respondem 503 (nunca ficam abertos por esquecimento)


def get_settings() -> Settings:
    return Settings()
