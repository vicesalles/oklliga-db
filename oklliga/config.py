"""Lectura de configuració (credencials) des d'un fitxer .env.

Cap script del projecte no ha de rebre el DSN per línia de comandes ni
tenir credencials hardcodes. El patró és:

    OKLLIGA_DSN=postgresql://user:pass@localhost/oklliga

al fitxer .env de l'arrel del projecte (o el passat per OKLLIGA_ENV_FILE).

Ús:
    from oklliga.config import get_dsn
    dsn = get_dsn()
"""

from __future__ import annotations

import os
from pathlib import Path


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip("'").strip('"')
    return key, value


def load_env(env_file: str | Path | None = None) -> dict[str, str]:
    """Carrega un fitxer .env i retorna els parells clau=valor.

    No sobreescriu variables d'entorn ja definides (l'entorn guanya).
    """
    candidates: list[Path] = []
    if env_file:
        candidates.append(Path(env_file))
    else:
        candidates.append(Path(os.environ.get("OKLLIGA_ENV_FILE", ".env")))
        candidates.append(Path.home() / ".oklliga.env")

    values: dict[str, str] = {}
    for path in candidates:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                parsed = _parse_env_line(line)
                if parsed:
                    values.setdefault(parsed[0], parsed[1])
            break
    return values


def get_dsn(env_file: str | Path | None = None) -> str:
    """Retorna el DSN: variable d'entorn o fitxer .env.

    Prioritat: OKLLIGA_DSN del entorn > .env > error amb instruccions.
    """
    env_dsn = os.environ.get("OKLLIGA_DSN")
    if env_dsn:
        return env_dsn
    file_values = load_env(env_file)
    file_dsn = file_values.get("OKLLIGA_DSN")
    if file_dsn:
        return file_dsn
    raise RuntimeError(
        "No s'ha trobat OKLLIGA_DSN. Defineix-lo com a variable d'entorn o "
        "crea un fitxer .env a l'arrel amb: OKLLIGA_DSN=postgresql://..."
    )
