"""Database layer (Person 1).

Portable across PostgreSQL (deployment) and SQLite (local):
- ``base.Base``      Declarative base shared by every table
- ``models``         the 9 canonical tables (spec section 5.2)
- ``money``          NUMERIC money type that never round-trips through float
- ``session``        async engine/session factory with SQLite pragmas
"""

__all__ = []
