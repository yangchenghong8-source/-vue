"""Auth routes: register, login, me."""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.database import get_db
from app.auth.deps import _get_current_user
from app.auth.models import User
from app.auth.schemas import RegisterRequest, UserPublic
from app.auth.security import create_access_token
from app.models.exception import HttpException
from app.utils import utils

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/register")
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    existing = db.scalar(select(User).where(User.username == body.username))
    if existing is not None:
        raise HttpException(task_id="auth", status_code=409, message="用户名已存在")

    user = User(
        username=body.username,
        password=body.password,
        nickname=body.nickname or body.username,
        email=body.email,
        role="user",
        status=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    return utils.get_response(
        200,
        {
            "access_token": token,
            "token_type": "bearer",
            "user": UserPublic.model_validate(user).model_dump(),
        },
    )


@router.post("/login")
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(User.username == form.username))
    if user is None or form.password != user.password:
        raise HttpException(task_id="auth", status_code=401, message="用户名或密码错误")

    if user.status != 0:
        raise HttpException(task_id="auth", status_code=403, message="账号已停用")

    user.last_login_at = datetime.now()
    user.last_login_ip = _client_ip(request)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id)
    return utils.get_response(
        200,
        {
            "access_token": token,
            "token_type": "bearer",
            "user": UserPublic.model_validate(user).model_dump(),
        },
    )


@router.get("/me")
def me(user: User = Depends(_get_current_user)):
    return utils.get_response(200, UserPublic.model_validate(user).model_dump())
