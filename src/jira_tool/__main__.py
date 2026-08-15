"""Entry point for ``python -m jira_tool`` (and ``pythonw -m jira_tool``).

Scheduled tasks invoke the tool this way so a windowless interpreter
(``pythonw.exe`` on Windows) can run it with no console window.
"""

from .cli import main

if __name__ == "__main__":
    main()
