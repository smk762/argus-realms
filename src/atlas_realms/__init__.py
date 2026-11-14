"""Atlas Realms: a tick-based territory game on maps painted by argus-atlas."""

try:
    from atlas_realms._version import __version__
except ImportError:  # pragma: no cover - only before the first build
    __version__ = "0.0.0"
