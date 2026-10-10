import { TranscriptEntry } from "@/lib/api";

export function CallTranscript({ entries }: { entries: TranscriptEntry[] }) {
  return (
    <div className="transcript">
      {entries.map((t) => (
        <div key={t.id} className="transcript-turn">
          <div className="transcript-turn-no">
            Turn {t.turn_number}
            {t.interrupted && <span className="badge amber">interrupted</span>}
          </div>
          <div className="transcript-line caller">
            <span className="transcript-who">Caller</span>
            <span className={`transcript-text${t.caller_text ? "" : " empty"}`}>
              {t.caller_text || "(nothing heard)"}
            </span>
          </div>
          <div className="transcript-line agent">
            <span className="transcript-who">Agent</span>
            <span className={`transcript-text${t.ai_response ? "" : " empty"}`}>
              {t.ai_response || "(no reply)"}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
