"""Auth configuration, resolved from environment with local-dev defaults."""
import os


def _env(name, default):
    value = os.getenv(name)
    return value if value not in (None, "") else default


# MySQL connection. Defaults assume the docker-compose ``mysql`` service on the
# docker bridge network (hostname ``mysql``).
DATABASE_URL = _env(
    "AUTH_DATABASE_URL",
    "mysql+pymysql://mpt:mpt_secret@mysql:3306/mpt?charset=utf8mb4",
)

# JWT settings.
JWT_SECRET_KEY = _env("JWT_SECRET_KEY", "change-me-in-production-please-9f8a7b6c")
JWT_ALGORITHM = _env("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(_env("JWT_EXPIRE_MINUTES", "10080"))  # 7 days
