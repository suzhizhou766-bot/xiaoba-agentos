import type { SaveStatus } from "./storage";

export async function confirmDraftDeparture(
  status: SaveStatus,
  flush: () => Promise<boolean>,
  confirmDiscard: () => boolean,
): Promise<boolean> {
  if (status === "saved") return true;
  let saved = false;
  try {
    saved = await flush();
  } catch {
    // An unexpected storage error must still require a discard decision.
  }
  return saved || confirmDiscard();
}
