import re


def create_slug(brand: str, name: str) -> str:
    """Converts 'COSRX', 'Advanced Snail 96 Mucin Power Essence' -> 'cosrx-advanced-snail-96-mucin-power-essence'"""
    raw = f"{brand}-{name}".lower()
    return re.sub(r'[^a-z0-9]+', '-', raw).strip('-')
