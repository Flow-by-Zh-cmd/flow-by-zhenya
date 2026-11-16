"""Точка входа для хостингов, которые ищут main.py (то же, что `python bot.py`)."""
import asyncio

from bot import main

if __name__ == "__main__":
    asyncio.run(main())
