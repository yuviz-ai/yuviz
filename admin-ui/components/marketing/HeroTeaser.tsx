'use client'

import { useState, useEffect } from 'react'
import { ArrowRight, Check, Mic, Play, Radio, RotateCcw } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { Button } from './Navbar'

type VoiceState = 'idle' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'complete'

const transcriptSnippets: Record<VoiceState, { speaker: string; text: string } | null> = {
  idle: null,
  connecting: { speaker: 'YUVIZ SYSTEM', text: 'Initializing secure Voice Stream...' },
  listening: { speaker: 'USER', text: '“Hi Yuviz, can you check my appointment status for tomorrow?”' },
  thinking: { speaker: 'YUVIZ AGENT', text: 'Querying calendar schedule & verifying customer ID...' },
  speaking: { speaker: 'YUVIZ AGENT', text: '“You are set for tomorrow at 2:30 PM with Dr. Sarah. Would you like me to send a SMS confirmation?”' },
  complete: { speaker: 'SUMMARY', text: 'Call resolved in 14s. Confirmation SMS dispatched.' },
}

export function Waveform({ active = false, intensity = 1 }: { active?: boolean; intensity?: number }) {
  return (
    <div className={`waveform ${active ? 'is-active' : ''}`} aria-hidden="true">
      {Array.from({ length: 34 }, (_, i) => {
        const heightPercent = 12 + ((i * 17) % 34) * intensity
        return <i key={i} style={{ height: `${Math.min(95, heightPercent)}%` }} />
      })}
    </div>
  )
}

export function SignalBackdrop() {
  return (
    <div className="signal-backdrop" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
    </div>
  )
}

