"""Connector per a la base de dades de l'OK Lliga.

Capa d'accés estable per a l'scraper: l'scraper només parla amb
aquest mòdul, mai amb SQL directament. Si l'esquema evoluciona,
només cal adaptar aquest connector.
"""

from .client import OkLligaDB

__all__ = ["OkLligaDB"]
__version__ = "0.1.0"
