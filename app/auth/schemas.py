"""Pydantic schemas for auth endpoints."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class UserPublic(BaseModel):
    id: int
    username: str
    nickname: str
    email: str
    role: str
    status: int
    last_login_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=30)
    password: str = Field(min_length=6, max_length=64)
    confirm_password: str
    nickname: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=50)

    @field_validator("username")
    @classmethod
    def _username_ok(cls, v: str) -> str:
        if not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("用户名只能包含字母、数字、下划线、连字符")
        return v

    @field_validator("confirm_password")
    @classmethod
    def _passwords_match(cls, v: str, info):
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("两次输入的密码不一致")
        return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic
