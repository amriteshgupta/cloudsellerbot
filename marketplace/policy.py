"""Conservative first-pass moderation; humans must verify resale rights."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

BLOCKED = re.compile(
    r'\b(?:api[\s_-]*(?:keys?|tokens?)|(?:access|bearer|refresh)[\s_-]*tokens?|secret[\s_-]*keys?|passwords?|credentials?|'
    r'accounts?|logins?|non[\s-]*transferable|promo(?:tional)?[\s-]*credits?|'
    r'trial[\s-]*credits?|free[\s-]*credits?)\b|'
    r'\bsk-[A-Za-z0-9_-]{16,}|\bsk_(?:live|test)_[A-Za-z0-9]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|'
    r'\bAIza[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|'
    r'\bAKIA[A-Z0-9]{16}\b|\b\d{7,}:[A-Za-z0-9_-]{25,}\b', re.I)


def validate_text(text):
    if BLOCKED.search(text):
        raise ValueError('Rejected by product policy. Do not submit keys, accounts, credentials, or restricted credits.')


def price_cents(text):
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount <= 0 or amount > 1_000_000 or amount.as_tuple().exponent < -2:
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Enter a positive unit price with at most 2 decimals, up to 1000000.') from None


def buyer_cents(seller_cents):
    return int((Decimal(seller_cents) * Decimal('1.10')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def money(cents, currency):
    amount = f'{cents // 100:,}.{cents % 100:02d}'
    return f'${amount}' if currency == 'USD' else f'{currency} {amount}'
