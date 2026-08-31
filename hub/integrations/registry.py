"""Central plugin registry — single access point for all integrations.

All fields default to noop implementations so Hub always starts cleanly.
Concrete implementations are registered in app.py lifespan based on config.
"""

from __future__ import annotations

from hub.integrations.noop import (
    NoopDispatch,
    NoopForge,
    NoopGitHub,
    NoopGitOps,
    NoopNotes,
    NoopTranscripts,
    NoopVast,
)
from hub.integrations.protocols import (
    DispatchPlugin,
    ForgePlugin,
    GitHubPlugin,
    GitOpsPlugin,
    NotesPlugin,
    TranscriptsPlugin,
    VastPlugin,
)


class PluginRegistry:
    dispatch: DispatchPlugin
    git_ops: GitOpsPlugin
    forge: ForgePlugin
    github: GitHubPlugin
    notes: NotesPlugin
    vast: VastPlugin
    transcripts: TranscriptsPlugin

    def __init__(self) -> None:
        self.dispatch = NoopDispatch()
        self.git_ops = NoopGitOps()
        self.forge = NoopForge()
        self.github = NoopGitHub()
        self.notes = NoopNotes()
        self.vast = NoopVast()
        self.transcripts = NoopTranscripts()


plugins = PluginRegistry()
