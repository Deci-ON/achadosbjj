import logging

import pytest

from achados import service
from achados.config import Settings
from achados.logs import FORMAT, QUIET_LOGGERS, configure_logging
from achados.scheduler import CollectScheduler


@pytest.fixture
def isolated():
    """Logger raiz de mentira (o pytest reinstala handlers no de verdade a cada fase) e níveis restaurados."""
    names = ("achados", *QUIET_LOGGERS)
    levels = {n: logging.getLogger(n).level for n in names}
    yield logging.Logger("raiz-de-teste")
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_sem_handler_liga_um_com_o_formato_do_projeto(isolated):
    configure_logging(root=isolated)

    assert len(isolated.handlers) == 1 and isolated.level == logging.INFO
    assert isolated.handlers[0].formatter._fmt == FORMAT
    assert logging.getLogger("achados").isEnabledFor(logging.INFO)


def test_e_idempotente_nao_duplica_handler(isolated):
    configure_logging(root=isolated)
    configure_logging(root=isolated)

    assert len(isolated.handlers) == 1


def test_com_handler_ja_configurado_deixa_os_handlers_em_paz(isolated):
    handler = logging.NullHandler()
    isolated.addHandler(handler)

    configure_logging(root=isolated)

    assert isolated.handlers == [handler]
    assert logging.getLogger("achados").isEnabledFor(logging.INFO)  # mas o nível do projeto vale


def test_silencia_o_ruido_de_requisicoes_e_respeita_o_nivel_pedido(isolated):
    configure_logging("DEBUG", root=isolated)

    for name in QUIET_LOGGERS:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    assert logging.getLogger("achados").isEnabledFor(logging.DEBUG)


def test_o_servico_liga_o_log_com_o_nivel_das_configuracoes(monkeypatch, sessions):
    calls = []
    monkeypatch.setattr(service, "configure_logging", calls.append)
    scheduler = CollectScheduler(sessions, None, lambda: None, "mercadolivre", interval=3600, enabled=False)

    service.create_app(Settings(_env_file=None, log_level="WARNING"), sessions=sessions, scheduler=scheduler)

    assert calls == ["WARNING"]


def test_log_level_padrao_e_info():
    assert Settings(_env_file=None).log_level == "INFO"
