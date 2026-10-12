import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fpc_fakes  # noqa: E402

if not Path(fpc_fakes.FPC_PATH, "FunPayAPI").exists():
    pytest.skip("Укажите путь к исходникам FunPay Cardinal в FPC_PATH", allow_module_level=True)


@pytest.fixture
def env(tmp_path):
    """Возвращает функцию: загрузить плагин на свежем Cardinal в пустой рабочей папке."""
    cwd = os.getcwd()
    loaded = []

    def make(file_name, telegram=True, init=True):
        cardinal = fpc_fakes.FakeCardinal(telegram=telegram)
        module = fpc_fakes.load_plugin(file_name, tmp_path)
        if init:
            for handler in getattr(module, "BIND_TO_PRE_INIT", []):
                handler(cardinal)
        loaded.append((module, cardinal))
        return module, cardinal

    yield make
    for module, cardinal in loaded:
        for handler in getattr(module, "BIND_TO_PRE_STOP", []) or []:
            try:
                handler(cardinal)
            except Exception:
                pass
    os.chdir(cwd)
