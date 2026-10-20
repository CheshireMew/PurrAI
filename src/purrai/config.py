from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PURRAI_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./data/purrai.db"
    bot_token: str = ""
    superadmin_ids: list[int] = []
    public_url: str = "http://127.0.0.1:8765"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    session_secret: str = ""
    cookie_secure: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    proxy_url: str = ""
    chat_api_base: str = "https://api.deepseek.com"
    chat_api_key: str = Field(default="", repr=False)
    chat_model: str = "deepseek-flash"
    chat_timeout: int = Field(default=30, ge=5, le=120)
    chat_max_tokens: int = Field(default=256, ge=100, le=4096)
    chat_api_extra: dict = Field(default_factory=dict)
    chat_daily_calls: int = Field(default=500, ge=1, le=100000)
    chat_group_daily_calls: int = Field(default=200, ge=1, le=100000)
    chat_user_daily_calls: int = Field(default=60, ge=1, le=100000)
    chat_daily_tokens: int = Field(default=1000000, ge=1000)
    chat_group_daily_tokens: int = Field(default=400000, ge=1000)
    chat_user_daily_tokens: int = Field(default=100000, ge=1000)
    chat_user_interval: float = Field(default=5, ge=0, le=3600)
    chat_group_interval: float = Field(default=2, ge=0, le=3600)
    pending_group_limit: int = Field(default=50, ge=1, le=1000)
    archive_max_db_mb: int = Field(default=512, ge=16)
    archive_max_revisions: int = Field(default=20, ge=1, le=100)
    backup_dir: Path = Path("data/backups")
    frontend_dist: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"

    @property
    def login_ready(self) -> bool:
        return bool(self.oidc_client_id and self.oidc_client_secret and len(self.session_secret) >= 32)

    @field_validator("chat_api_base", "chat_api_key", "chat_model")
    @classmethod
    def trim_chat_configuration(cls, value):
        return value.strip()

    @field_validator("chat_api_base")
    @classmethod
    def validate_chat_url(cls, value):
        from urllib.parse import urlsplit

        if value:
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment:
                raise ValueError("聊天 API 地址需要是完整的 HTTP(S) 地址，不含查询参数和片段")
        return value.rstrip("/")

    @property
    def chat_ready(self) -> bool:
        return bool(self.chat_api_base and self.chat_api_key and self.chat_model)
