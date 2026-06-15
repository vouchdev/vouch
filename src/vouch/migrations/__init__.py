"""KB migrations.

Two layers live here, by design:

* **Legacy integer "format" migrations** (`_legacy`) — the ``config.yaml``
  ``version`` / ``KB_FORMAT_VERSION`` directory-layout bump, reached by
  ``vouch migrate`` with no subcommand. Preserved verbatim.
* **Semver model-schema migrations** (`runner` + `manifest` + `rewriter` +
  `journal` + `schema`) — the versioned plan/apply/rollback/verify flow keyed off
  ``.vouch/schema_version`` and driven by yaml manifests. Reached by
  ``vouch migrate status|plan|apply|rollback|verify``.

Both are re-exported here so importers keep using ``vouch.migrations`` unchanged.
"""

from __future__ import annotations

from ._legacy import (
    MigrationError,
    MigrationPlan,
    MigrationResult,
    MigrationStep,
    build_plan,
    detect_version,
    migrate,
    read_config,
    write_config,
)
from .manifest import (
    Manifest,
    ManifestError,
    default_manifests_dir,
    load_manifests,
    parse_manifest,
)
from .runner import (
    SchemaPlan,
    SchemaPlanStep,
    build_schema_plan,
)
from .runner import apply as schema_apply
from .runner import plan as schema_plan
from .runner import rollback as schema_rollback
from .runner import status as schema_status
from .runner import verify as schema_verify
from .schema import (
    BASELINE_SCHEMA_VERSION,
    read_schema_version,
    write_schema_version,
)

__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "Manifest",
    "ManifestError",
    "MigrationError",
    "MigrationPlan",
    "MigrationResult",
    "MigrationStep",
    "SchemaPlan",
    "SchemaPlanStep",
    "build_plan",
    "build_schema_plan",
    "default_manifests_dir",
    "detect_version",
    "load_manifests",
    "migrate",
    "parse_manifest",
    "read_config",
    "read_schema_version",
    "schema_apply",
    "schema_plan",
    "schema_rollback",
    "schema_status",
    "schema_verify",
    "write_config",
    "write_schema_version",
]
