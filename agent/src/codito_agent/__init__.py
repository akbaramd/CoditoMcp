"""Codito Windows agent.

The import surface intentionally has no GUI or Windows-native side effects so the
protocol and filesystem core can be tested without PySide6 or a running broker.
"""

from .errors import AgentError
from .models import Project, ProjectMode

__all__ = ["AgentError", "Project", "ProjectMode"]
__version__ = "0.3.1"
