'use client'

import { useState } from 'react'
import { motion } from 'framer-motion'
import { UserCheck, ShieldCheck, Lock, Key, Server, FileText, ArrowRight } from 'lucide-react'

export function HumanHandoff() {
  return (
    <section className="section bg-card/60 border-y border-line" id="human-handoff">
      <div className="container">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-12 items-center">
          <div className="lg:col-span-6 space-y-4">
            <p className="eyebrow">
              <span />
              Graceful Escalation
            </p>
            <h2 className="text-4xl md:text-5xl font-semibold tracking-tight text-foreground">
              AI when it can. <em>Humans when it should.</em>
            </h2>
            <p className="text-muted leading-relaxed">
              Automated voice shouldn&apos;t be an inescapable trap. When a call requires nuanced judgment or executive attention, Yuviz warm-transfers the caller directly to a human teammate along with the real-time transcript summary.
            </p>

            <div className="pt-4 space-y-3">
              <div className="flex items-start gap-3">
                <div className="w-6 h-6 rounded-full bg-lime/10 text-lime flex items-center justify-center font-bold text-xs mt-0.5">
                  ✓
                </div>
                <div>
                  <h4 className="text-sm font-semibold text-foreground">Zero Cold Restarts</h4>
                  <p className="text-xs text-muted">The human teammate receives a 3-bullet summary of what was discussed so the caller doesn&apos;t repeat themselves.</p>
                </div>
              </div>

              <div className="flex items-start gap-3">
                <div className="w-6 h-6 rounded-full bg-lime/10 text-lime flex items-center justify-center font-bold text-xs mt-0.5">
                  ✓
                </div>
                <div>
                  <h4 className="text-sm font-semibold text-foreground">Sentiment-Triggered Hand-Off</h4>
                  <p className="text-xs text-muted">Configure triggers based on key phrases, sentiment scores, or explicit agent requests.</p>
                </div>
              </div>
            </div>
          </div>

          <div className="lg:col-span-6 border border-line rounded-2xl bg-panel p-8 shadow-sm">
            <p className="text-xs font-mono uppercase tracking-widest text-muted mb-6">Visual Warm Transfer Route</p>
            <div className="space-y-4 font-mono text-xs">
              <div className="p-4 rounded-xl bg-card border border-line flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="w-3 h-3 rounded-full bg-lime" />
                  <span className="font-semibold text-foreground">01. Yuviz Agent Conversation</span>
                </div>
                <span className="text-muted">In Progress</span>
              </div>

              <div className="p-3 text-center text-lime font-bold">&darr; Escalation Event Triggered</div>

              <div className="p-4 rounded-xl bg-lime-soft/40 border border-lime/40 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <UserCheck size={16} className="text-lime" />
                  <span className="font-semibold text-foreground">02. Warm Transfer to Front Desk</span>
                </div>
                <span className="text-lime font-bold">Transcript Passed</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export function SecuritySection() {
  const securityItems = [
    { title: 'Tenant Data Isolation', desc: 'Each workspace operates inside strict logical data boundaries ensuring zero cross-tenant contamination.', icon: Server },
    { title: 'Secrets & Vault Storage', desc: 'API keys, telephony tokens, and integration credentials are stored in encrypted Vault hardware modules.', icon: Key },
    { title: 'Encrypted Telephony', desc: 'Audio streams and WebRTC sessions use end-to-end TLS 1.3 and SRTP encryption protocol.', icon: Lock },
    { title: 'Role-Based Access Control', desc: 'Granular permissions ensure team members access only the audit logs and agents relevant to their role.', icon: ShieldCheck },
  ]

  return (
    <section className="section container" id="security">
      <div className="section-heading text-center max-w-2xl mx-auto">
        <p className="eyebrow justify-center">
          <span />
          Enterprise Trust &amp; Security
        </p>
        <h2>
          Your conversations deserve <em>serious protection.</em>
        </h2>
        <p className="text-muted leading-7">
          Security and data protection are fundamental architectural constraints, not afterthoughts.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mt-10">
        {securityItems.map((item) => {
          const Icon = item.icon
          return (
            <div key={item.title} className="p-6 rounded-2xl border border-line bg-card hover:border-lime transition-all">
              <div className="w-10 h-10 rounded-xl bg-panel border border-line flex items-center justify-center text-lime mb-4">
                <Icon size={20} />
              </div>
              <h4 className="text-lg font-semibold text-foreground tracking-tight">{item.title}</h4>
              <p className="mt-2 text-xs text-muted leading-relaxed">{item.desc}</p>
            </div>
          )
        })}
      </div>
    </section>
  )
}

export function WhyYuviz() {
  const principles = [
    { title: 'Natural', desc: 'Speech cadence, turn-taking, and interruption handling feel like a real human phone conversation.' },
    { title: 'Useful', desc: 'Agents do far more than talk. They trigger webhooks, query databases, and resolve requests.' },
    { title: 'Connected', desc: 'Integrates natively into the CRMs, calendar systems, and tools your business relies on.' },
    { title: 'Observable', desc: 'Complete visibility into transcripts, sentiment metrics, resolution rates, and audit logs.' },
  ]

  return (
    <section className="section bg-card/50 border-y border-line" id="why-yuviz">
      <div className="container">
        <div className="section-heading text-center max-w-2xl mx-auto">
          <p className="eyebrow justify-center">
            <span />
            Core Philosophy
          </p>
          <h2>
            Built around the <em>conversation.</em>
          </h2>
          <p className="text-muted leading-7">
            Four guiding principles that shape how Yuviz voice agents interact with your customers.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mt-10">
          {principles.map((p, idx) => (
            <div key={p.title} className="p-6 rounded-2xl border border-line bg-panel hover:bg-card transition-all">
              <span className="font-mono text-xs font-bold text-lime block mb-2">0{idx + 1}</span>
              <h3 className="text-xl font-semibold text-foreground tracking-tight">{p.title}</h3>
              <p className="mt-2 text-sm text-muted leading-relaxed">{p.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
