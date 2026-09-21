"""
cogs/ticket/__init__.py

Makes `cogs/ticket` loadable as a single extension via:
    await bot.load_extension("cogs.ticket")

Re-exports `setup` from ticket.py so discord.py finds it at the package level
rather than requiring you to load "cogs.ticket.ticket" directly.
"""

from .ticket import setup

__all__ = ["setup"]