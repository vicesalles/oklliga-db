"""Aplica les migracions de sql/migrations/ a la base de dades.

Llegeix el DSN del fitxer .env (o la variable d'entorn OKLLIGA_DSN),
mai no assumeix localhost ni cap host per defecte.

Ús:
    python scripts/migrate.py             # aplica totes les migracions en ordre
    python scripts/migrate.py --status    # només mostra l'estat

Registra les migracions aplicades a la taula _schema_migration, que es
crea automàticament, i només executa les pendents. Cada migració s'aplica
dins d'una transacció pròpia.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, ".")

import psycopg

from oklliga.config import get_dsn

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sql" / "migrations"
MIGRATION_TABLE = "_schema_migration"


def ensure_migration_table(c: psycopg.Cursor) -> None:
    c.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            filename text PRIMARY KEY,
            applied_at timestamp NOT NULL DEFAULT now()
        )
        """
    )


def applied_migrations(c: psycopg.Cursor) -> set[str]:
    ensure_migration_table(c)
    c.execute(f"SELECT filename FROM {MIGRATION_TABLE}")
    return {row["filename"] for row in c.fetchall()}


def pending_migrations(applied: set[str]) -> list[Path]:
    return sorted(
        p for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in applied
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Aplica migracions pendents")
    parser.add_argument("dsn", nargs="?", default=None,
                        help="DSN explícit (override; per defecte llegeix .env)")
    parser.add_argument("--status", action="store_true",
                        help="mostra l'estat sense aplicar res")
    args = parser.parse_args()

    dsn = args.dsn or get_dsn()
    conn = psycopg.connect(dsn, row_factory=psycopg.rows.dict_row,
                           autocommit=True)
    try:
        c = conn.cursor()
        applied = applied_migrations(c)
        pending = pending_migrations(applied)

        if args.status:
            print(f"Aplicades ({len(applied)}):")
            for f in sorted(applied):
                print(f"  [x] {f}")
            print(f"Pendents ({len(pending)}):")
            for f in pending:
                print(f"  [ ] {f.name}")
            return

        if not pending:
            print("No hi ha migracions pendents.")
            return

        for f in pending:
            sql = f.read_text(encoding="utf-8")
            print(f"Aplicant {f.name} ...", end=" ")
            with conn.transaction():
                c.execute(sql)
                c.execute(
                    f"INSERT INTO {MIGRATION_TABLE} (filename) VALUES (%s)",
                    (f.name,),
                )
            print("OK")
        print(f"\n{len(pending)} migració(ns) aplicades.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
