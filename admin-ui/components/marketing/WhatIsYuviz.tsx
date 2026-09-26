'use client'

import { Headphones, ShieldCheck, Zap, Layers, RefreshCw, Cpu } from 'lucide-react'
import { motion } from 'framer-motion'

export function TrustStrip() {
  const capabilities = [
    { icon: Headphones, label: 'Inbound & Outbound Telephony' },
    { icon: Zap, label: 'Sub-Second Voice Latency' },
    { icon: Layers, label: 'Grounded RAG Knowledge' },
    { icon: ShieldCheck, label: 'Contextual Human Handoff' },
  ]

  return (
    <section className="credibility border-y border-line bg-card/60">
      <div className="container flex flex-wrap items-center justify-between gap-6 py-6">
        <p className="text-xs uppercase tracking-widest text-muted font-mono font-semibold">
          Built for high-stakes business calls
        </p>

        <div className="flex flex-wrap items-center gap-x-8 gap-y-3 text-sm text-foreground/80 font-medium">
          {capabilities.map((item, idx) => {
            const Icon = item.icon
            return (
              <span key={idx} className="flex items-center gap-2 hover:text-lime transition-colors">
                <Icon size={16} className="text-lime" />
                {item.label}
              </span>
            )
          })}
        </div>
      </div>
    </section>
  )
}

export function WhatIsYuviz() {
  return (
    <section className="section container" id="what-is-yuviz">
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-12 items-center">
        {/* Text Left */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
          className="lg:col-span-6"
        >
          <p className="eyebrow mb-3">
            <span />
            The Voice AI Platform
          </p>
          <h2 className="text-4xl md:text-5xl lg:text-6xl font-semibold tracking-[--tracking-tight] leading-[1.05] text-foreground">
            Meet your <em>AI voice team.</em>
          </h2>
          <p className="mt-6 text-muted text-base md:text-lg leading-relaxed max-w-xl">
            Yuviz lets businesses deploy intelligent, natural voice agents that handle calls, converse with callers in real time, lookup internal knowledge, and execute next steps directly in your systems.
          </p>
          <p className="mt-4 text-muted text-base leading-relaxed max-w-xl">
            Whether taking inbound calls during peak hours or conducting polite outbound outreach, your Yuviz voice agents act as dedicated team members that never miss a detail.
          </p>

          <div className="mt-8 grid grid-cols-2 gap-4 border-t border-line pt-6">
            <div>
              <p className="text-2xl font-bold text-foreground">100%</p>
              <p className="text-xs text-muted font-medium mt-1">Call capture rate 24/7</p>
            </div>
            <div>
              <p className="text-2xl font-bold text-foreground">&lt;800ms</p>
              <p className="text-xs text-muted font-medium mt-1">Natural conversational latency</p>
            </div>
          </div>
        </motion.div>

        {/* Interactive Visual Flow Right */}
        <motion.div
          initial={{ opacity: 0, scale: 0.96 }}
          whileInView={{ opacity: 1, scale: 1 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="lg:col-span-6 border border-line rounded-2xl bg-panel p-8 relative overflow-hidden shadow-sm"
        >
          <p className="text-xs font-mono uppercase tracking-widest text-muted mb-8 text-center">
            Real-Time Signal Orchestration
          </p>

          <div className="flex flex-col md:flex-row items-center justify-between gap-6 relative z-10">
            {/* Customer Box */}
            <div className="flex flex-col items-center p-5 rounded-xl border border-line bg-card w-full md:w-36 text-center shadow-xs">
              <div className="w-10 h-10 rounded-full bg-lime/10 border border-lime/30 flex items-center justify-center text-lime mb-2 font-bold text-sm">
                C
              </div>
              <span className="text-xs font-semibold text-foreground">Customer</span>
              <span className="text-[10px] text-muted mt-0.5">Caller / Lead</span>
            </div>

            {/* Signal Flow Connector 1 */}
            <div className="flex md:flex-col items-center gap-1 text-lime font-mono text-[10px]">
              <motion.div
                animate={{ x: [0, 8, 0] }}
                transition={{ duration: 1.5, repeat: Infinity, ease: 'easeInOut' }}
                className="flex items-center gap-1"
              >
                <RefreshCw size={14} className="animate-spin text-lime" />
                <span>Audio Stream</span>
              </motion.div>
            </div>

            {/* Yuviz Agent Center */}
            <div className="flex flex-col items-center p-6 rounded-xl border-2 border-lime bg-lime-soft/30 w-full md:w-44 text-center shadow-md relative">
              <div className="w-12 h-12 rounded-full bg-lime text-background flex items-center justify-center font-bold text-base shadow-sm mb-2">
                Y
              </div>
              <span className="text-sm font-semibold text-foreground">Yuviz Agent</span>
              <span className="text-[11px] text-lime font-medium mt-0.5">Grounded RAG + Voice</span>
            </div>

            {/* Signal Flow Connector 2 */}
            <div className="flex md:flex-col items-center gap-1 text-lime font-mono text-[10px]">
              <motion.div
                animate={{ x: [0, -8, 0] }}
                transition={{ duration: 1.5, repeat: Infinity, ease: 'easeInOut' }}
                className="flex items-center gap-1"
              >
                <Cpu size={14} className="text-lime" />
                <span>API / CRM</span>
              </motion.div>
            </div>

            {/* Business Systems Box */}
            <div className="flex flex-col items-center p-5 rounded-xl border border-line bg-card w-full md:w-36 text-center shadow-xs">
              <div className="w-10 h-10 rounded-full bg-foreground/10 border border-line flex items-center justify-center text-foreground mb-2 font-bold text-sm">
                B
              </div>
              <span className="text-xs font-semibold text-foreground">Business</span>
              <span className="text-[10px] text-muted mt-0.5">CRM, Calendar, ERP</span>
            </div>
          </div>

          <div className="mt-8 pt-4 border-t border-line/60 flex items-center justify-between text-xs text-muted font-mono">
            <span>Flow: Speech &rarr; Intent &rarr; Action</span>
            <span className="text-lime font-semibold">&bull; Live Synchronized</span>
          </div>
        </motion.div>
      </div>
    </section>
  )
}
