'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { XCircle, CheckCircle2, Cpu, Lock, Shield, Database, Radio, Server, Layers } from 'lucide-react'

export function BeforeAfter() {
  const [activeTab, setActiveTab] = useState<'with' | 'without'>('with')

  return (
    <section className="section bg-card/60 border-y border-line" id="before-after">
      <div className="container">
        <div className="section-heading text-center max-w-2xl mx-auto">
          <p className="eyebrow justify-center">
            <span />
            Operational Transformation
          </p>
          <h2 className="text-4xl md:text-5xl font-semibold tracking-tight text-foreground">
            The difference is <em>clarity.</em>
          </h2>
          <p className="text-muted leading-7">
            Compare traditional phone support bottlenecks with Yuviz 24/7 grounded voice intelligence.
          </p>

          <div className="mt-8 inline-flex p-1 rounded-full bg-panel border border-line">
            <button
              onClick={() => setActiveTab('without')}
              className={`px-5 py-2 rounded-full text-xs font-semibold transition-all ${
                activeTab === 'without' ? 'bg-foreground text-background shadow-xs' : 'text-muted hover:text-foreground'
              }`}
            >
              Without Yuviz
            </button>
            <button
              onClick={() => setActiveTab('with')}
              className={`px-5 py-2 rounded-full text-xs font-semibold transition-all ${
                activeTab === 'with' ? 'bg-lime text-background shadow-xs' : 'text-muted hover:text-foreground'
              }`}
            >
              With Yuviz
            </button>
          </div>
        </div>

        <div className="mt-10 max-w-4xl mx-auto border border-line rounded-2xl bg-panel p-8 shadow-sm">
          <AnimatePresence mode="wait">
            {activeTab === 'without' ? (
              <motion.div
                key="without"
                initial={{ opacity: 0, scale: 0.98 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.98 }}
                transition={{ duration: 0.25 }}
                className="space-y-4"
              >
                <h3 className="text-xl font-semibold text-foreground flex items-center gap-2">
                  <XCircle className="text-red-500" size={20} /> Traditional Phone Friction
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                  <div className="p-4 rounded-xl bg-card border border-line">
                    <span className="text-xs font-semibold text-foreground block">Long Hold Times</span>
                    <p className="text-xs text-muted mt-1">Callers wait on hold for minutes or get sent to unanswered voicemail inboxes.</p>
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-line">
                    <span className="text-xs font-semibold text-foreground block">Rigid Phone Menus</span>
                    <p className="text-xs text-muted mt-1">Customers press 1, press 2, and get frustrated by repetitive automated phone trees.</p>
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-line">
                    <span className="text-xs font-semibold text-foreground block">Manual Follow-ups</span>
                    <p className="text-xs text-muted mt-1">Staff spends hours manually logging call notes, scheduling slots, and typing SMS texts.</p>
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-line">
                    <span className="text-xs font-semibold text-foreground block">Limited Coverage</span>
                    <p className="text-xs text-muted mt-1">Calls after 5:00 PM or during weekend rushes go completely unanswered.</p>
                  </div>
                </div>
              </motion.div>
            ) : (
              <motion.div
                key="with"
                initial={{ opacity: 0, scale: 0.98 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.98 }}
                transition={{ duration: 0.25 }}
                className="space-y-4"
              >
                <h3 className="text-xl font-semibold text-lime flex items-center gap-2">
                  <CheckCircle2 className="text-lime" size={20} /> Yuviz AI Voice Intelligence
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                  <div className="p-4 rounded-xl bg-card border border-lime/40 bg-lime-soft/10">
                    <span className="text-xs font-semibold text-foreground block">Instant 0.0s Pickup</span>
                    <p className="text-xs text-muted mt-1">Every caller is greeted immediately by a warm, natural voice agent.</p>
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-lime/40 bg-lime-soft/10">
                    <span className="text-xs font-semibold text-foreground block">Natural Voice Dialogue</span>
                    <p className="text-xs text-muted mt-1">Callers speak naturally without rigid phone prompts or robotic delays.</p>
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-lime/40 bg-lime-soft/10">
                    <span className="text-xs font-semibold text-foreground block">Automated Action Execution</span>
                    <p className="text-xs text-muted mt-1">Calendar slots are booked, CRMs are updated, and SMS texts dispatch automatically.</p>
                  </div>
                  <div className="p-4 rounded-xl bg-card border border-lime/40 bg-lime-soft/10">
                    <span className="text-xs font-semibold text-foreground block">24/7 Always-On Presence</span>
                    <p className="text-xs text-muted mt-1">Complete call coverage evenings, weekends, and holidays with zero extra staffing overhead.</p>
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

export function ArchitectureSection() {
  return (
    <section className="section container" id="architecture">
      <div className="tech-grid">
        <div>
          <p className="eyebrow">
            <span />
            Under the Voice
          </p>
          <h2 className="text-4xl md:text-5xl font-semibold tracking-tight text-foreground">
            Built for <em>real conversations.</em>
          </h2>
          <p className="mt-4 text-muted leading-7">
            A dependable, multi-tenant infrastructure engineered specifically for real-time speech processing, sub-second latency, and enterprise security.
          </p>

          <div className="mt-8 space-y-4">
            <div className="flex items-start gap-3">
              <div className="w-8 h-8 rounded-lg bg-panel border border-line flex items-center justify-center text-lime font-bold text-xs mt-0.5">
                01
              </div>
              <div>
                <h4 className="text-base font-semibold text-foreground">Real-Time Telephony Gateway</h4>
                <p className="text-xs text-muted leading-relaxed">WebRTC and gRPC streaming pipelines handle bi-directional audio with sub-second response speeds.</p>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <div className="w-8 h-8 rounded-lg bg-panel border border-line flex items-center justify-center text-lime font-bold text-xs mt-0.5">
                02
              </div>
              <div>
                <h4 className="text-base font-semibold text-foreground">Grounded Vector Knowledge (RAG)</h4>
                <p className="text-xs text-muted leading-relaxed">PostgreSQL &amp; pgvector store tenant knowledge embeddings for accurate, hallucination-free answers.</p>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <div className="w-8 h-8 rounded-lg bg-panel border border-line flex items-center justify-center text-lime font-bold text-xs mt-0.5">
                03
              </div>
              <div>
                <h4 className="text-base font-semibold text-foreground">Secure Multi-Tenant Isolation</h4>
                <p className="text-xs text-muted leading-relaxed">Strict data separation, encrypted Vault secrets management, and tokenized API access controls.</p>
              </div>
            </div>
          </div>
        </div>

        {/* Architecture Stack Diagram */}
        <div className="architecture shadow-md">
          <div className="arch-row">
            <span className="font-mono text-xs font-semibold">Speech Input</span>
            <i />
            <span className="font-mono text-xs font-semibold">Orchestration</span>
            <i />
            <span className="font-mono text-xs font-semibold">Voice Output</span>
          </div>

          <div className="arch-stack">
            <span className="font-mono text-xs font-bold text-foreground bg-lime-soft/40 border-lime/40">
              Real-Time Speech Pipeline (WebRTC / gRPC Gateway)
            </span>
            <span className="font-mono text-xs text-muted">
              Conversation Intelligence Engine (STT &rarr; LLM &rarr; TTS)
            </span>
            <span className="font-mono text-xs text-muted">
              Vector RAG Knowledge Layer (PostgreSQL + pgvector)
            </span>
            <span className="font-mono text-xs text-muted">
              Integration &amp; Webhook Gateway (CRM, Calendar, API)
            </span>
          </div>

          <p className="mt-6 text-xs font-mono uppercase tracking-[0.16em] text-muted text-center border-t border-line pt-4">
            Encrypted &bull; Multi-Tenant Isolated &bull; Vault Protected
          </p>
        </div>
      </div>
    </section>
  )
}
