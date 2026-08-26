from sqlalchemy import select

from app.auth.database import SessionLocal
from app.auth.models import User
from app.auth.security import hash_password

db = SessionLocal()
try:
    u = db.scalar(select(User).where(User.username == "testuser"))
    if u is None:
        print("no testuser")
    else:
        u.password = hash_password("test123")
        db.commit()
        print("testuser password reset to test123")
finally:
    db.close()
