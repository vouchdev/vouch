/** Typed errors for the desktop shell. */

export class DesktopError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "DesktopError";
    this.code = code;
  }
}

export class SidecarStartupError extends DesktopError {
  constructor(message: string) {
    super("sidecar_startup", message);
    this.name = "SidecarStartupError";
  }
}

export class KbValidationError extends DesktopError {
  readonly selectedPath: string;

  constructor(selectedPath: string, message: string) {
    super("kb_validation", message);
    this.name = "KbValidationError";
    this.selectedPath = selectedPath;
  }
}

export class StatePersistenceError extends DesktopError {
  constructor(message: string) {
    super("state_persistence", message);
    this.name = "StatePersistenceError";
  }
}
