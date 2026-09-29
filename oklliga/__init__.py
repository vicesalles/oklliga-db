"""Connector per a la base de dades de l'OK Lliga.

Capa d'accés estable per a l'scraper: l'scraper només parla amb
aquest mòdul, mai amb SQL directament. Si l'esquema evoluciona,
només cal adaptar aquest connector.
"""

from .client import OkLligaDB
from .resolution import NameResolver
from .audit import ClubAuditor

__all__ = ["OkLligaDB", "NameResolver", "ClubAuditor"]
__version__ = "0.1.0"
