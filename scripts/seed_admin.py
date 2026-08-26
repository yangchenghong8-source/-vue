"""Bootstrap the users table and an initial admin account.

Run inside the api container (or any env with the deps installed):

    python scripts/seed_admin.py

Idempotent: creates the table if missing, and only inserts the admin when the
``admin`` username does not exist.
"""
import os
import sys

from sqlalchemy import select

from app.auth.database import Base, SessionLocal, engine
from app.auth.models import User
from app.auth.security import hash_password

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


def main() -> int:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.username == ADMIN_USERNAME))
        if existing is not None:
            print(f"admin user {ADMIN_USERNAME!r} already exists, skipping")
            return 0
        db.add(
            User(
                username=ADMIN_USERNAME,
                password=hash_password(ADMIN_PASSWORD),
                nickname="Administrator",
                email="",
                role="admin",
                status=0,
            )
        )
        db.commit()
        print(f"created admin user {ADMIN_USERNAME!r} (role=admin)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
