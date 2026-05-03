"""
Migrate manually-onboarded tenants into registered_clients.

For each tenant that has NO registered_clients row (matched by tenant_id),
this script:
  1. Reads the tenant row and all tenant_db_credentials rows.
  2. Builds a purchased_products JSONB array from the credential rows
     (using product_slug / display_name if populated, falling back to
     sensible defaults for legacy single-DB tenants).
  3. Inserts a registered_clients row with tenant_id already set,
     contact_name = company_name (best guess for legacy rows),
     and synced_at = NOW().

This ensures existing tenants are found by find_registered_client_by_chat
via the tenant_id join path, so they are never rejected as "Tier 3 unknown".

Usage:
  python scripts/migrate_existing_tenants_to_registered_clients.py           # dry-run (default)
  python scripts/migrate_existing_tenants_to_registered_clients.py --apply   # commit changes
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# Allow imports from the project root when running as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import RegisteredClient, Tenant, TenantDBCredential


async def run(apply: bool) -> None:
    import os

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL is not set.")
        sys.exit(1)

    # asyncpg requires postgresql+asyncpg:// scheme
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    mode_label = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode_label}] Starting migration of existing tenants into registered_clients.\n")

    inserted = 0
    skipped = 0
    errors = 0

    async with session_factory() as session:
        # Load all tenants
        tenants_result = await session.execute(sa.select(Tenant))
        tenants: list[Tenant] = list(tenants_result.scalars().all())

        if not tenants:
            print("No tenants found in the database. Nothing to migrate.")
            return

        print(f"Found {len(tenants)} tenant(s) to evaluate.\n")

        for tenant in tenants:
            try:
                # Check if a registered_clients row already exists for this tenant_id
                existing_result = await session.execute(
                    sa.select(RegisteredClient).where(
                        RegisteredClient.tenant_id == tenant.id
                    )
                )
                existing = existing_result.scalars().first()

                if existing is not None:
                    print(
                        f"  SKIP  tenant={tenant.id} company={tenant.company_name!r} "
                        f"— registered_clients row {existing.id} already exists."
                    )
                    skipped += 1
                    continue

                # Load all credential rows for this tenant
                creds_result = await session.execute(
                    sa.select(TenantDBCredential)
                    .where(TenantDBCredential.tenant_id == tenant.id)
                    .order_by(TenantDBCredential.last_connected_at.asc().nulls_last())
                )
                creds: list[TenantDBCredential] = list(creds_result.scalars().all())

                # Build purchased_products from credential rows.
                # Legacy single-DB tenants often have no product_slug — synthesise one.
                purchased_products: list[dict] = []
                seen_slugs: set[str] = set()
                for cred in creds:
                    slug = cred.product_slug
                    if not slug:
                        # Derive a stable slug from the credential id so duplicates are
                        # detectable even without a real product_slug.
                        slug = f"db_{str(cred.id)[:8]}"
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)
                    display = cred.display_name or slug
                    purchased_products.append({"slug": slug, "display_name": display})

                # Resolve chat identifiers from the tenant row
                tg_id = tenant.telegram_chat_id
                wa_num = tenant.whatsapp_number

                new_client = RegisteredClient(
                    id=uuid.uuid4(),
                    company_name=tenant.company_name,
                    # Legacy rows have no separate contact name — use company name as fallback.
                    contact_name=tenant.company_name,
                    telegram_chat_id=tg_id,
                    whatsapp_number=wa_num,
                    purchased_products=purchased_products,
                    is_active=True,
                    tenant_id=tenant.id,
                )

                print(
                    f"  INSERT tenant={tenant.id} company={tenant.company_name!r} "
                    f"tg={tg_id!r} wa={wa_num!r} "
                    f"products={[p['slug'] for p in purchased_products]}"
                )

                if apply:
                    session.add(new_client)

                inserted += 1

            except Exception as exc:
                print(f"  ERROR  tenant={tenant.id}: {exc}")
                errors += 1

        if apply and inserted > 0:
            await session.commit()
            print(f"\n[APPLY] Committed {inserted} new registered_clients row(s).")
        elif not apply and inserted > 0:
            print(f"\n[DRY-RUN] Would insert {inserted} registered_clients row(s). Re-run with --apply to commit.")

    print(
        f"\nDone. inserted={inserted} skipped={skipped} errors={errors}"
    )

    await engine.dispose()


def main() -> None:
    apply = "--apply" in sys.argv
    asyncio.run(run(apply=apply))


if __name__ == "__main__":
    main()
