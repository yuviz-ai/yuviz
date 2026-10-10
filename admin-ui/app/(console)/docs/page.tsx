"use client";

// Configuration guide for tenant admins. Static (no fetches) so it still renders when the API is down.
// Keep in step with the agent wizard, APIs tab and knowledge base forms.

import { useState } from "react";

type SectionId =
  | "start" | "agents" | "knowledge" | "apis" | "chained"
  | "flows" | "voice" | "other" | "test" | "trouble";

const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "start", label: "How it fits together" },
  { id: "agents", label: "Agents" },
  { id: "knowledge", label: "Knowledge bases" },
  { id: "apis", label: "APIs" },
  { id: "chained", label: "Dependent APIs" },
  { id: "flows", label: "IVR-Call" },
  { id: "voice", label: "AI & Voice" },
  { id: "other", label: "Other pages" },
  { id: "test", label: "Testing an agent" },
  { id: "trouble", label: "When something is wrong" },
];

/** A field row: what the form calls it, what it does, and what goes wrong. */
function Field({
  name, required, children, wrong,
}: {
  name: string; required?: boolean; children: React.ReactNode; wrong?: string;
}) {
  return (
    <div style={{ padding: "12px 0", borderTop: "1px solid var(--border)" }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontWeight: 600, fontSize: ".92rem" }}>{name}</span>
        {required && (
          <span className="badge" style={{ fontSize: ".62rem" }}>required</span>
        )}
      </div>
      <div style={{ fontSize: ".88rem", color: "var(--text-2)", marginTop: 4, maxWidth: "68ch" }}>
        {children}
      </div>
      {wrong && (
        <div style={{ fontSize: ".82rem", color: "var(--text-3)", marginTop: 6, maxWidth: "68ch" }}>
          <strong style={{ color: "var(--text-2)" }}>If it&apos;s wrong: </strong>{wrong}
        </div>
      )}
    </div>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        borderLeft: "3px solid var(--cyan)", background: "var(--surf-2)",
        padding: "11px 14px", margin: "14px 0", fontSize: ".88rem",
        color: "var(--text-2)", maxWidth: "72ch",
      }}
    >
      {children}
    </div>
  );
}

function H3({ children }: { children: React.ReactNode }) {
  return (
    <h3 style={{ fontSize: "1.02rem", fontWeight: 600, margin: "26px 0 6px" }}>{children}</h3>
  );
}

function P({ children }: { children: React.ReactNode }) {
  return (
    <p style={{ fontSize: ".92rem", color: "var(--text-2)", margin: "0 0 12px", maxWidth: "70ch" }}>
      {children}
    </p>
  );
}

