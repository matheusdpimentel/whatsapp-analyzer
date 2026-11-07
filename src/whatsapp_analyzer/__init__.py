"""Local analysis of WhatsApp conversation exports."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("whatsapp-conversation-analyzer")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0"
