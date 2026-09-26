'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { PhoneCall, Brain, CheckCircle, ArrowRight, Sparkles, UserCheck, Calendar, ShieldCheck, Zap, Headphones } from 'lucide-react'

export function WhyVoice() {
  const [activeStep, setActiveStep] = useState(0)

  const steps = [
    {
      id: 'answer',
      num: '01',
      title: 'ANSWER',
      heading: 'Someone picks up instantly.',
      desc: 'No menus, no endless hold tones, no voicemail black holes. The moment a customer calls, Yuviz greets them naturally with zero delay.',
      icon: PhoneCall,
      signal: 'Call Connected • 0.0s',
      detail: 'Yuviz handles incoming calls immediately, verifying caller identity and capturing context from sentence one.',
    },
    {
      id: 'understand',
      num: '02',
      title: 'UNDERSTAND',
      heading: 'The conversation is understood.',
      desc: 'Yuviz listens actively, handles interruptions gracefully, parses tone and intent, and retrieves ground-truth knowledge from your business files.',
      icon: Brain,
      signal: 'Context Parsed • RAG Verified',
      detail: 'Semantic search matches caller requests with exact product specs, policies, or calendar openings.',
    },
    {
      id: 'act',
      num: '03',
      title: 'ACT',
      heading: 'The right next step happens.',
      desc: 'Rather than leaving notes for later, Yuviz books appointments, dispatches webhooks, qualifies leads, or hands off with full transcript context.',
      icon: CheckCircle,
      signal: 'Workflow Executed • 100% Sync',
      detail: 'Direct CRM sync, instant SMS dispatch, or warm transfers ensure caller request resolution before hanging up.',
    },
  ]

  return (
    <section className="section bg-card/40 border-y border-line" id="why-voice">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">
            <span />
            Why Voice Matters
          </p>
          <h2>
            Some conversations are <em>better spoken.</em>
          </h2>
          <p className="max-w-xl text-muted leading-7">
            When people need help, guidance, or immediate action, they pick up the phone. Yuviz gives your business the capacity to listen and act on every call.
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 mt-12 items-center">
          {/* Interactive Steps Left */}
          <div className="lg:col-span-6 space-y-4">
            {steps.map((step, idx) => {
              const Icon = step.icon
              const isActive = activeStep === idx
              return (
                <motion.div
                  key={step.id}
                  onClick={() => setActiveStep(idx)}
                  whileHover={{ x: 4 }}
                  className={`p-6 rounded-2xl border transition-all cursor-pointer ${
                    isActive
                      ? 'border-lime bg-card shadow-md ring-1 ring-lime/20'
                      : 'border-line bg-panel/60 hover:bg-card/80'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <span className="font-mono text-xs text-lime font-bold px-2 py-0.5 rounded bg-lime-soft/60">
                        {step.num}
                      </span>
                      <h3 className="text-lg font-semibold text-foreground tracking-tight">{step.title}</h3>
                    </div>
                    <Icon size={20} className={isActive ? 'text-lime' : 'text-muted'} />
                  </div>
                  <h4 className="mt-3 text-base font-medium text-foreground">{step.heading}</h4>
                  <p className="mt-1 text-sm text-muted leading-relaxed">{step.desc}</p>
                </motion.div>
              )
            })}
          </div>

          {/* Interactive Visual Display Right */}
          <div className="lg:col-span-6">
            <div className="border border-line rounded-2xl bg-panel p-8 min-h-[380px] flex flex-col justify-between relative overflow-hidden shadow-sm">
              <div className="flex items-center justify-between border-b border-line pb-4">
                <span className="text-xs font-mono uppercase tracking-widest text-muted">
                  Stage {steps[activeStep].num} Preview
                </span>
                <span className="text-xs font-mono text-lime font-semibold flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-lime animate-pulse" />
                  {steps[activeStep].signal}
                </span>
              </div>

              <AnimatePresence mode="wait">
                <motion.div
                  key={activeStep}
                  initial={{ opacity: 0, y: 15 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -15 }}
                  transition={{ duration: 0.3 }}
                  className="my-auto py-6"
                >
                  <div className="w-14 h-14 rounded-2xl bg-lime/10 border border-lime/30 flex items-center justify-center text-lime mb-6 shadow-xs">
                    {(() => {
                      const ActiveIcon = steps[activeStep].icon
                      return <ActiveIcon size={30} />
                    })()}
                  </div>

                  <h3 className="text-2xl font-semibold text-foreground">{steps[activeStep].heading}</h3>
                  <p className="mt-3 text-base text-muted leading-relaxed">{steps[activeStep].detail}</p>
                </motion.div>
              </AnimatePresence>

              <div className="border-t border-line pt-4 flex items-center justify-between">
                <div className="flex gap-2">
                  {steps.map((_, i) => (
                    <button
                      key={i}
                      onClick={() => setActiveStep(i)}
                      className={`h-2 rounded-full transition-all ${
                        activeStep === i ? 'w-8 bg-lime' : 'w-2 bg-line'
                      }`}
                      aria-label={`Go to step ${i + 1}`}
                    />
                  ))}
                </div>
                <span className="text-xs font-mono text-muted">Click any step to inspect</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export function AgentTypes() {
  const agents = [
    {
      title: 'AI Receptionist',
      tag: 'Front Desk & Routing',
      desc: 'Welcomes every caller, handles FAQs, qualifies intent, and routes to the exact department or teammate.',
      icon: Headphones,
      metrics: '100% Call Coverage',
    },
    {
      title: 'Sales Agent',
      tag: 'Inbound Qualification',
      desc: 'Engages prospects, answers pricing and capability questions, and books qualified meetings directly into calendars.',
      icon: Zap,
      metrics: '3x Booking Velocity',
    },
    {
      title: 'Support Agent',
      tag: 'Tier 1 Resolution',
      desc: 'Diagnoses repeat issues, checks order status, retrieves policy knowledge, and resolves support calls gracefully.',
      icon: ShieldCheck,
      metrics: '70% Instant Resolution',
    },
    {
      title: 'Appointment Agent',
      tag: 'Scheduling & Reminders',
      desc: 'Handles bookings, reschedules, cancellations, and sends automatic SMS confirmations without hold times.',
      icon: Calendar,
      metrics: 'Zero Scheduling Overhead',
    },
    {
      title: 'Lead Qualification',
      tag: 'Prospect Screening',
      desc: 'Asks structured qualifying questions, logs key data into CRM fields, and flags high-priority deals.',
      icon: UserCheck,
      metrics: 'Structured CRM Logging',
    },
    {
      title: 'Outbound Agent',
      tag: 'Permissioned Outreach',
      desc: 'Executes compliant follow-ups, re-engages dormant leads, and confirms appointment reminders at scale.',
      icon: Sparkles,
      metrics: 'Automated Campaign Reach',
    },
  ]

  return (
    <section className="section container" id="agents">
      <div className="section-heading">
        <p className="eyebrow">
          <span />
          Specialized Conversational AI
        </p>
        <h2>
          Give every conversation <em>an agent.</em>
        </h2>
        <p className="max-w-xl text-muted leading-7">
          Deploy specialized AI agents tailored to your business functions. Each agent operates with defined guardrails, knowledge, and execution rights.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mt-10">
        {agents.map((agent, i) => {
          const Icon = agent.icon
          return (
            <motion.div
              key={agent.title}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.4, delay: i * 0.08 }}
              whileHover={{ y: -5 }}
              className="p-6 rounded-2xl border border-line bg-card hover:border-lime hover:shadow-lg transition-all group flex flex-col justify-between"
            >
              <div>
                <div className="flex items-center justify-between mb-4">
                  <div className="w-10 h-10 rounded-xl bg-panel border border-line flex items-center justify-center text-lime group-hover:bg-lime group-hover:text-background transition-colors">
                    <Icon size={20} />
                  </div>
                  <span className="text-[11px] font-mono font-medium text-lime px-2.5 py-1 rounded-full bg-lime-soft/50 border border-lime/20">
                    {agent.tag}
                  </span>
                </div>

                <h3 className="text-xl font-semibold text-foreground tracking-tight group-hover:text-lime transition-colors">
                  {agent.title}
                </h3>
                <p className="mt-2 text-sm text-muted leading-relaxed">{agent.desc}</p>
              </div>

              <div className="mt-6 pt-4 border-t border-line/60 flex items-center justify-between">
                <span className="text-xs font-mono text-foreground/80 font-medium">{agent.metrics}</span>
                <a href="#demo" className="text-xs font-semibold text-lime flex items-center gap-1 opacity-80 group-hover:opacity-100">
                  Deploy agent <ArrowRight size={13} />
                </a>
              </div>
            </motion.div>
          )
        })}
      </div>
    </section>
  )
}