export default function DocsPage() {
  const [section, setSection] = useState<SectionId>("start");

  return (
    <div style={{ maxWidth: 980 }}>
      <div style={{ marginBottom: 18 }}>
        <h1 style={{ fontSize: "1.5rem", fontWeight: 600, letterSpacing: "-.025em", margin: 0, color: "var(--text)" }}>
          Configuration guide
        </h1>
        <div className="form-hint" style={{ marginTop: 4 }}>
          What every field does, and what happens if you get it wrong
        </div>
      </div>

      <div className="tabs" style={{ flexWrap: "wrap" }}>
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            className={`tab${section === s.id ? " active" : ""}`}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      <div className="card">
        <div className="card-body" style={{ paddingBottom: 28 }}>

          {section === "start" && (
            <>
              <H3>The short version</H3>
              <P>
                An agent is the thing that answers the phone. On its own it can only
                talk. You make it useful by giving it two kinds of source, and the
                difference between them is the single most important idea here.
              </P>

              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14, margin: "16px 0" }}>
                <div className="card" style={{ padding: 16 }}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>A knowledge base</div>
                  <div style={{ fontSize: ".88rem", color: "var(--text-2)" }}>
                    Documents you upload. Use it for anything <strong>written down</strong> and
                    slow to change: opening hours, policies, how long things take, what a
                    product is and who it suits.
                  </div>
                </div>
                <div className="card" style={{ padding: 16 }}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>An API</div>
                  <div style={{ fontSize: ".88rem", color: "var(--text-2)" }}>
                    A call to one of your own systems. Use it for anything <strong>live</strong>:
                    a price, a stock level, an order, a specific booking — anything that
                    is different for each caller or each day.
                  </div>
                </div>
              </div>

              <Note>
                <strong>Never put the same fact in both.</strong> If a price is in a
                document <em>and</em> behind an API, the two will disagree the moment
                one changes — and the agent will confidently read out the stale one.
                Prices, stock and anything per-caller belong in an API only.
              </Note>

              <H3>The order to set things up</H3>
              <P>
                Each step depends on the one before it, so it is worth doing them in
                this order:
              </P>
              <ol style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 6 }}><strong>AI &amp; Voice</strong> — add the speech, language and voice providers the agent will use.</li>
                <li style={{ marginBottom: 6 }}><strong>Knowledge Base</strong> — upload your documents, and register any APIs on the APIs tab.</li>
                <li style={{ marginBottom: 6 }}><strong>Agent Studio</strong> — create the agent and attach the knowledge bases and APIs it should have.</li>
                <li style={{ marginBottom: 6 }}><strong>Test</strong> — call it from the browser before it ever takes a real call.</li>
              </ol>
            </>
          )}

          {section === "agents" && (
            <>
              <P>
                <strong>Agent Studio → New agent.</strong> The wizard has six steps.
                Everything is editable afterwards, so it is fine to move through it
                quickly and refine later.
              </P>

              <H3>Identity</H3>
              <Field name="Name" required>
                What you call this agent in the console. Callers never hear it.
              </Field>
              <Field name="Slug" required
                wrong="Changing it later breaks the test page URL and anything referencing the agent. Pick it once.">
                The short id used in URLs — lowercase, no spaces. Generated from the
                name, and you can edit it.
              </Field>
              <Field name="Greeting"
                wrong="Left empty, the agent waits for the caller to speak first, which reads as a dead line.">
                The first thing the caller hears, spoken before they say anything.
                Keep it to one sentence: who they have reached and an offer to help.
              </Field>
              <Field name="System Prompt" required
                wrong="This is the most common cause of an agent ignoring its tools. If the prompt describes what the knowledge base contains and that description is stale, the agent will not bother looking things up.">
                The agent&apos;s standing instructions: who it is, how it should sound,
                and — importantly — where its answers come from. Describe the split
                between what is written down and what is live, but <strong>do not name
                individual APIs</strong>; each API&apos;s own description handles that, and a
                prompt that names tools competes with those descriptions.
              </Field>

              <H3>Language &amp; Voice</H3>
              <Field name="STT" required
                wrong="A mismatched language model mis-hears names and numbers, which then reach your APIs as wrong arguments.">
                Speech to text — how the caller&apos;s words are transcribed. Must match
                the language they will actually speak.
              </Field>
              <Field name="LLM" required>
                The model that decides what to say and which tool to use. A stronger
                model picks the right API more reliably; a smaller one is cheaper and
                faster.
              </Field>
              <Field name="Voice" required
                wrong="A local voice sounds noticeably synthetic. If the agent sounds robotic, this is the field, not the prompt.">
                Text to speech — what the caller hears.
              </Field>
              <Field name="Tone">
                A short style instruction folded into the prompt, e.g. warm, brisk,
                formal.
              </Field>

              <H3>Limits</H3>
              <Field name="Max call duration"
                wrong="Too short and real conversations are cut off mid-sentence.">
                A hard ceiling on one call. The call ends when it is reached.
              </Field>
              <Field name="Goodbye grace">
                How long the agent waits after saying goodbye before hanging up, so a
                caller who adds &ldquo;oh, one more thing&rdquo; is not cut off.
              </Field>

              <H3>Advanced</H3>
              <Field name="Transfer Type"
                wrong="Set to anything other than none without a destination, and a caller asking for a human reaches nothing.">
                Whether the agent can hand the call to a person, and how. Leave as
                <code> none</code> if there is nobody to transfer to.
              </Field>
              <Field name="Caller ID policy">
                Which number the person receiving a transfer sees — the original
                caller&apos;s, or yours.
              </Field>

              <H3>Knowledge &amp; Tools</H3>
              <Field name="Knowledge Bases">
                Tick the ones this agent may search. Nothing ticked means the agent
                cannot look anything up, even if your account has documents.
              </Field>
              <Field name="Custom APIs"
                wrong="An API left unticked is invisible to the agent. There is no error — the agent simply behaves as though that capability does not exist.">
                Tick the APIs this agent may call.
              </Field>

              <Note>
                <strong>Attachment is per agent, and silent.</strong> Your account can
                own a dozen knowledge bases and fifty APIs; an agent only ever sees
                what is ticked here. If an agent will not do something it should, check
                these two lists first — before rewriting the prompt.
              </Note>
            </>
          )}

          {section === "knowledge" && (
            <>
              <P>
                <strong>Knowledge Base → Sources.</strong> Documents the agent can
                search while a call is in progress.
              </P>

              <H3>Creating one</H3>
              <Field name="Name" required>
                How you refer to it. Group documents that belong together — one per
                subject area is usually right.
              </Field>
              <Field name="Embedding model" required
                wrong="Without one, uploads fail during processing and the document never becomes searchable. The error will say so.">
                How documents are indexed for searching. Set it once, before
                uploading. Changing it later means re-uploading everything, because
                existing documents were indexed with the old one.
              </Field>

              <H3>Uploading a document</H3>
              <P>
                Markdown, plain text and PDF. After upload the document is processed
                in the background: text is extracted, split into passages and indexed.
                It is only searchable once its status reaches <strong>ready</strong>.
              </P>
              <Field name="Status">
                <strong>pending</strong> — queued. <strong>ready</strong> — searchable.{" "}
                <strong>failed</strong> — something went wrong, and the row tells you what.
                A very small file is stored whole rather than split, which is normal.
              </Field>

              <H3>Writing documents that work</H3>
              <ul style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 7 }}>
                  <strong>Answer questions, don&apos;t describe topics.</strong> Callers ask
                  &ldquo;how much notice to cancel?&rdquo; — a heading that says exactly that
                  is found far more reliably than one called &ldquo;Cancellation.&rdquo;
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Keep related facts together.</strong> The agent retrieves
                  passages, not whole documents. A fact separated from its context may
                  arrive without it.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Leave out anything that changes.</strong> Prices, stock and
                  availability belong in an API. A document is the wrong place for a
                  number that moves.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Say what you cannot do.</strong> A section listing what the
                  line does not handle gives the agent something correct to say instead
                  of improvising.
                </li>
              </ul>
            </>
          )}

          {section === "apis" && (
            <>
              <P>
                <strong>Knowledge Base → APIs.</strong> Each row is one call to one of
                your systems. The agent chooses between them using the description you
                write, so that field is doing more work than any other.
              </P>

              <H3>The basics</H3>
              <Field name="Name" required
                wrong="A vague name is harmless — the description does the routing — but it makes the APIs list hard to read.">
                An identifier, e.g. <code>get_order_status</code>. Lowercase with
                underscores.
              </Field>
              <Field name="Description" required
                wrong="A description that names the thing but not the occasion — 'Order lookup API' — leaves the agent guessing, and it will guess wrong.">
                <strong>The most important field on this form.</strong> Write it as an
                instruction to the agent, saying <em>when</em> to use it, not what it is.
                Good: &ldquo;Check where a customer&apos;s order is. Call this whenever the
                caller asks about a delivery or a tracking number. You need their email
                address.&rdquo;
              </Field>
              <Field name="Endpoint URL" required
                wrong="Must be reachable from the internet and use https. A local address is rejected, because the platform will not call into private networks.">
                The address to call. Put variable parts in braces:{" "}
                <code>/orders/{"{order_id}"}</code>.
              </Field>
              <Field name="Method" required>
                GET to look something up, POST to create something.
              </Field>
              <Field name="Side-effecting"
                wrong="Left off for something that creates a booking or a payment, a retry can duplicate it.">
                Turn on if calling this <em>changes</em> something — a booking, an order,
                a payment. The platform then guards against the same action firing
                twice.
              </Field>
              <Field name="Timeout"
                wrong="Left empty, a slow system holds the caller in silence far longer than they will tolerate.">
                How long to wait before giving up. On a phone call, short is kinder
                than thorough — a few seconds.
              </Field>

              <H3>Parameters</H3>
              <P>
                One row per value the call needs. <strong>Where it comes from</strong> is
                the field that matters:
              </P>
              <Field name="From the caller">
                The agent supplies it from what the caller said. Only these become
                things the agent can be asked for — so describe them in the caller&apos;s
                terms (&ldquo;the product they named&rdquo;), not yours.
              </Field>
              <Field name="Fixed value">
                Always the same. Account ids, API versions, constants.
              </Field>
              <Field name="From another API"
                wrong="If the path does not match what the other API actually returns, every call fails. Test the other API first and read its response.">
                Taken from an earlier call&apos;s response — this is how chains are built.
                Pick the API and the path to the value, e.g.{" "}
                <code>$.products[0].id</code>.
              </Field>
              <Field name="Send as">
                Where it goes in the request: the path, the query string, a header, or
                the body. This must match what your system expects.
              </Field>
              <Field name="Sensitive">
                Marks a value that must never appear in logs. Use it for anything
                personal.
              </Field>

              <H3>Chaining APIs together</H3>
              <P>
                Most useful lookups need two steps: find the thing, then fetch its
                detail. You register both, and make the second one take its id{" "}
                <em>from</em> the first. The agent then only ever sees the second —
                it asks for &ldquo;the price of the blue widget&rdquo; and the lookup happens
                automatically.
              </P>
              <Note>
                <strong>An API used by another API disappears from the agent&apos;s
                choices.</strong> That is deliberate: the agent should pick things a
                caller can ask for, not internal steps. If an API vanished from the
                list after you chained it, that is why. Up to four steps can chain
                together.
              </Note>

              <H3>Spoken result</H3>
              <Field name="Success template"
                wrong="Left empty, the agent phrases the result itself — which is where a wrong number can creep in.">
                <strong>The strongest protection you have against a made-up answer.</strong>{" "}
                Write the exact sentence to speak, with values in braces:{" "}
                <code>&ldquo;The {"{$.title}"} is {"{$.price}"} pounds, and we have {"{$.stock}"} in
                stock.&rdquo;</code> The agent then reads it out verbatim rather than
                composing it. Use it for every price, reference number and confirmation.
              </Field>
              <Field name="Hidden response fields">
                Paths in the response the agent must never see. Anything sensitive the
                system returns but the caller should not hear.
              </Field>
            </>
          )}


          {section === "chained" && (
            <>
              <P>
                Most real lookups need two calls: find the thing, then fetch its
                detail. Your systems almost certainly work this way — you cannot ask
                for order <code>#4471</code>&apos;s tracking number without first knowing
                which customer it belongs to.
              </P>
              <P>
                You set this up by registering <strong>each call as its own API</strong>,
                then telling the second one to take a value <em>from</em> the first. The
                agent never sees the intermediate steps.
              </P>

              <H3>Worked example: &ldquo;how much is the blue widget?&rdquo;</H3>
              <P>
                Two calls are needed. A search that turns a name into an id, then a
                detail call that turns the id into a price.
              </P>

              <div className="card" style={{ padding: 16, margin: "14px 0", fontSize: ".88rem" }}>
                <div style={{ fontFamily: "var(--mono, monospace)", fontSize: ".82rem", lineHeight: 1.9 }}>
                  <div><strong>1. search_products</strong> &nbsp;GET /products/search?q=blue+widget</div>
                  <div style={{ color: "var(--text-3)" }}>&nbsp;&nbsp;&nbsp;returns {"{ products: [ { id: 6, … } ] }"}</div>
                  <div style={{ marginTop: 8 }}><strong>2. get_product_details</strong> &nbsp;GET /products/6</div>
                  <div style={{ color: "var(--text-3)" }}>&nbsp;&nbsp;&nbsp;returns {"{ title, price, stock }"}</div>
                </div>
              </div>

              <H3>Step by step</H3>
              <ol style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 10 }}>
                  <strong>Register the first API</strong> (<code>search_products</code>).
                  Give it one parameter, <code>q</code>, <em>from the caller</em>, sent in
                  the query string. Describe it as an internal step — it will be hidden
                  from the agent shortly.
                </li>
                <li style={{ marginBottom: 10 }}>
                  <strong>Call it once yourself</strong> and look at the response. You
                  need to know the exact path to the value the next call requires. In
                  the example above that is <code>$.products[0].id</code> — &ldquo;the id of
                  the first product in the list&rdquo;.
                </li>
                <li style={{ marginBottom: 10 }}>
                  <strong>Register the second API</strong> (<code>get_product_details</code>),
                  with the variable part in braces in the URL:{" "}
                  <code>/products/{"{product_id}"}</code>.
                </li>
                <li style={{ marginBottom: 10 }}>
                  <strong>Add its parameter as <em>from another API</em></strong>. Name it
                  to match the brace — <code>product_id</code> — pick{" "}
                  <code>search_products</code> as the source, and paste the path you
                  found in step 2. Send it as <strong>path</strong>.
                </li>
                <li style={{ marginBottom: 10 }}>
                  <strong>Write the second API&apos;s description for the caller&apos;s
                  question</strong>, not the mechanism: &ldquo;Get a product&apos;s price and
                  stock. Call this whenever the caller asks what something costs. You
                  only need the product name they said.&rdquo;
                </li>
                <li style={{ marginBottom: 10 }}>
                  <strong>Tick both on the agent.</strong> Both must be enabled — the
                  hidden one still has to run.
                </li>
              </ol>

              <Note>
                <strong>The first API now disappears from the agent&apos;s choices.</strong>{" "}
                That is correct and automatic: an API used by another API becomes an
                internal step. The agent is offered only{" "}
                <code>get_product_details</code>, and asks for it with the words the
                caller used. If you were looking for a chained API in the agent&apos;s list
                and could not find it, this is why.
              </Note>

              <H3>Where the caller&apos;s words go</H3>
              <P>
                Notice that <code>q</code> belongs to the <em>first</em> API, but the agent
                only ever calls the second. The platform handles this: it gathers the
                caller-supplied parameters from every step of the chain and presents
                them on the API the agent can actually see. You do not need to
                duplicate anything.
              </P>

              <H3>Rules and limits</H3>
              <ul style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 7 }}>
                  <strong>Up to four steps</strong> can chain together.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Steps run in order, never at the same time</strong>, so a long
                  chain costs the caller real silence. Keep each timeout short.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Loops are rejected</strong> when you save. A cannot depend on B
                  if B already depends on A.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>A value the agent invents cannot be used.</strong> Because{" "}
                  <code>product_id</code> comes from another API, anything the agent
                  made up for it is simply never read. This is the strongest reason to
                  chain rather than ask the agent for an id.
                </li>
              </ul>

              <H3>When it does not work</H3>
              <Field name="Nothing was found">
                The first call ran and matched nothing — the caller asked for something
                you do not have. The agent is told to say so plainly and ask for a
                different spelling. Not an error.
              </Field>
              <Field name="The path did not fit"
                wrong="Re-read the first API's actual response. The commonest mistake is assuming a list where the system returns a single object, or vice versa.">
                The path you entered does not match the response shape, so the chain
                cannot continue. A configuration problem — rephrasing by the caller
                will never fix it.
              </Field>
            </>
          )}


          {section === "flows" && (
            <>
              <P>
                <strong>IVR-Call</strong> menus are keypad menus — &ldquo;press 1 for sales&rdquo;.
                They are a separate thing from an agent: a menu does not hold a conversation, it
                plays messages, reads keypresses, and sends the caller on. A menu can hand the call
                to an agent when it is done.
              </P>

              <Note>
                <strong>You do not need a phone menu.</strong> An agent answers perfectly well
                on its own. Add one only when callers should choose from options, key
                in a number, or reach different destinations before anyone speaks to
                them.
              </Note>

              <H3>Creating one</H3>
              <P>
                Start from scratch or copy an existing menu, then choose whether it is for
                inbound calls, outbound, or both. You then get a canvas to build on.
              </P>

              <H3>The steps you can add</H3>
              <Field name="Start" required>
                Every menu begins here. It carries the <strong>voice</strong> used by every
                speaking step in the menu — set it once, here, not per step.
              </Field>
              <Field name="Play message">
                Speaks something and moves on. Use it for a welcome or an announcement.
                Needs a prompt.
              </Field>
              <Field name="Menu"
                wrong="A branch you never wire up means that keypress does nothing and the caller hears silence.">
                Speaks its prompt and waits for a keypress. Add a branch per key you
                accept — 0–9, star, hash. Two extra branches you should always set:{" "}
                <strong>timeout</strong> (they pressed nothing) and <strong>invalid</strong>{" "}
                (they pressed something you do not handle). Defaults: waits 5 seconds,
                retries twice.
              </Field>
              <Field name="Collect digits"
                wrong="Without a variable name the menu will not publish — there is nowhere to put what they typed.">
                Gathers a number — a reference, an account, a date of birth. Needs a{" "}
                <strong>variable name</strong> to store it under. Set the minimum and
                maximum digits (up to 32) and the key that ends entry, normally hash.
                Tick <strong>sensitive</strong> for anything personal: it is then kept out
                of the transcript, the logs, and anything handed to an agent.
              </Field>
              <Field name="Transfer"
                wrong="Without a destination the menu will not publish.">
                Sends the call to a real number. Its prompt, if set, is the
                announcement played before transferring.
              </Field>
              <Field name="Hand to agent"
                wrong="Pick the wrong agent and the caller reaches a bot configured for something else entirely — a common and confusing mistake.">
                Passes the live call to a conversational agent, which takes over from
                that point. Choose which agent.
              </Field>
              <Field name="Hang up">
                Ends the call. Nothing can follow it.
              </Field>

              <H3>What has to be true before it will publish</H3>
              <ul style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 6 }}>Every step that speaks has a prompt.</li>
                <li style={{ marginBottom: 6 }}>Every transfer has a destination; every hand-off has an agent.</li>
                <li style={{ marginBottom: 6 }}>Every collect step has a variable name and sane digit limits.</li>
                <li style={{ marginBottom: 6 }}>Every branch points at a step that exists.</li>
                <li style={{ marginBottom: 6 }}>Nothing leads out of a hang-up.</li>
              </ul>
              <P>
                You can save an unfinished menu as a draft at any point. Publishing is
                what makes it live, and it will refuse until the above holds. Warnings —
                a menu with no timeout branch, for instance — do not block publishing
                but are worth reading.
              </P>

              <H3>Versions</H3>
              <P>
                Every publish is kept. You can look back at earlier versions and roll
                back to one, which republishes it as a <em>new</em> version rather than
                rewinding — so the history stays intact and the rollback is itself
                recorded. A call already in progress finishes on the version it
                started with; publishing never moves a live caller onto a new version.
              </P>

              <H3>Putting a menu in front of an agent</H3>
              <P>
                A menu plays when it is attached to an agent and that agent gets a call.
                Pick the agents on the menu&apos;s &ldquo;Agents behind this menu&rdquo; list.
              </P>
              <Note>
                <strong>An attached menu takes over the whole call.</strong> The caller
                hears the menu, not the agent&apos;s greeting — the agent only speaks if a{" "}
                <em>hand to agent</em> step reaches it. If an agent that used to
                talk suddenly answers with a menu, a phone menu is attached to it.
              </Note>
            </>
          )}

          {section === "voice" && (
            <>
              <P>
                <strong>AI &amp; Voice.</strong> The providers your agents draw on. Set
                these up before creating an agent — the wizard asks you to pick from
                them.
              </P>
              <Field name="Role" required>
                What the provider is for: <strong>STT</strong> (hearing the caller),{" "}
                <strong>LLM</strong> (deciding what to say), <strong>TTS</strong> (speaking),{" "}
                <strong>Embedding</strong> (indexing knowledge bases for search).
              </Field>
              <Field name="Engine" required>
                Which service. Some run locally — no key needed, no per-call cost, but
                noticeably more synthetic on the voice side. Cloud engines sound better
                and need a key.
              </Field>
              <Field name="Model / Voice">
                The specific model or voice within that engine. For a voice you can
                preview it here before assigning it to anyone.
              </Field>
              <Field name="API key"
                wrong="A wrong or expired key fails at call time, not when you save it. Preview a voice to check it works.">
                Stored encrypted. It is never shown again after saving and never leaves
                the server.
              </Field>
              <Note>
                If your agent sounds robotic, change the <strong>TTS</strong> provider.
                No amount of prompt wording fixes a synthetic voice, and a cloud voice
                is the single biggest improvement most people can make.
              </Note>
            </>
          )}


          {section === "other" && (
            <>
              <P>
                The remaining pages, and when you need them.
              </P>

              <H3>Phone Numbers</H3>
              <P>
                The numbers callers dial, and which agent answers each one. A number
                with no agent assigned rings nowhere. This is the last step before you
                are live — everything else can be built and tested without it.
              </P>

              <H3>Phone Numbers</H3>
              <P>
                How calls physically reach the platform: your carrier or SIP trunk
                details. Usually set up once, by whoever manages your telephony, and
                then left alone. If no calls arrive at all and your numbers look
                correctly assigned, this is where to look.
              </P>

              <H3>Campaigns</H3>
              <P>
                Outbound calling — a list of numbers, an agent to make the calls, and
                when it may dial. Distinct from everything else here, which is about
                answering calls that come to you.
              </P>

              <H3>Calls and Live Calls</H3>
              <P>
                <strong>Calls</strong> is the history: transcripts, how each call ended,
                what the agent did. The single most useful page for understanding why
                an agent behaved oddly — read the transcript rather than guessing.{" "}
                <strong>Live Calls</strong> shows what is happening right now (superadmins only).
              </P>

              <H3>Users</H3>
              <P>
                Who can sign in. Roles decide what they may change: admins configure
                agents, APIs and knowledge bases; other roles can watch calls without
                being able to alter the setup.
              </P>

              <H3>Accounts</H3>
              <P>
                Only visible if you manage more than one organisation. Each account&apos;s
                agents, documents and APIs are completely separate — nothing configured
                in one is ever visible to another.
              </P>

              <H3>Settings</H3>
              <P>
                Your own profile and password, active sessions, and an audit log of
                who changed what. Worth checking the audit log when a setting is not
                what you remember leaving it as.
              </P>
            </>
          )}

          {section === "test" && (
            <>
              <P>
                Open an agent and choose <strong>Test</strong> to call it from your
                browser. You will see the transcript as you speak, and there is a mute
                button.
              </P>
              <H3>What to check before it goes live</H3>
              <ul style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 7 }}>
                  <strong>A document question</strong> — something only your uploaded
                  files could answer.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>A live question</strong> — something only an API could answer.
                  Check the number it says against the real system.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Something you do not offer.</strong> The agent should say so
                  plainly rather than inventing a plausible answer. This is the test
                  most worth doing.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>Something outside its job entirely.</strong> It should decline
                  rather than answer from general knowledge.
                </li>
                <li style={{ marginBottom: 7 }}>
                  <strong>A booking or change, twice.</strong> The second attempt should
                  not silently duplicate the first.
                </li>
              </ul>
            </>
          )}

          {section === "trouble" && (
            <>
              <H3>The agent ignores an API</H3>
              <P>
                Almost always configuration rather than wording, and it fails{" "}
                <em>silently</em> — there is no error, the capability is simply absent.
                Check in this order:
              </P>
              <ol style={{ fontSize: ".92rem", color: "var(--text-2)", maxWidth: "70ch", paddingLeft: "1.2rem" }}>
                <li style={{ marginBottom: 6 }}>Is the API ticked on that agent&apos;s Knowledge &amp; Tools?</li>
                <li style={{ marginBottom: 6 }}>Is it used by another API? Then it is hidden on purpose — ask for the thing that uses it.</li>
                <li style={{ marginBottom: 6 }}>Does its description say <em>when</em> to call it, in the caller&apos;s words?</li>
                <li style={{ marginBottom: 6 }}>Does the system prompt claim answers come from somewhere else? That overrides everything.</li>
              </ol>

              <H3>The agent will not use a document</H3>
              <P>
                Check the document reached <strong>ready</strong>, that its knowledge base
                is ticked on the agent, and that the wording in the document resembles
                how a caller would ask. A document whose headings are internal
                terminology is hard to find.
              </P>

              <H3>It says something that is not true</H3>
              <P>
                Give the API a <strong>success template</strong>. That takes the sentence
                away from the agent entirely for that result. If the invented detail is
                not a number, it is usually a fact living in a document that should
                live in an API.
              </P>

              <H3>It sounds robotic</H3>
              <P>
                Change the TTS provider on AI &amp; Voice. That is the cause, not the
                prompt.
              </P>

              <H3>It answers a menu instead of talking</H3>
              <P>
                A phone menu is attached to the agent. Detach it unless you meant the
                call to start with a keypad menu.
              </P>
            </>
          )}

        </div>
      </div>
    </div>
  );
}
