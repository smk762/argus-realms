"""The tick: one cron step of the game.

`engine.run_tick` is pure (world + orders + tick id + daily secret -> new world + events), so a
tick can be replayed exactly in tests. `store` loads and saves the world; `runner` wraps one tick
in a transaction under an advisory lock.
"""
