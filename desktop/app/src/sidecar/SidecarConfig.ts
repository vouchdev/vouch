import { DEFAULT_BIND_HOST, DEFAULT_BIND_PORT } from "../core/constants";

export function defaultBaseUrl(): string {
  return `http://${DEFAULT_BIND_HOST}:${DEFAULT_BIND_PORT}`;
}

export function reviewUiArgs(projectRoot: string, port: number = DEFAULT_BIND_PORT): string[] {
  return [
    "review-ui",
    "--bind",
    `${DEFAULT_BIND_HOST}:${port}`,
    "--kb",
    projectRoot,
    "--no-open-browser",
    "--reviewer",
    "desktop-reviewer",
  ];
}

export function windowTitleForLabel(label: string): string {
  return `vouch · ${label}`;
}
