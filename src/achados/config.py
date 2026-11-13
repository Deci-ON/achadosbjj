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
    # Coleta agendada da Shopee (API de afiliados). Só roda com SHOPEE_APP_ID e SHOPEE_SECRET definidos.
    shopee_collect_enabled: bool = True
    shopee_collect_interval_seconds: float = 21600.0  # 6h, como o ML
    # Só ofertas do ML com pontuação >= isto entram na fila de links (a geração do link é um clique seu).
    # 60 ~ queda de uns 23% sobre a mediana (17% se o "de" do vendedor foi praticado); 0 = todas as ofertas.
    link_min_score: int = 60
    # Robô de envio ao WhatsApp (sidecar `sender/`, whatsapp-web.js). Vazio = desligado: só copiar e colar.
    sender_url: str = ""  # ex.: http://sender:3000
    sender_token: str = ""
    sender_daily_cap: int = 30  # mensagens por dia (cada grupo conta uma)
    sender_min_delay_seconds: float = 60.0  # intervalo aleatório entre duas mensagens
    sender_max_delay_seconds: float = 180.0
    sender_window_start_hour: int = 8  # só envia nesta janela (horário de Brasília)
    sender_window_end_hour: int = 22
    admin_token: str = ""  # sem ele, os endpoints /api/* respondem 503 (nunca ficam abertos por esquecimento)


def get_settings() -> Settings:
    return Settings()
