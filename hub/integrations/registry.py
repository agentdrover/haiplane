"""Central plugin registry — single access point for all integrations.

All fields default to noop implementations so Hub always starts cleanly.
Concrete implementations are registered in app.py lifespan based on config.
"""

from __future__ import annotations

from hub.integrations.noop import (
    NoopDispatch,
    NoopGitHub,
    NoopGitOps,
    NoopNotes,
    NoopTranscripts,
    NoopVast,
)
from hub.integrations.protocols import (
    DispatchPlugin,
    GitHubPlugin,
    GitOpsPlugin,
    NotesPlugin,
    TranscriptsPlugin,
    VastPlugin,
)


class PluginRegistry:
    dispatch: DispatchPlugin
    git_ops: GitOpsPlugin
    github: GitHubPlugin
    notes: NotesPlugin
    vast: VastPlugin
    transcripts: TranscriptsPlugin

    def __init__(self) -> None:
        self.dispatch = NoopDispatch()
        self.git_ops = NoopGitOps()
        self.github = NoopGitHub()
        self.notes = NoopNotes()
        self.vast = NoopVast()
        self.transcripts = NoopTranscripts()


plugins = PluginRegistry()
