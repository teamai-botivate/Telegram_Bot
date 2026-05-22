from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, RegisteredClient, Tenant, TenantDBCredential

from .core import *
async def store_query_example(
    tenant_id: uuid.UUID | str,
    question: str,
    sql: str,
    product_connection_id: uuid.UUID | str | None = None,
    verified_by: str = "auto",
) -> uuid.UUID | None:
    if session_factory is None:
        logger.warning("store_query_example: DATABASE_URL not configured, skipping.")
        return None

    from app.embeddings import embed_text

    embedding = await embed_text(question)
    if embedding is None:
        logger.warning("store_query_example: embedding failed for question=%r, skipping store.", question[:80])
        return None

    tenant_uuid = uuid.UUID(str(tenant_id))
    product_conn_uuid = uuid.UUID(str(product_connection_id)) if product_connection_id else None
    # Pass embedding as a vector-castable string literal; avoids needing asyncpg register_vector.
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    async with session_factory() as session:
        # Upsert: increment success_count if same (tenant, question) already exists.
        find_stmt = text(
            "SELECT id FROM tenant_query_examples "
            "WHERE tenant_id = :tenant_id "
            "AND LOWER(TRIM(question)) = LOWER(TRIM(:question)) "
            "LIMIT 1"
        )
        result = await session.execute(find_stmt, {"tenant_id": tenant_uuid, "question": question})
        existing_id = result.scalar_one_or_none()

        if existing_id is not None:
            update_stmt = text(
                "UPDATE tenant_query_examples "
                "SET success_count = success_count + 1, last_used_at = NOW() "
                "WHERE id = :id"
            )
            await session.execute(update_stmt, {"id": existing_id})
            await session.commit()
            return existing_id

        insert_stmt = text(
            "INSERT INTO tenant_query_examples "
            "(tenant_id, product_connection_id, question, sql, question_embedding, verified_by) "
            "VALUES (:tenant_id, :product_connection_id, :question, :sql, "
            "CAST(:embedding AS vector), :verified_by) "
            "RETURNING id"
        )
        insert_result = await session.execute(
            insert_stmt,
            {
                "tenant_id": tenant_uuid,
                "product_connection_id": product_conn_uuid,
                "question": question,
                "sql": sql,
                "embedding": embedding_str,
                "verified_by": verified_by,
            },
        )
        new_id = insert_result.scalar_one()
        await session.commit()
        return new_id

async def retrieve_similar_examples(
    tenant_id: uuid.UUID | str,
    question: str,
    product_connection_id: uuid.UUID | str | None = None,
    limit: int = 5,
    precomputed_embedding: list[float] | None = None,
) -> list[dict]:
    if session_factory is None:
        return []

    if precomputed_embedding is not None:
        embedding = precomputed_embedding
    else:
        from app.embeddings import embed_text
        embedding = await embed_text(question)

    if embedding is None:
        logger.warning("retrieve_similar_examples: embedding failed, returning empty for tenant=%s.", tenant_id)
        return []

    tenant_uuid = uuid.UUID(str(tenant_id))
    product_conn_uuid = uuid.UUID(str(product_connection_id)) if product_connection_id else None
    embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"

    if product_conn_uuid is not None:
        scope_filter = "AND product_connection_id = :product_connection_id "
    else:
        scope_filter = ""

    query = text(
        "SELECT question, sql, "
        "1 - (question_embedding <=> CAST(:embedding AS vector)) AS similarity "
        "FROM tenant_query_examples "
        "WHERE tenant_id = :tenant_id "
        + scope_filter +
        "ORDER BY question_embedding <=> CAST(:embedding AS vector) "
        "LIMIT :limit"
    )

    params: dict = {
        "tenant_id": tenant_uuid,
        "embedding": embedding_str,
        "limit": limit,
    }
    if product_conn_uuid is not None:
        params["product_connection_id"] = product_conn_uuid

    async with session_factory() as session:
        result = await session.execute(query, params)
        rows = result.mappings().all()

    return [
        {"question": row["question"], "sql": row["sql"], "similarity": float(row["similarity"])}
        for row in rows
        if float(row["similarity"]) >= 0.5
    ]

async def deactivate_stale_examples(tenant_id: uuid.UUID | str, days: int = 90) -> int:
    # TODO: add is_active boolean column to tenant_query_examples and implement soft-delete here.
    logger.info("deactivate_stale_examples: not yet implemented (no is_active column).")
    return 0

