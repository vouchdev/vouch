"""Flatpak packaging validation helpers (#211).

Pure-stdlib checks used by tests and ``scripts/validate-manifest.py``. Keeps
the manifest, desktop entry, metainfo, and icon tree aligned with Flathub
expectations without requiring flatpak-builder at pytest time.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

FLATPAK_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = FLATPAK_DIR.parents[1]

MANIFEST_NAME = "com.vouchdev.vouch.yaml"
APP_ID = "com.vouchdev.vouch"
RUNTIME = "org.freedesktop.Platform"
RUNTIME_VERSION = "23.08"
COMMAND = "vouch-review-ui"

REQUIRED_FINISH_ARGS = frozenset(
    {
        "--share=network",
        "--filesystem=home",
    }
)

RECOMMENDED_FINISH_ARGS = frozenset(
    {
        "--talk-name=org.freedesktop.portal.Desktop",
        "--socket=wayland",
        "--socket=fallback-x11",
    }
)

ICON_SIZES = (16, 32, 48, 64, 128, 256, 512)

DESKTOP_REQUIRED_KEYS = frozenset(
    {
        "Type",
        "Name",
        "Exec",
        "Icon",
    }
)


@dataclass
class ValidationIssue:
    level: str  # error | warning | info
    message: str
    path: str | None = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]

    def ok(self) -> bool:
        return not self.errors

    def add(self, level: str, message: str, path: str | None = None) -> None:
        self.issues.append(ValidationIssue(level=level, message=message, path=path))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_simple_yaml_list(block: str, key: str) -> list[str]:
    """Minimal YAML list parser for flatpak manifest keys (no PyYAML dep)."""
    lines = block.splitlines()
    in_key = False
    items: list[str] = []
    for line in lines:
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            in_key = True
            continue
        if in_key:
            m = re.match(r"^\s+-\s+(.+)$", line)
            if m:
                items.append(m.group(1).strip())
                continue
            if line and not line.startswith(" "):
                break
    return items


def _manifest_scalar(block: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", block, re.MULTILINE)
    return m.group(1).strip().strip("'\"") if m else None


def validate_manifest_exists(report: ValidationReport, root: Path = FLATPAK_DIR) -> Path:
    path = root / MANIFEST_NAME
    if not path.is_file():
        report.add("error", f"missing manifest {MANIFEST_NAME}", str(path))
        return path
    report.add("info", "manifest present", str(path))
    return path


def validate_manifest_content(report: ValidationReport, root: Path = FLATPAK_DIR) -> None:
    path = root / MANIFEST_NAME
    if not path.is_file():
        return
    text = _read_text(path)

    app_id = _manifest_scalar(text, "id")
    if app_id != APP_ID:
        report.add("error", f"id must be {APP_ID!r}, got {app_id!r}", str(path))

    runtime = _manifest_scalar(text, "runtime")
    if runtime != RUNTIME:
        report.add("error", f"runtime must be {RUNTIME!r}, got {runtime!r}", str(path))

    runtime_version = _manifest_scalar(text, "runtime-version")
    if runtime_version != RUNTIME_VERSION:
        report.add(
            "error",
            f"runtime-version must be {RUNTIME_VERSION!r}, got {runtime_version!r}",
            str(path),
        )

    command = _manifest_scalar(text, "command")
    if command != COMMAND:
        report.add("error", f"command must be {COMMAND!r}, got {command!r}", str(path))

    finish_args = set(_parse_simple_yaml_list(text, "finish-args"))
    missing_required = REQUIRED_FINISH_ARGS - finish_args
    for arg in sorted(missing_required):
        report.add("error", f"finish-args missing required {arg}", str(path))

    missing_recommended = RECOMMENDED_FINISH_ARGS - finish_args
    for arg in sorted(missing_recommended):
        report.add("warning", f"finish-args missing recommended {arg}", str(path))

    if "org.freedesktop.Sdk.Extension.python3" in text:
        report.add(
            "error",
            "do not use Sdk.Extension.python3 — python3 ships in org.freedesktop.Sdk",
            str(path),
        )

    if "/usr/lib/sdk/python3" in text:
        report.add(
            "error",
            "do not append-path /usr/lib/sdk/python3 — use the SDK's built-in pip3",
            str(path),
        )

    if "exclude:" in text and "type: dir" in text:
        report.add(
            "warning",
            "dir sources do not support exclude on older flatpak-builder; "
            "use a git source or staging module instead",
            str(path),
        )

    if "name: vouch" not in text:
        report.add("error", "modules must include vouch python module", str(path))

    if "name: vouch-desktop" not in text:
        report.add("error", "modules must include vouch-desktop assets module", str(path))

    if ".[web]" not in text:
        report.add("error", "pip install must include [web] extra for review-ui", str(path))


def validate_desktop_entry(report: ValidationReport, root: Path = FLATPAK_DIR) -> None:
    path = root / f"{APP_ID}.desktop"
    if not path.is_file():
        report.add("error", "missing .desktop launcher", str(path))
        return

    text = _read_text(path)
    keys: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("[") or not line.strip() or line.strip().startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            keys[k.strip()] = v.strip()

    for req in DESKTOP_REQUIRED_KEYS:
        if req not in keys:
            report.add("error", f".desktop missing [{req}]", str(path))

    if keys.get("Icon") != APP_ID:
        report.add("error", f".desktop Icon must be {APP_ID}", str(path))

    if keys.get("Exec", "").split()[0] != COMMAND:
        report.add("error", f".desktop Exec must start with {COMMAND}", str(path))


def validate_metainfo(report: ValidationReport, root: Path = FLATPAK_DIR) -> None:
    path = root / f"{APP_ID}.metainfo.xml"
    if not path.is_file():
        report.add("error", "missing AppStream metainfo", str(path))
        return

    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        report.add("error", f"metainfo XML parse error: {e}", str(path))
        return

    root_el = tree.getroot()
    component_id = root_el.findtext("id")
    if component_id != APP_ID:
        report.add("error", f"<id> must be {APP_ID}, got {component_id!r}", str(path))

    launchable = root_el.find("launchable")
    if launchable is None or launchable.get("type") != "desktop-id":
        report.add("error", "<launchable type=desktop-id> required", str(path))
    elif launchable.text != f"{APP_ID}.desktop":
        report.add("error", "launchable desktop-id must match .desktop file", str(path))

    if root_el.find("name") is None:
        report.add("error", "<name> required for Flathub", str(path))

    if root_el.find("summary") is None:
        report.add("error", "<summary> required for Flathub", str(path))

    releases = root_el.findall("releases/release")
    if not releases:
        report.add("warning", "no <releases> — Flathub needs versioned releases", str(path))
    else:
        for rel in releases:
            if not rel.get("version"):
                report.add("error", "<release> missing version attribute", str(path))
            if not rel.get("date"):
                report.add("warning", f"release {rel.get('version')} missing date", str(path))

    screenshots = root_el.findall("screenshots/screenshot")
    if not screenshots:
        report.add("warning", "no screenshots — Flathub listing needs at least one", str(path))


def validate_icons(report: ValidationReport, root: Path = FLATPAK_DIR) -> None:
    scalable = (
        root / "share/icons/hicolor/scalable/apps" / f"{APP_ID}.svg"
    )
    if scalable.is_file():
        report.add("info", "scalable SVG icon present", str(scalable))
    else:
        report.add("warning", "no scalable SVG icon", str(scalable))

    for size in ICON_SIZES:
        png = (
            root
            / "share/icons/hicolor"
            / f"{size}x{size}"
            / "apps"
            / f"{APP_ID}.png"
        )
        if not png.is_file():
            report.add("error", f"missing {size}x{size} PNG icon", str(png))
        elif png.stat().st_size < 64:
            report.add("error", f"{size}x{size} PNG looks truncated", str(png))


def validate_launcher_script(report: ValidationReport, root: Path = FLATPAK_DIR) -> None:
    path = root / "vouch-review-ui.sh"
    if not path.is_file():
        report.add("error", "missing vouch-review-ui.sh launcher", str(path))
        return
    text = _read_text(path)
    if "vouch review-ui" not in text:
        report.add("error", "launcher must invoke vouch review-ui", str(path))
    if not text.startswith("#!/"):
        report.add("warning", "launcher should have a shebang", str(path))


def validate_requirements_pin(report: ValidationReport, root: Path = FLATPAK_DIR) -> None:
    path = root / "requirements-flatpak.txt"
    if not path.is_file():
        report.add("warning", "requirements-flatpak.txt missing", str(path))
        return
    text = _read_text(path)
    for dep in ("fastapi", "uvicorn", "pydantic", "mcp"):
        if dep not in text:
            report.add("error", f"requirements-flatpak.txt must pin {dep}", str(path))


def run_all_validations(root: Path = FLATPAK_DIR) -> ValidationReport:
    report = ValidationReport()
    validate_manifest_exists(report, root)
    validate_manifest_content(report, root)
    validate_desktop_entry(report, root)
    validate_metainfo(report, root)
    validate_icons(report, root)
    validate_launcher_script(report, root)
    validate_requirements_pin(report, root)
    return report
