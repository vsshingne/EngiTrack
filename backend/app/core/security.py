from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models import User

passwords = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


def token_for(user: User) -> str:
    return jwt.encode(
        {
            "sub": str(user.id),
            "ver": user.token_version,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.token_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(
            credentials.credentials if credentials else "",
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp", "sub", "ver"]},
        )
        user = db.get(User, int(payload["sub"]))
        if not user or not user.active or user.token_version != payload["ver"]:
            raise ValueError()
        return user
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise HTTPException(
            401, "Invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"}
        ) from None
