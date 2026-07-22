"""Ingestion connectors.

Connectors are resolved by convention: connector name ``foo`` maps to module
``techradar.ingest.foo`` exposing a ``Connector`` class. Adding a source is a
new module + a ``sources.yaml`` entry — no changes to the runner.
"""

from __future__ import annotations

import importlib

from .base import BaseConnector


def get_connector(name: str) -> type[BaseConnector]:
    module = importlib.import_module(f"techradar.ingest.{name}")
    return module.Connector
