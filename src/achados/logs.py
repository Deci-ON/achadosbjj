import logging

FORMAT = "%(asctime)s %(levelname)s %(message)s"

# Cada requisição HTTP vira uma linha de INFO no httpx: ~1.500 por coleta, escondendo o resumo e os erros.
QUIET_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str | int = "INFO", root: logging.Logger | None = None) -> None:
    """Liga os logs do projeto e silencia o ruído de requisições.

    O uvicorn só configura os próprios loggers: sem isto, no modo serviço nada do `achados` (início do
    agendador, resumo de cada coleta, avisos de falha) chega ao `docker compose logs`. Idempotente.
    `root` só existe para os testes (o pytest mexe nos handlers do logger raiz de verdade).
    """
    root = root or logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
        root.setLevel(level)
    logging.getLogger("achados").setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
