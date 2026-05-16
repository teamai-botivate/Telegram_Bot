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
from .core import _fernet
def _get_fernet() -> Fernet:
	global _fernet

	if not FERNET_SECRET_KEY:
		raise RuntimeError("FERNET_SECRET_KEY is not configured. Add it to your .env file.")

	if _fernet is None:
		_fernet = Fernet(FERNET_SECRET_KEY.encode())

	return _fernet

def encrypt_credential_value(value: str) -> str:
	return _get_fernet().encrypt(value.encode()).decode()

def _decrypt_credential_value(value: str) -> str:
	return _get_fernet().decrypt(value.encode()).decode()

def _sanitize_select_sql(sql: str, allow_select_star: bool = False) -> str:
	cleaned = sql.strip().rstrip(";").strip()
	if not cleaned:
		raise SecurityError("Query is empty.")

	if ";" in cleaned:
		raise SecurityError("Multiple statements are not allowed.")

	lowered = cleaned.lower()
	if not (lowered.startswith("select") or lowered.startswith("with")):
		raise SecurityError("Only SELECT or WITH statements are allowed.")

	blocked_keywords = ("insert", "update", "delete", "drop", "truncate", "alter", "create", "grant", "revoke")
	for keyword in blocked_keywords:
		if re.search(rf"\b{keyword}\b", lowered):
			raise SecurityError(f"Disallowed keyword detected: {keyword.upper()}")

	return cleaned

