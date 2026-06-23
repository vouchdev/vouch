import type { HealthzResponse } from "../core/types";
import { HEALTH_POLL_INTERVAL_MS, SIDECAR_STARTUP_TIMEOUT_MS } from "../core/constants";
import { createLogger } from "../core/logger";

const log = createLogger("health");

export async function pollHealthz(
  baseUrl: string,
  expectedRoot?: string,
  timeoutMs: number = SIDECAR_STARTUP_TIMEOUT_MS,
): Promise<HealthzResponse> {
  const deadline = Date.now() + timeoutMs;
  let lastError = "sidecar did not become healthy";
  const healthUrl = `${baseUrl.replace(/\/$/, "")}/healthz`;

  while (Date.now() < deadline) {
    try {
      const resp = await fetch(healthUrl, { method: "GET" });
      if (!resp.ok) {
        lastError = `healthz HTTP ${resp.status}`;
      } else {
        const body = (await resp.json()) as HealthzResponse;
        if (!body.ok) {
          lastError = "healthz ok=false";
        } else if (expectedRoot && normalize(body.kb) !== normalize(expectedRoot)) {
          lastError = `kb mismatch: ${body.kb} != ${expectedRoot}`;
        } else {
          return body;
        }
      }
    } catch (err) {
      lastError = String(err);
    }
    await sleep(HEALTH_POLL_INTERVAL_MS);
  }
  log.error("health poll timed out", { lastError, healthUrl });
  throw new Error(lastError);
}

function normalize(path: string): string {
  return path.replace(/\\/g, "/").toLowerCase();
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
