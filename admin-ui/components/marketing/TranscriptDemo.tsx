'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageSquare, Clock, User, ShieldAlert, Share2, Layers, CheckCircle2 } from 'lucide-react'

export function TranscriptDemo() {
  const [selectedTurn, setSelectedTurn] = useState(0)

  const transcript = [
    { time: '00:02', speaker: 'Yuviz Agent', text: 'Hello, thank you for calling Yuviz AI support. My name is Maya. How can I help you today?', node: 'Call Started &bull; Greeting Delivered' },
    { time: '00:08', speaker: 'Caller', text: 'Hi Maya, I submitted a refund request yesterday for order #9821. Has it been processed yet?', node: 'Intent Parsed &bull; Order #9821 Identified' },
    { time: '00:14', speaker: 'Yuviz Agent', text: 'Let me look that up for you right now... Yes, your refund of $149 was approved and issued to your original payment method this morning.', node: 'Stripe API Ingestion &bull; Grounded Refund Verification' },
    { time: '00:22', speaker: 'Caller', text: 'Great! How long will it take to show up on my bank statement?', node: 'Follow-up Question &bull; RAG Knowledge Search' },
    { time: '00:28', speaker: 'Yuviz Agent', text: 'It typically takes 3 to 5 business days depending on your bank. I have also sent a receipt to your email.', node: 'Resolution Delivered &bull; Confirmation Email Dispatched' },
  ]

  return (
    <section className="section bg-card/40 border-y border-line" id="transcript-demo">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">
            <span />
            Call Intelligence &amp; Audit Trail
          </p>
          <h2>
            Every conversation <em>leaves a trail.</em>
          </h2>
          <p className="max-w-xl text-muted leading-7">
            Click any line in the transcript to inspect the exact system node, API lookups, and sentiment metrics logged during the call.
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 mt-10 items-center">
          {/* Interactive Transcript List Left */}
          <div className="lg:col-span-7 space-y-3">
            {transcript.map((item, idx) => (
              <div
                key={idx}
                onClick={() => setSelectedTurn(idx)}
                className={`p-4 rounded-xl border transition-all cursor-pointer ${
                  selectedTurn === idx
                    ? 'border-lime bg-card shadow-xs ring-1 ring-lime/20'
                    : 'border-line bg-panel/50 hover:bg-card/60'
                }`}
              >
                <div className="flex items-center justify-between text-xs font-mono mb-1">
                  <span className={item.speaker === 'Yuviz Agent' ? 'text-lime font-bold' : 'text-foreground font-semibold'}>
                    {item.speaker}
                  </span>
                  <span className="text-muted">{item.time}</span>
                </div>
                <p className="text-sm text-foreground/90 leading-relaxed">{item.text}</p>
              </div>
            ))}
          </div>

          {/* System Timeline Inspector Right */}
          <div className="lg:col-span-5 border border-line rounded-2xl bg-panel p-6 shadow-sm min-h-[340px] flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between border-b border-line pb-3 mb-4">
                <span className="text-xs font-mono uppercase text-muted">Node Telemetry Inspector</span>
                <span className="text-xs font-mono text-lime font-semibold">Turn 0{selectedTurn + 1} Selected</span>
              </div>

              <div className="space-y-4">
                <div className="p-3.5 rounded-lg bg-card border border-line">
                  <span className="text-[10px] font-mono uppercase text-muted block mb-1">Executed System Node</span>
                  <p className="text-sm font-mono font-semibold text-foreground">{transcript[selectedTurn].node}</p>
                </div>

                <div className="p-3.5 rounded-lg bg-card border border-line space-y-2 text-xs font-mono">
                  <div className="flex justify-between">
                    <span className="text-muted">Turn Timestamp:</span>
                    <span className="text-foreground">{transcript[selectedTurn].time}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Speech Confidence:</span>
                    <span className="text-lime">99.4%</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Sentiment Score:</span>
                    <span className="text-foreground">0.96 (Positive)</span>
                  </div>
                </div>
              </div>
            </div>

            <div className="border-t border-line pt-4 flex items-center justify-between text-xs font-mono text-muted">
              <span>Audit Trail Secured</span>
              <span className="text-lime">100% Verifiable</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export function Integrations() {
  const integrations = [
    { name: 'Google Calendar', category: 'Scheduling', status: 'Native Sync' },
    { name: 'HubSpot CRM', category: 'Lead Sync', status: 'Native Sync' },
    { name: 'Salesforce', category: 'Enterprise CRM', status: 'API Supported' },
    { name: 'Zendesk', category: 'Support Tickets', status: 'Native Sync' },
    { name: 'Stripe', category: 'Payments Lookup', status: 'API Supported' },
    { name: 'Webhooks', category: 'Custom Events', status: 'Real-Time' },
  ]

  return (
    <section className="section container" id="integrations">
      <div className="section-heading text-center max-w-2xl mx-auto">
        <p className="eyebrow justify-center">
          <span />
          Workflow Ecosystem
        </p>
        <h2>
          Fits into the way your business <em>already works.</em>
        </h2>
        <p className="text-muted leading-7">
          Yuviz connects directly with your existing CRMs, calendar systems, customer support desks, and custom API webhooks.
        </p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mt-10">
        {integrations.map((item) => (
          <div
            key={item.name}
            className="p-5 rounded-2xl border border-line bg-card hover:border-lime hover:shadow-md transition-all text-center group"
          >
            <div className="w-10 h-10 rounded-xl bg-panel border border-line flex items-center justify-center text-lime font-bold text-sm mx-auto mb-3 group-hover:bg-lime group-hover:text-background transition-colors">
              {item.name[0]}
            </div>
            <h4 className="text-sm font-semibold text-foreground tracking-tight">{item.name}</h4>
            <span className="block text-[11px] text-muted mt-1 font-mono">{item.category}</span>
            <span className="inline-block mt-3 text-[10px] font-mono text-lime font-semibold px-2 py-0.5 rounded bg-lime-soft/60">
              {item.status}
            </span>
          </div>
        ))}
      </div>
    </section>
  )
}
