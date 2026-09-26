'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { PhoneIncoming, PhoneOutgoing, Users, BarChart3, Check, ArrowRight } from 'lucide-react'

export function InboundOutbound() {
  const [mode, setMode] = useState<'inbound' | 'outbound'>('inbound')

  return (
    <section className="section bg-card/50 border-y border-line" id="inbound-outbound">
      <div className="container">
        <div className="section-heading text-center max-w-2xl mx-auto">
          <p className="eyebrow justify-center">
            <span />
            Dual Direction Telephony
          </p>
          <h2 className="text-4xl md:text-5xl font-semibold tracking-tight text-foreground">
            Inbound reception &amp; <em>outbound scale.</em>
          </h2>
          <p className="text-muted leading-7">
            Toggle between inbound call answering and automated outbound campaign execution. Yuviz provides complete coverage for both channels.
          </p>

          {/* Segmented Mode Selector */}
          <div className="mt-8 inline-flex p-1.5 rounded-full bg-panel border border-line">
            <button
              onClick={() => setMode('inbound')}
              className={`flex items-center gap-2 px-6 py-2.5 rounded-full text-xs font-semibold transition-all ${
                mode === 'inbound'
                  ? 'bg-lime text-background shadow-xs'
                  : 'text-muted hover:text-foreground'
              }`}
            >
              <PhoneIncoming size={15} /> Inbound Mode
            </button>
            <button
              onClick={() => setMode('outbound')}
              className={`flex items-center gap-2 px-6 py-2.5 rounded-full text-xs font-semibold transition-all ${
                mode === 'outbound'
                  ? 'bg-lime text-background shadow-xs'
                  : 'text-muted hover:text-foreground'
              }`}
            >
              <PhoneOutgoing size={15} /> Outbound Campaign
            </button>
          </div>
        </div>

        {/* Dynamic Display Panel */}
        <div className="mt-12 border border-line rounded-2xl bg-panel p-8 shadow-md">
          <AnimatePresence mode="wait">
            {mode === 'inbound' ? (
              <motion.div
                key="inbound"
                initial={{ opacity: 0, x: -15 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 15 }}
                transition={{ duration: 0.3 }}
                className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-center"
              >
                <div className="lg:col-span-6 space-y-4">
                  <span className="text-xs font-mono font-bold text-lime uppercase tracking-widest">Inbound Workflow</span>
                  <h3 className="text-3xl font-semibold text-foreground">Never miss a customer call again.</h3>
                  <p className="text-muted leading-relaxed">
                    When callers reach your business, Yuviz picks up immediately. It handles peak traffic overflow, answers FAQs, schedules appointments, and transfers complex calls.
                  </p>
                  <ul className="space-y-2 text-sm text-foreground/90 font-medium pt-2">
                    <li className="flex items-center gap-2">
                      <Check size={16} className="text-lime" /> Zero hold queue during peak call hours
                    </li>
                    <li className="flex items-center gap-2">
                      <Check size={16} className="text-lime" /> Automatic caller ID verification &amp; CRM lookup
                    </li>
                    <li className="flex items-center gap-2">
                      <Check size={16} className="text-lime" /> Contextual warm transfer to human desk
                    </li>
                  </ul>
                </div>

                <div className="lg:col-span-6 border border-line rounded-xl bg-card p-6 shadow-xs">
                  <p className="text-xs font-mono uppercase text-muted mb-4">Inbound Telephony Route</p>
                  <div className="space-y-3 font-mono text-xs">
                    <div className="p-3 rounded bg-panel border border-line flex justify-between">
                      <span className="text-muted">Caller Phone Ring:</span>
                      <span className="font-semibold text-foreground">Immediate Answer (0s)</span>
                    </div>
                    <div className="p-3 rounded bg-panel border border-line flex justify-between">
                      <span className="text-muted">Grounded RAG Lookup:</span>
                      <span className="font-semibold text-lime">240ms Response</span>
                    </div>
                    <div className="p-3 rounded bg-panel border border-line flex justify-between">
                      <span className="text-muted">Calendar Booking:</span>
                      <span className="font-semibold text-foreground">Synced to Google Cal</span>
                    </div>
                  </div>
                </div>
              </motion.div>
            ) : (
              <motion.div
                key="outbound"
                initial={{ opacity: 0, x: 15 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -15 }}
                transition={{ duration: 0.3 }}
                className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-center"
              >
                <div className="lg:col-span-6 space-y-4">
                  <span className="text-xs font-mono font-bold text-lime uppercase tracking-widest">Outbound Campaigns</span>
                  <h3 className="text-3xl font-semibold text-foreground">Start conversations at scale.</h3>
                  <p className="text-muted leading-relaxed">
                    Launch permissioned outbound campaigns for appointment confirmations, lead follow-ups, re-engagement, and customer satisfaction check-ins.
                  </p>
                  <ul className="space-y-2 text-sm text-foreground/90 font-medium pt-2">
                    <li className="flex items-center gap-2">
                      <Check size={16} className="text-lime" /> Compliant, permissioned outreach schedules
                    </li>
                    <li className="flex items-center gap-2">
                      <Check size={16} className="text-lime" /> Dynamic personalized context per lead
                    </li>
                    <li className="flex items-center gap-2">
                      <Check size={16} className="text-lime" /> Real-time campaign conversion tracking
                    </li>
                  </ul>
                </div>

                <div className="lg:col-span-6 border border-line rounded-xl bg-card p-6 shadow-xs">
                  <p className="text-xs font-mono uppercase text-muted mb-4">Outbound Campaign Dashboard Visual</p>
                  <div className="grid grid-cols-2 gap-3 font-mono text-xs mb-4">
                    <div className="p-3 rounded bg-panel border border-line">
                      <span className="text-muted block">Queued Contacts</span>
                      <span className="text-xl font-bold text-foreground">2,480</span>
                    </div>
                    <div className="p-3 rounded bg-panel border border-line">
                      <span className="text-muted block">Calls Connected</span>
                      <span className="text-xl font-bold text-lime">1,124</span>
                    </div>
                  </div>
                  <div className="p-3 rounded bg-panel border border-line text-xs font-mono text-center">
                    <span className="text-lime font-bold">Campaign Status:</span> Active &bull; 918 Completed Conversations
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </section>
  )
}

export function UseCaseMatrix() {
  const [selectedRow, setSelectedRow] = useState<number | null>(0)

  const rows = [
    {
      case: 'Sales & Qualification',
      yuviz: 'Answers prospect questions, screens intent, and schedules sales calls directly into representative calendars.',
      outcome: '3x faster lead response time & zero lost inbound sales leads.',
    },
    {
      case: 'Customer Support',
      yuviz: 'Resolves Tier-1 questions, verifies order status, retrieves policy knowledge, and escalates when appropriate.',
      outcome: '70% reduction in support queue wait times.',
    },
    {
      case: 'Front Desk & Reception',
      yuviz: 'Greets callers warmly, identifies intent, routes calls to specific extensions, and takes overflow calls 24/7.',
      outcome: '100% call capture with zero missed phone leads.',
    },
    {
      case: 'Appointments & Rescheduling',
      yuviz: 'Coordinates open appointment slots, reschedules existing bookings, and dispatches instant SMS confirmations.',
      outcome: 'Eliminates phone tag and no-shows.',
    },
    {
      case: 'Outbound Reminders',
      yuviz: 'Conducts polite reminder calls for upcoming appointments, service renewals, or follow-ups.',
      outcome: 'Higher appointment completion rates at scale.',
    },
  ]

  return (
    <section className="section container" id="use-case-matrix">
      <div className="section-heading">
        <p className="eyebrow">
          <span />
          Use Case Comparison Matrix
        </p>
        <h2>
          What should your agent <em>handle?</em>
        </h2>
        <p className="max-w-xl text-muted leading-7">
          Scan common enterprise voice workflows and see what Yuviz automates versus the immediate business outcome.
        </p>
      </div>

      <div className="mt-10 border border-line rounded-2xl bg-panel overflow-hidden shadow-sm">
        <div className="hidden md:flex items-center justify-between p-4 border-b border-line bg-card text-xs font-mono uppercase text-muted">
          <span className="w-1/4">Use Case</span>
          <span className="w-1/2">What Yuviz Does</span>
          <span className="w-1/4 text-right">Business Outcome</span>
        </div>

        {rows.map((row, idx) => (
          <div
            key={row.case}
            onClick={() => setSelectedRow(selectedRow === idx ? null : idx)}
            className={`p-5 border-b border-line/70 transition-all cursor-pointer ${
              selectedRow === idx ? 'bg-card' : 'bg-panel/40 hover:bg-panel'
            }`}
          >
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
              <span className="w-full md:w-1/4 font-semibold text-foreground text-base tracking-tight">{row.case}</span>
              <p className="w-full md:w-1/2 text-sm text-muted leading-relaxed">{row.yuviz}</p>
              <div className="w-full md:w-1/4 md:text-right">
                <span className="inline-flex items-center gap-1 text-xs font-mono font-semibold text-lime px-3 py-1 rounded-full bg-lime-soft/50 border border-lime/30">
                  {row.outcome}
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
