"""inventario versionato: inventory_versions, inventory_head, audit

Vedi BACKEND-PLAN.md §8.11 (commit atomico) e §8.9 (audit dal server).

Note di progetto materializzate qui:

- `inventory_versions.version` è **generata dal database** (identity bigint). Non
  si calcola `head + 1` in applicazione: il numero è unico per costruzione anche
  se qualcuno aggirasse il lock. I buchi possibili (una transazione annullata
  consuma un valore) sono irrilevanti, perché il client confronta la versione per
  uguaglianza con la testa, non conta gli incrementi.

- `inventory_head` è un singleton: `id boolean PRIMARY KEY CHECK (id)` ammette
  una sola riga possibile. È il punto di serializzazione delle scritture
  (`SELECT ... FOR UPDATE`) e l'unica fonte della versione corrente: nessuna
  lettura usa `MAX(version)`.

- `audit.actor_user_id` è un uuid **senza foreign key**: la tabella `users`
  arriva con l'autenticazione (punto 5 di §9). La FK va aggiunta in quella
  migrazione. `actor_username` e `actor_role` sono istantanee denormalizzate:
  devono sopravvivere alla disattivazione dell'utente (§8.6) e a un cambio di
  ruolo, perché l'audit racconta chi era quella persona *allora*.

Revision ID: 0002_inventory
Revises: 0001_baseline
Create Date: 2026-08-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002_inventory"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory_versions",
        sa.Column("version", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("doc", pg.JSONB, nullable=False),
        # SHA-256 della forma canonica (default materializzati, _uid rimossi,
        # chiavi ordinate): è ciò che rende riconoscibile un salvataggio a vuoto
        # senza confrontare interi documenti.
        sa.Column("canonical_sha256", sa.Text, nullable=False),
        sa.Column("actor_username", sa.Text, nullable=False),
        sa.Column("actor_role", sa.Text, nullable=False),
        sa.Column("actor_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("actor_role IN ('view','edit','admin')", name="ck_versions_role"),
        sa.CheckConstraint("length(canonical_sha256) = 64", name="ck_versions_sha_len"),
    )
    op.create_index("ix_inventory_versions_created_at", "inventory_versions", ["created_at"])
    op.create_index("ix_inventory_versions_sha", "inventory_versions", ["canonical_sha256"])

    op.create_table(
        "inventory_head",
        sa.Column("id", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column("version", sa.BigInteger,
                  sa.ForeignKey("inventory_versions.version", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id IS TRUE", name="ck_head_singleton"),
    )

    op.create_table(
        "audit",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_username", sa.Text, nullable=False),
        sa.Column("actor_role", sa.Text, nullable=False),
        sa.Column("ip", pg.INET, nullable=True),
        sa.Column("inventory_version", sa.BigInteger,
                  sa.ForeignKey("inventory_versions.version", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("scopes", pg.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("events", pg.JSONB, nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        # Testo di comodo fornito dal client: NON attendibile. Il nome della
        # colonna lo dice a chi la interroga (§8.9).
        sa.Column("client_hint", sa.Text, nullable=True),
        sa.CheckConstraint("actor_role IN ('view','edit','admin')", name="ck_audit_role"),
    )
    op.create_index("ix_audit_ts", "audit", [sa.text("ts DESC")])
    op.create_index("ix_audit_inventory_version", "audit", ["inventory_version"])


def downgrade() -> None:
    op.drop_table("audit")
    op.drop_table("inventory_head")
    op.drop_table("inventory_versions")
