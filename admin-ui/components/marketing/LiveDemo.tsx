'use client'

import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Mic, PhoneCall, Radio, RotateCcw, Volume2, Check, ArrowRight, ChevronDown } from 'lucide-react'
import { Waveform, SignalBackdrop } from './HeroTeaser'
import { Button, Logo } from './Navbar'

type CallState = 'idle' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'complete'

export function LiveDemo() {
  const [state, setState] = useState<CallState>('idle')
  const [seconds, setSeconds] = useState(0)

  const active = state !== 'idle' && state !== 'complete'

  useEffect(() => {
    let interval: NodeJS.Timeout
    if (active) {
      interval = setInterval(() => setSeconds((s) => s + 1), 1000)
    } else if (state === 'idle') {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSeconds(0)
    }
    return () => clearInterval(interval)
  }, [active, state])

  const startFullDemo = () => {
    if (active) return
    setState('connecting')
     
      setSeconds(0)

    setTimeout(() => setState('listening'), 1200)
    setTimeout(() => setState('thinking'), 3500)
    setTimeout(() => setState('speaking'), 5800)
    setTimeout(() => setState('complete'), 10000)
  }

  const reset = () => {
    setState('idle')
     
      setSeconds(0)
  }

  const formatTimer = (s: number) => {
    const mins = Math.floor(s / 60)
    const secs = s % 60
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
  }

  const fullTranscript = [
    { speaker: 'YUVIZ SYSTEM', text: 'Call stream connected to Yuviz Voice Gateway. Latency: 320ms.', time: '00:01' },
    { speaker: 'MAYA (AI AGENT)', text: '“Hello! Thank you for calling Yuviz AI. I am Maya, your AI receptionist. How can I help you today?”', time: '00:03' },
    { speaker: 'CALLER', text: '“Hi Maya! I would like to know if Yuviz integrates with Google Calendar and HubSpot CRM.”', time: '00:06' },
    { speaker: 'MAYA (AI AGENT)', text: '“Yes, absolutely! Yuviz has native two-way sync with Google Calendar and HubSpot CRM so appointments and lead notes sync automatically.”', time: '00:09' },
  ]

  return (
    <section className="section showcase bg-card/60 border-y border-line" id="demo">
      <div className="container">
        <div className="section-heading text-center max-w-2xl mx-auto">
          <p className="eyebrow justify-center">
            <span />
            Full Voice Playground
          </p>
          <h2 className="text-4xl md:text-5xl lg:text-6xl font-semibold tracking-tight text-foreground">
            Don&apos;t take our word for it. <em>Talk to Yuviz.</em>
          </h2>
          <p className="text-muted leading-7">
            Experience an interactive voice agent conversation in action. Test speech cadence, knowledge retrieval, and real-time turn-taking.
          </p>
        </div>

        {/* Large Dedicated Voice Agent Card */}
        <div className="mt-12 max-w-4xl mx-auto border border-line rounded-2xl bg-panel p-8 shadow-xl">
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-center">
            {/* Agent Profile & Waveform Left */}
            <div className="lg:col-span-6 text-center border-b lg:border-b-0 lg:border-r border-line pb-8 lg:pb-0 lg:pr-8">
              <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-card border border-line text-xs font-mono text-lime font-semibold mb-6">
                <span className={`w-2 h-2 rounded-full ${active ? 'bg-lime animate-ping' : 'bg-lime'}`} />
                Maya &bull; AI Voice Agent &bull; Online
              </div>

              <div className={`voice-orb ${active ? 'orb-active' : ''} mx-auto my-4`}>
                <Radio size={36} strokeWidth={1.5} />
                <span className="orb-ring" />
              </div>

              <h3 className="text-2xl font-semibold text-foreground mt-4">Maya</h3>
              <p className="text-xs font-mono text-muted uppercase tracking-wider">Conversation Mode: Natural &bull; Knowledge Connected</p>

              <div className="mt-6 w-full px-4">
                <Waveform active={active} intensity={state === 'speaking' ? 1.6 : 1} />
              </div>

              <div className="mt-6 flex items-center justify-center gap-4">
                <span className="font-mono text-xs font-bold text-lime bg-lime-soft/60 px-3 py-1 rounded-full border border-lime/30">
                  Timer: {formatTimer(seconds)}
                </span>
              </div>

              <div className="mt-6 flex justify-center">
                {state === 'complete' ? (
                  <button
                    onClick={reset}
                    className="inline-flex items-center gap-2 px-6 py-3 rounded-full bg-foreground text-background text-sm font-semibold hover:opacity-90 transition-all cursor-pointer"
                  >
                    <RotateCcw size={16} /> Reset Conversation
                  </button>
                ) : (
                  <button
                    onClick={startFullDemo}
                    disabled={active}
                    className="inline-flex items-center gap-2.5 px-8 py-3.5 rounded-full bg-lime text-background text-sm font-semibold hover:opacity-90 transition-all shadow-md disabled:opacity-50 cursor-pointer"
                  >
                    <Mic size={18} />
                    {state === 'idle' ? 'Talk to Yuviz Agent' : state[0].toUpperCase() + state.slice(1) + '...'}
                  </button>
                )}
              </div>
            </div>

            {/* Realtime Live Transcript Right */}
            <div className="lg:col-span-6 flex flex-col justify-between min-h-[360px]">
              <div>
                <div className="flex items-center justify-between border-b border-line pb-3 mb-4">
                  <span className="text-xs font-mono uppercase text-muted">Live Conversation Transcript</span>
                  <span className="text-xs font-mono text-lime">HD Audio Simulation</span>
                </div>

                <div className="space-y-3 font-mono text-xs max-h-[260px] overflow-y-auto pr-2">
                  <AnimatePresence>
                    {active || state === 'complete' ? (
                      fullTranscript.map((t, idx) => (
                        <motion.div
                          key={idx}
                          initial={{ opacity: 0, y: 6 }}
                          animate={{ opacity: 1, y: 0 }}
                          transition={{ duration: 0.3, delay: idx * 0.4 }}
                          className="p-3 rounded-lg border border-line bg-card"
                        >
                          <div className="flex justify-between text-[10px] text-muted mb-1 font-bold">
                            <span className={t.speaker.includes('MAYA') ? 'text-lime' : 'text-foreground'}>{t.speaker}</span>
                            <span>{t.time}</span>
                          </div>
                          <p className="text-foreground/90 font-sans text-xs leading-relaxed">{t.text}</p>
                        </motion.div>
                      ))
                    ) : (
                      <p className="text-xs text-muted/70 italic text-center py-12 font-sans">
                        Press &quot;Talk to Yuviz Agent&quot; to begin the voice interaction simulation.
                      </p>
                    )}
                  </AnimatePresence>
                </div>
              </div>

              <div className="border-t border-line pt-4 flex items-center justify-between text-xs font-mono text-muted">
                <span>Audio Codec: OPUS 24kHz</span>
                <span className="text-lime">Sub-350ms Latency</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export function FinalCTA() {
  return (
    <section className="cta-section" id="contact">
      <div className="container relative cta-layout">
        <SignalBackdrop />
        <div>
          <p className="eyebrow">
            <span />
            Get Started Today
          </p>
          <h2>
            Your next customer is<br />
            <em>already calling.</em>
          </h2>
          <p className="max-w-md text-muted leading-7">
            Make sure someone is there to answer. Deploy an intelligent AI voice agent for your business in less than an hour.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Button href="#demo">
              Talk to Yuviz <ArrowRight size={16} />
            </Button>
            <Button href="mailto:hello@yuviz.ai" variant="quiet">
              Talk to our team
            </Button>
          </div>
        </div>
      </div>
    </section>
  )
}

export function FooterGuide() {
  const [open, setOpen] = useState<string | null>(null)
  const topics = [
    { id: 'start', label: 'Where should I start?', text: 'Start with one high-volume conversation: scheduling, support, or lead qualification. Prove the handoff, then expand.' },
    { id: 'trust', label: 'How does Yuviz stay reliable?', text: 'Every agent has defined knowledge, explicit action boundaries, and a human handoff path when context matters.' },
    { id: 'fit', label: 'Is this right for my team?', text: 'If your team spends time answering repeat questions or coordinating next steps, Yuviz can help move that work forward.' },
  ]
  return (
    <div className="footer-guide" aria-label="Yuviz quick guide">
      <div>
        <p className="footer-guide-kicker">A clearer place to start</p>
        <h3>Questions before the first call?</h3>
        <p>Learn how to choose a useful first workflow and make the rollout feel considered.</p>
      </div>
      <div className="footer-guide-list">
        {topics.map((topic) => (
          <div className={`footer-guide-item ${open === topic.id ? 'is-open' : ''}`} key={topic.id}>
            <button
              type="button"
              aria-expanded={open === topic.id}
              onClick={() => setOpen(open === topic.id ? null : topic.id)}
            >
              <span>{topic.label}</span>
              <ChevronDown size={16} />
            </button>
            {open === topic.id && <p>{topic.text}</p>}
          </div>
        ))}
      </div>
    </div>
  )
}

export function Footer() {
  return (
    <footer className="container footer">
      <FooterGuide />
      <div className="footer-main">
        <div>
          <Logo />
          <p className="mt-4 max-w-xs text-sm leading-6 text-muted">
            AI voice agents for real conversations.
          </p>
        </div>
        <div className="footer-links">
          <div>
            <p>Explore</p>
            <a href="#product">Product</a>
            <a href="#agents">Agents</a>
            <a href="#how-it-works">How it works</a>
            <a href="#demo">Voice Demo</a>
          </div>
          <div>
            <p>Company</p>
            <a href="#contact">Contact</a>
            <a href="#contact">Careers</a>
            <a href="#contact">Privacy</a>
          </div>
        </div>
      </div>
      <div className="footer-bottom">
        <span>&copy; 2026 Yuviz AI</span>
        <span>Built for better conversations.</span>
      </div>
    </footer>
  )
}
