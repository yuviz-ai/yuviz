import { CallSentiment } from "@/lib/api";

// One place deciding how each sentiment reads, so the Call Log and the call
// detail page can never drift into using different colours for the same
// label. `frustrated` is amber rather than red on purpose: it flags a call
// worth listening to, not a failure — see schema.sql's calls_sentiment_check
// for why it is tracked separately from `negative`.
const PRESENTATION: Record<CallSentiment, { label: string; tone: string }> = {
  positive:   { label: "Positive",   tone: "green" },
  neutral:    { label: "Neutral",    tone: "gray" },
  negative:   { label: "Negative",   tone: "red" },
  frustrated: { label: "Frustrated", tone: "amber" },
};

export const SENTIMENT_ORDER: CallSentiment[] = ["positive", "neutral", "negative", "frustrated"];

export function sentimentLabel(sentiment: CallSentiment): string {
  return PRESENTATION[sentiment].label;
}

export function SentimentBadge({ sentiment }: { sentiment: CallSentiment | null }) {
  // null is "never scored", not "neutral" — an em dash says that honestly
  // where a grey "Neutral" pill would quietly assert a reading nobody made.
  if (sentiment === null) {
    return (
      <span className="sentiment-unscored" title="Not scored — this call ended before sentiment scoring, had no caller speech, or could not be scored">
        —
      </span>
    );
  }
  const { label, tone } = PRESENTATION[sentiment];
  return (
    <span className={`badge ${tone}`}>
      <span className={`sentiment-dot ${tone}`} aria-hidden="true" />
      {label}
    </span>
  );
}
