import { createLogger } from "../core/logger";
import { pollHealthz } from "../sidecar/HealthPoller";
import { defaultBaseUrl } from "../sidecar/SidecarConfig";

const log = createLogger("sidecar-verify");

export async function verifySidecarReady(
  baseUrl: string = defaultBaseUrl(),
  expectedRoot?: string,
): Promise<boolean> {
  try {
    await pollHealthz(baseUrl, expectedRoot, 5_000);
    return true;
  } catch (err) {
    log.warn("sidecar not ready", err);
    return false;
  }
}
