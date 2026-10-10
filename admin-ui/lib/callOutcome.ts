export type Tone = "g" | "a" | "r" | "n";
export type OutcomeKey = "done" | "to_person" | "transfer_failed" | "dropped" | "unrecorded";
export interface Outcome { key: OutcomeKey; label: string; short: string; tone: Tone }

export const OUTCOMES: Record<OutcomeKey, Outcome> = {
  done:            { key: "done", label: "Ended normally", short: "Done", tone: "g" },
  to_person:       { key: "to_person", label: "Sent to a person", short: "To person", tone: "a" },
  transfer_failed: { key: "transfer_failed", label: "Transfer failed", short: "Transfer failed", tone: "r" },
  dropped:         { key: "dropped", label: "Call dropped", short: "Dropped", tone: "r" },
  unrecorded:      { key: "unrecorded", label: "Not recorded", short: "—", tone: "n" },
};

// close_reason is a system code; group it into the few outcomes a business user cares about.
export function outcomeOf(reason: string | null): Outcome {
  switch (reason) {
    case "caller_hangup":
    case "stream_ended":
    case "session_destroyed":
      return OUTCOMES.done;
    case "TRANSFER_SUCCESS":
      return OUTCOMES.to_person;
    case "TRANSFER_FAILED":
    case "TRANSFER_TIMEOUT":
      return OUTCOMES.transfer_failed;
    case "transport_error":
    case "close_timeout":
    case "reconciled_inactive":
    case "reconciled_stale":
    case "reconciled_dead_node":
      return OUTCOMES.dropped;
    default:
      return OUTCOMES.unrecorded;
  }
}
