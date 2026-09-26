'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { CheckCircle2, ChevronRight, Filter, Search, PhoneCall, ArrowUpRight } from 'lucide-react'

export function ProductShowcase() {
  const [filter, setFilter] = useState<'all' | 'resolved' | 'escalated'>('all')

  const calls = [
    { id: 'call_901', caller: 'Alex Morgan', time: '10:42 AM', duration: '01:45', agent: 'Maya (Reception)', intent: 'Reschedule Appointment', status: 'resolved', action: 'Google Calendar Updated' },
    { id: 'call_902', caller: 'Jordan Reed', time: '10:38 AM', duration: '02:12', agent: 'Ethan (Sales)', intent: 'Pricing & Enterprise Plan', status: 'resolved', action: 'Demo Booked for Fri 3PM' },
    { id: 'call_903', caller: 'Sarah Jenkins', time: '10:25 AM', duration: '03:04', agent: 'Clara (Support)', intent: 'Custom Integration Help', status: 'escalated', action: 'Transferred to Tech Support' },
    { id: 'call_904', caller: 'David Chen', time: '10:14 AM', duration: '01:18', agent: 'Maya (Reception)', intent: 'Office Hours Inquiry', status: 'resolved', action: 'Info Shared via SMS' },
    { id: 'call_905', caller: 'Elena Rostova', time: '09:55 AM', duration: '02:40', agent: 'Ethan (Sales)', intent: 'Inbound Lead Qualification', status: 'resolved', action: 'HubSpot Contact Created' },
  ]

  const filteredCalls = calls.filter((c) => filter === 'all' || c.status === filter)

  return (
    <section className="section showcase bg-card/50 border-y border-line" id="showcase">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">
            <span />
            Real-Time Observability
          </p>
          <h2>
            One calm <em>control room.</em>
          </h2>
          <p className="max-w-xl text-muted leading-7">
            Observe active calls as they happen, review transcripts, analyze resolution rates, and audit automated actions in real time.
          </p>
        </div>

        {/* Dashboard Container */}
        <div className="dashboard mt-10 shadow-lg">
          <div className="dashboard-top flex-wrap gap-4">
            <div>
              <span className="text-xs font-mono uppercase tracking-widest text-muted">System Dashboard &bull; Today</span>
              <h3 className="mt-1 text-2xl font-semibold text-foreground">Active Call Command Center</h3>
            </div>
            <div className="flex items-center gap-3">
              <span className="demo-badge">Illustrative Product Visual</span>
              <button className="px-3 py-1.5 rounded-full bg-lime text-background text-xs font-semibold hover:opacity-90">
                Export Audit Log
              </button>
            </div>
          </div>

          {/* Metric Bar */}
          <div className="metric-row">
            <div>
              <span>Total Conversations</span>
              <strong>1,248</strong>
              <small>Across 4 active voice agents</small>
            </div>
            <div>
              <span>Autonomous Resolution</span>
              <strong>84.2%</strong>
              <small>Resolved without human transfer</small>
            </div>
            <div>
              <span>Automated Actions</span>
              <strong>1,051</strong>
              <small>Calendar bookings & CRM syncs</small>
            </div>
          </div>

          {/* Table Control Header */}
          <div className="px-6 py-4 border-b border-line bg-panel/60 flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <Filter size={15} className="text-muted" />
              <span className="text-xs font-mono uppercase text-muted mr-2">Filter:</span>
              {(['all', 'resolved', 'escalated'] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`px-3 py-1 rounded-full text-xs font-medium capitalize transition-colors ${
                    filter === f ? 'bg-lime text-background' : 'bg-card text-muted border border-line'
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>

            <div className="flex items-center gap-2 text-xs text-muted font-mono bg-card px-3 py-1.5 rounded-lg border border-line">
              <Search size={14} />
              <span>Search transcripts...</span>
            </div>
          </div>

          {/* Table Data */}
          <div className="dashboard-table overflow-x-auto">
            <div className="min-w-[650px]">
              <div className="table-head">
                <span className="w-1/4">Caller &amp; Agent</span>
                <span className="w-1/4">Intent Detected</span>
                <span className="w-1/4">Executed Action</span>
                <span className="w-1/4 text-right">Outcome</span>
              </div>

              <AnimatePresence mode="popLayout">
                {filteredCalls.map((call) => (
                  <motion.div
                    key={call.id}
                    layout
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -8 }}
                    transition={{ duration: 0.2 }}
                    className="table-row hover:bg-panel/40 transition-colors cursor-pointer"
                  >
                    <div className="w-1/4 flex items-center gap-3">
                      <span className="avatar font-bold">{call.caller[0]}</span>
                      <div>
                        <span className="block font-medium text-foreground text-sm">{call.caller}</span>
                        <span className="block text-[11px] text-muted font-mono">{call.agent} &bull; {call.duration}</span>
                      </div>
                    </div>

                    <div className="w-1/4 text-sm text-foreground/90">
                      <span>{call.intent}</span>
                    </div>

                    <div className="w-1/4 text-xs font-mono text-lime flex items-center gap-1">
                      <CheckCircle2 size={13} />
                      <span>{call.action}</span>
                    </div>

                    <div className="w-1/4 text-right">
                      <span
                        className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-mono font-medium ${
                          call.status === 'resolved'
                            ? 'bg-lime-soft text-lime border border-lime/30'
                            : 'bg-panel text-muted border border-line'
                        }`}
                      >
                        <span className={`w-1.5 h-1.5 rounded-full ${call.status === 'resolved' ? 'bg-lime' : 'bg-muted'}`} />
                        {call.status === 'resolved' ? 'Resolved' : 'Escalated'}
                      </span>
                    </div>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export function HowItWorks() {
  const [activeStep, setActiveStep] = useState(0)

  const steps = [
    { num: '01', title: 'Answer', subtitle: 'Instant Pickup', copy: 'Yuviz answers inbound calls immediately or initiates polite outbound connections with sub-second voice latency.' },
    { num: '02', title: 'Understand', subtitle: 'Intent & Tone Parsing', copy: 'Advanced speech recognition and natural turn-taking allow callers to speak naturally without rigid phone menus.' },
    { num: '03', title: 'Retrieve', subtitle: 'Grounded RAG Ingestion', copy: 'Yuviz queries your company knowledge base, pricing docs, and policy FAQs to ground every answer in facts.' },
    { num: '04', title: 'Act', subtitle: 'API & Workflow Execution', copy: 'The agent triggers CRM updates, schedules calendar slots, issues confirmation messages, or performs API lookups.' },
    { num: '05', title: 'Resolve', subtitle: 'Conversation Outcome', copy: 'The call ends cleanly with all caller questions answered, next steps verified, and no remaining friction.' },
    { num: '06', title: 'Record', subtitle: 'Full Transcript & Audit', copy: 'Transcripts, audio logs, sentiment metrics, and structured key-value data are instantly saved to your workspace.' },
  ]

  return (
    <section className="section container" id="how-it-works">
      <div className="section-heading">
        <p className="eyebrow">
          <span />
          End-to-End Voice Lifecycle
        </p>
        <h2>
          From hello to <em>done.</em>
        </h2>
        <p className="max-w-xl text-muted leading-7">
          Trace how a single call moves seamlessly through reception, knowledge lookup, action execution, and audit logging.
        </p>
      </div>

      {/* Steps Navigation Bar */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-2 mt-10 border-b border-line pb-4">
        {steps.map((step, idx) => (
          <button
            key={step.num}
            onClick={() => setActiveStep(idx)}
            className={`p-3 rounded-xl text-left transition-all border ${
              activeStep === idx
                ? 'border-lime bg-lime-soft/40 text-foreground font-semibold shadow-xs'
                : 'border-transparent text-muted hover:text-foreground'
            }`}
          >
            <span className="block font-mono text-xs text-lime font-bold mb-1">{step.num}</span>
            <span className="block text-sm tracking-tight">{step.title}</span>
          </button>
        ))}
      </div>

      {/* Detailed Active Step Focus Card */}
      <div className="mt-8 border border-line rounded-2xl bg-panel p-8">
        <AnimatePresence mode="wait">
          <motion.div
            key={activeStep}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -12 }}
            transition={{ duration: 0.3 }}
            className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-center"
          >
            <div className="lg:col-span-7">
              <div className="flex items-center gap-3">
                <span className="w-10 h-10 rounded-full bg-lime text-background flex items-center justify-center font-bold text-sm font-mono">
                  {steps[activeStep].num}
                </span>
                <div>
                  <span className="text-xs font-mono uppercase text-lime font-semibold">{steps[activeStep].subtitle}</span>
                  <h3 className="text-2xl font-semibold text-foreground">{steps[activeStep].title}</h3>
                </div>
              </div>

              <p className="mt-4 text-muted text-base leading-relaxed">{steps[activeStep].copy}</p>

              <div className="mt-6 flex items-center gap-3 text-xs font-mono text-foreground font-medium">
                <span className="w-2 h-2 rounded-full bg-lime" />
                <span>Verified System Pipeline Step</span>
              </div>
            </div>

            <div className="lg:col-span-5 border border-line rounded-xl bg-card p-6 shadow-xs">
              <span className="text-xs font-mono uppercase text-muted block mb-3">Live Step Telemetry</span>
              <div className="space-y-2 text-xs font-mono">
                <div className="p-2.5 rounded bg-panel border border-line flex justify-between">
                  <span className="text-muted">Current Step:</span>
                  <span className="font-semibold text-lime">{steps[activeStep].title}</span>
                </div>
                <div className="p-2.5 rounded bg-panel border border-line flex justify-between">
                  <span className="text-muted">Target Response Time:</span>
                  <span className="font-semibold text-foreground">&lt;400ms</span>
                </div>
                <div className="p-2.5 rounded bg-panel border border-line flex justify-between">
                  <span className="text-muted">State Handshake:</span>
                  <span className="font-semibold text-lime">SUCCESS</span>
                </div>
              </div>
            </div>
          </motion.div>
        </AnimatePresence>

        {/* Step Navigation Dots */}
        <div className="mt-8 pt-4 border-t border-line/60 flex items-center justify-between">
          <button
            onClick={() => setActiveStep((prev) => Math.max(0, prev - 1))}
            disabled={activeStep === 0}
            className="text-xs font-semibold text-muted hover:text-foreground disabled:opacity-30 cursor-pointer"
          >
            &larr; Previous Step
          </button>
          <div className="flex gap-1.5">
            {steps.map((_, i) => (
              <span
                key={i}
                className={`w-2 h-2 rounded-full transition-all ${
                  activeStep === i ? 'bg-lime w-6' : 'bg-line'
                }`}
              />
            ))}
          </div>
          <button
            onClick={() => setActiveStep((prev) => Math.min(steps.length - 1, prev + 1))}
            disabled={activeStep === steps.length - 1}
            className="text-xs font-semibold text-lime hover:opacity-80 disabled:opacity-30 cursor-pointer flex items-center gap-1"
          >
            Next Step &rarr;
          </button>
        </div>
      </div>
    </section>
  )
}
