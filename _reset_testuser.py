from sqlalchemy import select

from app.auth.database import SessionLocal
from app.auth.models import User

db = SessionLocal()
try:
    u = db.scalar(select(User).where(User.username == "testuser"))
    if u is None:
        print("no testuser")
    else:
        u.password = "test123"
        db.commit()
        print("testuser password reset to test123")
finally:
    db.close()
