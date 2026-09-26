'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Mic, Settings, Database, Wrench, ShieldCheck, Check, Sparkles, Play } from 'lucide-react'

type TabKey = 'voice' | 'knowledge' | 'personality' | 'tools' | 'handoff'

export function AgentBuilderVisual() {
  const [activeTab, setActiveTab] = useState<TabKey>('voice')
  const [voiceSpeed, setVoiceSpeed] = useState('1.0x')
  const [selectedVoice, setSelectedVoice] = useState('Maya (Warm & Professional)')

  const tabs: { id: TabKey; label: string; icon: React.ComponentType<{ size?: number; className?: string }> }[] = [
    { id: 'voice', label: 'Voice & Latency', icon: Mic },
    { id: 'knowledge', label: 'Knowledge (RAG)', icon: Database },
    { id: 'personality', label: 'Prompt & Tone', icon: Settings },
    { id: 'tools', label: 'API & Workflows', icon: Wrench },
    { id: 'handoff', label: 'Human Handoff', icon: ShieldCheck },
  ]

  const tabContents: Record<TabKey, { title: string; desc: string; options: string[] }> = {
    voice: {
      title: 'Voice Persona & Ultra-Low Latency',
      desc: 'Select a natural sounding voice model tuned for human speech cadence, pause handling, and immediate responses.',
      options: ['Maya (Warm & Professional)', 'Ethan (Direct & Clear)', 'Clara (Empathetic Support)', 'Marcus (Executive Sales)'],
    },
    knowledge: {
      title: 'Ground-Truth Document Knowledge',
      desc: 'Connect FAQs, website URLs, PDF manuals, and internal Notion/Confluence docs for verified answers.',
      options: ['Pricing_Matrix_2026.pdf (Synced)', 'Return_Policy_FAQ.docx (Synced)', 'Web_Catalog_Index (Live)', 'API_Support_KB (Synced)'],
    },
    personality: {
      title: 'Conversational Boundary & Tone',
      desc: 'Define custom system prompts, greeting tone, conciseness rules, and forbidden phrases.',
      options: ['Tone: Professional & Courteous', 'Greeting: "Thank you for calling Yuviz, how can I help?"', 'Max Turn Length: 3 Sentences', 'Interruptibility: Active'],
    },
    tools: {
      title: 'Action Trigger & Webhook Tools',
      desc: 'Give agents permission to query APIs, check calendar availability, and post CRM records in real time.',
      options: ['Google Calendar API (Active)', 'Salesforce Lead Sync (Active)', 'Stripe Payment Lookup (Active)', 'Twilio SMS Gateway (Active)'],
    },
    handoff: {
      title: 'Contextual Escalation Rules',
      desc: 'Configure exact conditions when an agent hands off a call to a human teammate along with call transcript summaries.',
      options: ['Escalate on explicitly requested human representative', 'Escalate on sentiment score drop (<0.3)', 'Warm Transfer to Front Desk Desk-04', 'Pass real-time conversation summary text'],
    },
  }

  return (
    <section className="section bg-card/60 border-y border-line" id="agent-builder">
      <div className="container">
        <div className="section-heading text-center max-w-2xl mx-auto">
          <p className="eyebrow justify-center">
            <span />
            No-Code Agent Creation
          </p>
          <h2 className="text-4xl md:text-5xl font-semibold tracking-tight text-foreground">
            Configure an agent in <em>minutes.</em>
          </h2>
          <p className="text-muted leading-7">
            Designing a Yuviz voice agent feels like onboarding a top-tier teammate. Define knowledge, pick a voice, and wire up your workflow tools without writing complex code.
          </p>
        </div>

        {/* Builder Interface Mockup */}
        <div className="mt-12 border border-line rounded-2xl bg-panel overflow-hidden shadow-md">
          {/* Top Header */}
          <div className="flex flex-wrap items-center justify-between p-6 border-b border-line bg-card">
            <div className="flex items-center gap-3">
              <div className="w-9 h-9 rounded-full bg-lime text-background flex items-center justify-center font-bold text-sm">
                Y
              </div>
              <div>
                <h3 className="text-base font-semibold text-foreground">Yuviz Agent Configurator</h3>
                <p className="text-xs text-muted font-mono">Agent ID: agt_9824_production</p>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <span className="flex items-center gap-1.5 text-xs font-mono text-lime font-semibold px-3 py-1 rounded-full bg-lime-soft/50 border border-lime/30">
                <span className="w-2 h-2 rounded-full bg-lime animate-pulse" /> Status: Ready to Deploy
              </span>
              <button className="hidden sm:flex items-center gap-2 px-4 py-2 rounded-full bg-lime text-background text-xs font-semibold hover:opacity-90 transition-opacity">
                <Play size={13} /> Test Agent
              </button>
            </div>
          </div>

          {/* Navigation Tabs */}
          <div className="flex border-b border-line bg-panel overflow-x-auto">
            {tabs.map((tab) => {
              const Icon = tab.icon
              const isActive = activeTab === tab.id
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center gap-2 px-6 py-4 text-xs font-semibold whitespace-nowrap transition-all border-b-2 ${
                    isActive
                      ? 'border-lime text-foreground bg-card'
                      : 'border-transparent text-muted hover:text-foreground'
                  }`}
                >
                  <Icon size={16} className={isActive ? 'text-lime' : 'text-muted'} />
                  {tab.label}
                </button>
              )
            })}
          </div>

          {/* Content Body */}
          <div className="p-8 grid grid-cols-1 lg:grid-cols-12 gap-8 items-start bg-card">
            {/* Left Options Controls */}
            <div className="lg:col-span-7 space-y-6">
              <AnimatePresence mode="wait">
                <motion.div
                  key={activeTab}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: 10 }}
                  transition={{ duration: 0.25 }}
                >
                  <h4 className="text-xl font-semibold text-foreground">{tabContents[activeTab].title}</h4>
                  <p className="mt-1 text-sm text-muted leading-relaxed">{tabContents[activeTab].desc}</p>

                  <div className="mt-6 space-y-3">
                    {tabContents[activeTab].options.map((opt, i) => (
                      <div
                        key={i}
                        onClick={() => {
                          if (activeTab === 'voice') setSelectedVoice(opt)
                        }}
                        className={`p-4 rounded-xl border transition-all flex items-center justify-between cursor-pointer ${
                          activeTab === 'voice' && selectedVoice === opt
                            ? 'border-lime bg-lime-soft/20 text-foreground font-medium'
                            : 'border-line bg-panel/40 hover:bg-panel text-foreground/90'
                        }`}
                      >
                        <span className="text-sm font-medium">{opt}</span>
                        <div className="w-5 h-5 rounded-full border border-lime flex items-center justify-center text-lime bg-card">
                          <Check size={12} />
                        </div>
                      </div>
                    ))}
                  </div>
                </motion.div>
              </AnimatePresence>

              {activeTab === 'voice' && (
                <div className="p-4 rounded-xl border border-line bg-panel flex items-center justify-between">
                  <span className="text-xs text-muted font-medium">Speech Rate Multiplier:</span>
                  <div className="flex gap-2">
                    {['0.9x', '1.0x', '1.1x'].map((speed) => (
                      <button
                        key={speed}
                        onClick={() => setVoiceSpeed(speed)}
                        className={`px-3 py-1 rounded-md text-xs font-mono font-semibold transition-colors ${
                          voiceSpeed === speed
                            ? 'bg-lime text-background'
                            : 'bg-card text-muted border border-line'
                        }`}
                      >
                        {speed}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Right Live Preview Card */}
            <div className="lg:col-span-5 border border-line rounded-xl bg-panel p-6">
              <div className="flex items-center justify-between border-b border-line pb-3 mb-4">
                <span className="text-xs font-mono uppercase tracking-widest text-muted">Config Preview</span>
                <span className="text-xs font-mono text-lime">Active Simulation</span>
              </div>

              <div className="space-y-3 text-xs font-mono">
                <div className="p-3 rounded-lg bg-card border border-line flex justify-between">
                  <span className="text-muted">Selected Voice:</span>
                  <span className="font-semibold text-foreground truncate max-w-[170px]">{selectedVoice}</span>
                </div>
                <div className="p-3 rounded-lg bg-card border border-line flex justify-between">
                  <span className="text-muted">Speech Speed:</span>
                  <span className="font-semibold text-lime">{voiceSpeed}</span>
                </div>
                <div className="p-3 rounded-lg bg-card border border-line flex justify-between">
                  <span className="text-muted">Active Knowledge:</span>
                  <span className="font-semibold text-foreground">4 Synced Files</span>
                </div>
                <div className="p-3 rounded-lg bg-card border border-line flex justify-between">
                  <span className="text-muted">Connected APIs:</span>
                  <span className="font-semibold text-foreground">Google Cal + CRM</span>
                </div>
              </div>

              <div className="mt-6 p-4 rounded-xl bg-card border border-line text-center">
                <Sparkles size={20} className="mx-auto text-lime mb-2" />
                <p className="text-xs text-foreground font-semibold">Agent Ready to Handle Live Calls</p>
                <p className="text-[11px] text-muted mt-1">Changes sync automatically across telephony routes.</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
