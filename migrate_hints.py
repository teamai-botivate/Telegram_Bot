import asyncio
from sqlalchemy import select

from app.database import session_factory, refresh_schema_blueprint
from app.models import TenantDBCredential

async def migrate():
    print("Starting hint migration...")
    if not session_factory:
        print("DATABASE_URL is not configured.")
        return

    async with session_factory() as session:
        statement = select(TenantDBCredential).where(TenantDBCredential.db_type.ilike('postgresql'))
        result = await session.execute(statement)
        credentials = result.scalars().all()

    print(f"Found {len(credentials)} PostgreSQL tenants to migrate.")

    for cred in credentials:
        print(f"Refreshing schema blueprint for tenant {cred.tenant_id}...")
        try:
            await refresh_schema_blueprint(str(cred.tenant_id))
            print(f"  -> Success: {cred.tenant_id}")
        except Exception as e:
            print(f"  -> Failed: {cred.tenant_id} ({e})")

    print("Migration complete.")

if __name__ == "__main__":
    asyncio.run(migrate())
