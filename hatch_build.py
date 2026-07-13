"""Bundle the built React console (webapp/dist) into the wheel.

When webapp/dist has been built, ship it as ``vouch/web/console`` so
``pip install 'vouch-kb[web]'`` + ``vouch console`` serves it with no node.
When it has NOT been built (a fresh checkout, or the sdist -> wheel path where
the gitignored dist isn't present), skip it silently: the wheel still builds,
and ``vouch console`` reports the missing console cleanly. This is why the
include is a hook rather than a static ``force-include``, which hard-errors on
a missing source path.
"""

from __future__ import annotations

import os
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ConsoleBundleHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        dist = os.path.join(self.root, "webapp", "dist")
        if not os.path.isfile(os.path.join(dist, "index.html")):
            return  # not built — degrade gracefully rather than fail the build
        build_data.setdefault("force_include", {})[dist] = "vouch/web/console"
