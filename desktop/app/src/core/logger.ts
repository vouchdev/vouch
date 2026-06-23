/** Minimal namespaced logger for shell diagnostics. */

type Level = "debug" | "info" | "warn" | "error";

function emit(level: Level, scope: string, message: string, extra?: unknown): void {
  const prefix = `[vouch-desktop:${scope}]`;
  const payload = extra === undefined ? message : `${message} ${JSON.stringify(extra)}`;
  switch (level) {
    case "debug":
      console.debug(prefix, payload);
      break;
    case "info":
      console.info(prefix, payload);
      break;
    case "warn":
      console.warn(prefix, payload);
      break;
    case "error":
      console.error(prefix, payload);
      break;
  }
}

export function createLogger(scope: string) {
  return {
    debug: (message: string, extra?: unknown) => emit("debug", scope, message, extra),
    info: (message: string, extra?: unknown) => emit("info", scope, message, extra),
    warn: (message: string, extra?: unknown) => emit("warn", scope, message, extra),
    error: (message: string, extra?: unknown) => emit("error", scope, message, extra),
  };
}
