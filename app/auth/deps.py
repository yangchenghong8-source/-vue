"""FastAPI dependencies for current-user resolution."""
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.auth.database import get_db
from app.auth.models import User
from app.auth.security import decode_access_token
from app.models.exception import HttpException

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

# 视频流/下载接口由浏览器原生 <video>/<a> 标签请求，无法携带 Authorization 头；
# 登录时前端会把 JWT 写入同名 cookie，这里作为无 Authorization 头时的回退读取。
ACCESS_TOKEN_COOKIE = "mpt_access_token"


def _get_current_user(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if not token:
        raise HttpException(task_id="auth", status_code=401, message="not authenticated")
    user_id = decode_access_token(token)
    if user_id is None:
        raise HttpException(task_id="auth", status_code=401, message="invalid or expired token")
    user = db.get(User, user_id)
    if user is None:
        raise HttpException(task_id="auth", status_code=401, message="user not found")
    if user.status != 0:
        raise HttpException(task_id="auth", status_code=403, message="account disabled")
    return user
