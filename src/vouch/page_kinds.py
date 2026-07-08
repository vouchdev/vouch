"""Config-declared page kinds and their frontmatter validation.

The built-in `PageType` enum is closed by design — it pins the kinds vouch
ships with. This module lets a KB *extend* that set from `.vouch/config.yaml`
without forking the model: a contributor can declare a `meeting-notes` or
`decision-record` kind, give it a required-field list and a small frontmatter
schema, and have `kb.propose_page` validate against it.

The validation layer lives here rather than on the `Page` model because the
model has no access to the store (hence no access to the config). Both the
propose gate and the approve gate call `validate_page` so a kind that is added
or tightened after a proposal is filed is still enforced at approval time.

Built-in kinds carry an empty spec (no required fields, no schema), so they
keep working exactly as before.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .models import PageType
from .storage import KBStore

CONFIG_FILENAME = "config.yaml"

# JSON-Schema `type` keyword -> python types it accepts. `bool` is excluded
# from the numeric types on purpose: in python `True` is an int, but a schema
# author asking for an integer never means a boolean. `string` accepts yaml's
# native date/datetime scalars: a bare `due_at: 2026-07-01` loads as a date
# object from CLI --meta parsing and from every frontmatter disk round-trip,
# so rejecting it would make string schemas unusable for date fields (and
# fail re-validation at approve time for pages that validated at propose).
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str, _dt.date, _dt.datetime),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class PageKindError(ValueError):
    """Raised when a page violates its declared kind's schema.

    Carries one message per failing field so callers can surface a per-field
    error rather than a single opaque "invalid page".
    """

    def __init__(self, kind: str, problems: list[str]) -> None:
        self.kind = kind
        self.problems = problems
        joined = "; ".join(problems)
        super().__init__(f"page kind '{kind}': {joined}")


class PageKindSpec(BaseModel):
    """One declared page kind, parsed from `config.yaml: page_kinds.<name>`."""

    extends: str | None = Field(
        default=None,
        description="inherit fields + schema from another kind (one level only)",
    )
    required_fields: list[str] = Field(default_factory=list)
    frontmatter_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON-Schema subset: {type: object, properties: {...}, required: [...]}",
    )
    required_citations: bool = False
    # A protected kind is exempt from the `review.approver_role:
    # trusted-agent` self-approval opt-out: its pages always need a reviewer
    # other than the proposer. Not inherited through `extends` — protection
    # is a property of the concrete kind, declared where a reviewer reads it.
    protected: bool = False
    description: str | None = None

    @field_validator("frontmatter_schema", mode="before")
    @classmethod
    def _parse_inline_schema(cls, v: Any) -> Any:
        # The issue's worked example quotes the schema as an inline YAML/JSON
        # string; a nested mapping is just as valid. Accept both.
        if isinstance(v, str):
            loaded = yaml.safe_load(v)
            return loaded if isinstance(loaded, dict) else {}
        return v


# Built-in kinds resolve to an empty spec — declaring them in config is
# allowed (to add required fields) but never required for them to work.
BUILTIN_PAGE_KINDS: dict[str, PageKindSpec] = {pt.value: PageKindSpec() for pt in PageType}


def _coerce_schema(spec: PageKindSpec) -> dict[str, Any]:
    return spec.frontmatter_schema or {}


class PageKindRegistry:
    """Resolved view over built-in + config-declared page kinds."""

    def __init__(self, specs: dict[str, PageKindSpec]) -> None:
        self._specs = specs

    def known(self) -> set[str]:
        return set(self._specs)

    def is_known(self, name: str) -> bool:
        return name in self._specs

    def is_protected(self, name: str) -> bool:
        spec = self._specs.get(name)
        return bool(spec and spec.protected)

    def resolve(self, name: str) -> tuple[list[str], dict[str, Any], bool]:
        """Return (required_fields, frontmatter_schema, required_citations).

        Applies a single level of `extends`. A parent that itself extends
        another kind raises — issue #234 scopes inheritance to one level.
        """
        spec = self._specs[name]
        required = list(spec.required_fields)
        schema = _coerce_schema(spec)
        citations = spec.required_citations

        if spec.extends is not None:
            parent_name = spec.extends
            if parent_name not in self._specs:
                raise PageKindError(name, [f"extends unknown kind '{parent_name}'"])
            parent = self._specs[parent_name]
            if parent.extends is not None:
                raise PageKindError(
                    name,
                    [f"multi-level inheritance unsupported (parent '{parent_name}' also extends)"],
                )
            required = list(dict.fromkeys([*parent.required_fields, *required]))
            schema = _merge_schema(_coerce_schema(parent), schema)
            citations = citations or parent.required_citations

        return required, schema, citations

    def validate(
        self,
        page_type: str,
        metadata: dict[str, Any],
        *,
        has_citations: bool,
    ) -> None:
        """Raise `PageKindError` if `page_type`/`metadata` violate the kind.

        Collects *all* problems before raising so the caller can report every
        offending field at once.
        """
        if not self.is_known(page_type):
            declared = ", ".join(sorted(self.known()))
            raise PageKindError(page_type, [f"unknown page kind (declared: {declared})"])

        required, schema, requires_citations = self.resolve(page_type)
        problems: list[str] = []

        for field in required:
            if _is_missing(metadata.get(field)):
                problems.append(f"missing required field '{field}'")

        problems.extend(_validate_against_schema(schema, metadata))

        if requires_citations and not has_citations:
            problems.append("requires at least one claim or source citation")

        if problems:
            raise PageKindError(page_type, problems)


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _merge_schema(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge two object schemas; child wins on per-property conflicts."""
    if not parent:
        return child
    if not child:
        return parent
    merged: dict[str, Any] = {"type": "object"}
    props: dict[str, Any] = {}
    props.update(parent.get("properties", {}))
    props.update(child.get("properties", {}))
    merged["properties"] = props
    required = list(dict.fromkeys([*parent.get("required", []), *child.get("required", [])]))
    if required:
        merged["required"] = required
    return merged