export function HeroTeaserCard() {
  const [state, setState] = useState<VoiceState>('idle')
  const [seconds, setSeconds] = useState(0)

  const active = state !== 'idle' && state !== 'complete'

  useEffect(() => {
    let interval: NodeJS.Timeout
    if (active) {
      interval = setInterval(() => {
        setSeconds((prev) => prev + 1)
      }, 1000)
    } else if (state === 'idle') {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSeconds(0)
    }
    return () => clearInterval(interval)
  }, [active, state])

  const startSimulation = () => {
    if (active) return
    setState('connecting')
     
      setSeconds(0)

    setTimeout(() => setState('listening'), 1200)
    setTimeout(() => setState('thinking'), 3200)
    setTimeout(() => setState('speaking'), 5200)
    setTimeout(() => setState('complete'), 9000)
  }

  const reset = () => {
    setState('idle')
     
      setSeconds(0)
  }

  const formatTime = (totalSeconds: number) => {
    const mins = Math.floor(totalSeconds / 60)
    const secs = totalSeconds % 60
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`
  }

  const stateLabels: Record<VoiceState, string> = {
    idle: 'Ready when you are',
    connecting: 'Connecting to voice gateway...',
    listening: 'Listening to caller...',
    thinking: 'Understanding intent...',
    speaking: 'Maya is speaking...',
    complete: 'Conversation complete',
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, delay: 0.1 }}
      className="voice-card"
      id="hero-demo"
    >
      {/* Header Bar */}
      <div className="flex items-center justify-between border-b border-line pb-4 mb-4">
        <div className="flex items-center gap-2 text-xs uppercase tracking-[0.18em] text-muted">
          <span className={`live-dot ${active ? 'pulse' : ''}`} />
          {active ? 'Interactive Simulation' : 'Yuviz Voice Agent Teaser'}
        </div>
        <span className="font-mono text-xs font-semibold text-lime px-2 py-0.5 rounded bg-lime-soft/40 border border-lime/20">
          {formatTime(seconds)}
        </span>
      </div>

      {/* Main Agent Orb & Waveform */}
      <div className="voice-center">
        <div className={`voice-orb ${active ? 'orb-active' : ''}`}>
          <Radio size={28} strokeWidth={1.5} />
          <span className="orb-ring" />
        </div>

        <p className="mt-5 text-xl font-medium text-foreground">Maya</p>
        <p className="mt-0.5 text-xs font-mono uppercase tracking-wider text-muted">AI Voice Receptionist</p>

        <div className="mt-6 w-full px-2">
          <Waveform active={active} intensity={state === 'speaking' ? 1.5 : 1} />
        </div>

        {/* Status Pill */}
        <div className="mt-5 inline-flex items-center gap-2 px-3 py-1 rounded-full bg-panel border border-line text-xs font-medium text-lime">
          <span className={`w-2 h-2 rounded-full ${active ? 'bg-lime animate-ping' : 'bg-muted'}`} />
          {stateLabels[state]}
        </div>

        {/* Live Transcript Snippet Card */}
        <div className="mt-6 min-h-[64px] rounded-xl border border-line bg-card p-3 text-left">
          <AnimatePresence mode="wait">
            {transcriptSnippets[state] ? (
              <motion.div
                key={state}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.2 }}
              >
                <span className="block text-[10px] font-mono font-semibold uppercase tracking-wider text-lime mb-1">
                  {transcriptSnippets[state]?.speaker}
                </span>
                <p className="text-xs text-foreground/90 leading-relaxed">
                  {transcriptSnippets[state]?.text}
                </p>
              </motion.div>
            ) : (
              <motion.p
                key="idle-text"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="text-xs text-muted/70 italic text-center py-2"
              >
                Click &quot;Talk to Yuviz&quot; to test a real voice conversation flow.
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* Action Footer */}
      <div className="mt-6 flex items-center justify-between border-t border-line pt-4">
        <span className="text-xs text-muted font-mono">Simulated Voice Pipeline</span>
        {state === 'complete' ? (
          <button
            onClick={reset}
            className="inline-flex items-center gap-1.5 text-xs text-foreground font-semibold hover:text-lime transition-colors"
          >
            <RotateCcw size={13} /> Reset Demo
          </button>
        ) : (
          <button
            onClick={startSimulation}
            disabled={active}
            className="flex items-center gap-2 rounded-full bg-lime px-4 py-2 text-xs font-semibold text-background hover:opacity-90 transition-all disabled:opacity-50 cursor-pointer"
          >
            <Mic size={14} />
            {state === 'idle' ? 'Talk to Yuviz' : stateLabels[state]}
          </button>
        )}
      </div>
    </motion.div>
  )
}

export function HeroSection() {
  return (
    <section className="hero container" id="hero">
      <SignalBackdrop />
      
      <motion.div
        initial={{ opacity: 0, x: -20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.6 }}
        className="hero-copy"
      >
        <p className="eyebrow">
          <span />
          AI voice agents for real conversations
        </p>

        <h1>
          Let AI handle
          <br />
          <em>the conversation.</em>
        </h1>

        <p className="hero-sub">
          Yuviz equips your business with intelligent AI voice agents that answer inbound calls, engage outbound leads, retrieve knowledge, and execute business actions — 24/7.
        </p>

        <div className="flex flex-wrap gap-3">
          <Button href="#demo">
            Talk to Yuviz <ArrowRight size={16} />
          </Button>
          <Button variant="quiet" href="#how-it-works">
            <Play size={15} /> See how it works
          </Button>
        </div>

        <div className="hero-note mt-6">
          <span className="flex items-center gap-1.5 font-medium text-xs text-muted">
            <Check size={14} className="text-lime" /> Zero hold time
          </span>
          <span className="text-muted/40">•</span>
          <span className="flex items-center gap-1.5 font-medium text-xs text-muted">
            <Check size={14} className="text-lime" /> Natural human pause handling
          </span>
          <span className="text-muted/40">•</span>
          <span className="flex items-center gap-1.5 font-medium text-xs text-muted">
            <Check size={14} className="text-lime" /> Human escalation path
          </span>
        </div>
      </motion.div>

      <HeroTeaserCard />
    </section>
  )
}
