"""FastAPI dependencies for current-user resolution."""
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth.database import get_db
from app.auth.models import User
from app.auth.security import decode_access_token
from app.models.exception import HttpException

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def _get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    user_id = decode_access_token(token)
    if user_id is None:
        raise HttpException(task_id="auth", status_code=401, message="invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HttpException(task_id="auth", status_code=401, message="user not found")
    if user.status != 0:
        raise HttpException(task_id="auth", status_code=403, message="account disabled")
    return user
