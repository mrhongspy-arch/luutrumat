import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    bank_code: str
    bank_account: str
    bank_account_name: str
    order_prefix: str
    order_timeout_minutes: int
    support_contact: str
    db_path: str
    channel_id: str
    auto_restock_notify: bool
    admin_password: str
    web_port: int
    backup_dir: str
    backup_keep: int
    backup_telegram_hour: int
    webhook_enabled: bool
    webhook_secret: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Thiếu BOT_TOKEN. Sao chép .env.example thành .env và điền token.")
    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        bank_code=os.getenv("BANK_CODE", ""),
        bank_account=os.getenv("BANK_ACCOUNT", ""),
        bank_account_name=os.getenv("BANK_ACCOUNT_NAME", ""),
        order_prefix=os.getenv("ORDER_PREFIX", "DH").upper(),
        order_timeout_minutes=int(os.getenv("ORDER_TIMEOUT_MINUTES", "30")),
        support_contact=os.getenv("SUPPORT_CONTACT", ""),
        db_path=os.getenv("DB_PATH", "shop.db"),
        channel_id=os.getenv("CHANNEL_ID", "").strip(),
        auto_restock_notify=_bool(os.getenv("AUTO_RESTOCK_NOTIFY", "true")),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        web_port=int(os.getenv("WEB_PORT") or os.getenv("WEBHOOK_PORT") or "8080"),
        backup_dir=os.getenv("BACKUP_DIR", "backups"),
        backup_keep=int(os.getenv("BACKUP_KEEP", "72")),
        backup_telegram_hour=int(os.getenv("BACKUP_TELEGRAM_HOUR", "3")),
        webhook_enabled=_bool(os.getenv("WEBHOOK_ENABLED")),
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
    )
