import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admins: frozenset[int]
    database: str
    currency: str
    broadcast_rate: float

    @classmethod
    def load(cls):
        load_dotenv()
        token = os.getenv('BOT_TOKEN', '')
        if not token or token.startswith('replace_'):
            raise ValueError('Set BOT_TOKEN in .env first.')
        admins = frozenset(int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip())
        if not admins or any(x <= 0 for x in admins):
            raise ValueError('ADMIN_IDS must contain positive Telegram user IDs.')
        rate = float(os.getenv('BROADCAST_PER_SECOND', '10'))
        currency = os.getenv('CURRENCY', 'USD').upper()
        if not 0 < rate <= 20:
            raise ValueError('BROADCAST_PER_SECOND must be >0 and <=20.')
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            raise ValueError('CURRENCY must be a three-letter currency code.')
        return cls(token, admins, os.getenv('DATABASE_PATH', 'data/marketplace.sqlite3'), currency, rate)