def _validate_against_schema(schema: dict[str, Any], data: dict[str, Any]) -> list[str]:
    """Validate `data` against a small JSON-Schema subset.

    Supports `type: object` with `properties` (each a `{type: <jsontype>}`)
    and a top-level `required` list. Unknown keywords are ignored so a richer
    schema degrades gracefully rather than rejecting valid pages.
    """
    if not schema or schema.get("type") != "object":
        return []
    problems: list[str] = []
    for field in schema.get("required", []):
        if _is_missing(data.get(field)):
            problems.append(f"missing required field '{field}'")
    for field, field_schema in schema.get("properties", {}).items():
        if field not in data or data[field] is None:
            continue
        expected = field_schema.get("type") if isinstance(field_schema, dict) else None
        if expected and expected in _JSON_TYPES:
            allowed = _JSON_TYPES[expected]
            value = data[field]
            # `bool` is an `int`; reject it where a number/integer is expected.
            bool_as_number = isinstance(value, bool) and expected in ("number", "integer")
            if bool_as_number or not isinstance(value, allowed):
                problems.append(f"field '{field}' must be {expected}")
    return problems


def load_page_kind_registry(store: KBStore) -> PageKindRegistry:
    """Build a registry from built-in kinds + `config.yaml: page_kinds`."""
    specs = dict(BUILTIN_PAGE_KINDS)
    raw = _read_page_kinds(store)
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise PageKindError(str(name), ["definition must be a mapping"])
        specs[str(name)] = PageKindSpec.model_validate(body)
    return PageKindRegistry(specs)


def _read_page_kinds(store: KBStore) -> dict[str, Any]:
    path = store.kb_dir / CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    kinds = loaded.get("page_kinds")
    return kinds if isinstance(kinds, dict) else {}


def validate_page(
    store: KBStore,
    page_type: str,
    metadata: dict[str, Any] | None,
    *,
    has_citations: bool,
) -> None:
    """Convenience wrapper used by both the propose and approve gates."""
    registry = load_page_kind_registry(store)
    registry.validate(page_type, metadata or {}, has_citations=has_citations)
