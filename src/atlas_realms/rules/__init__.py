"""Pure game rules: no database, no clock, no I/O.

Everything here is a function of its inputs plus a seeded dice stream, so the tick
engine can be replayed in tests and anyone can re-run a battle from its log.
"""
