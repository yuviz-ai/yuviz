'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageSquare, ArrowRight, Database, Check, RefreshCw, Cpu, CheckCircle } from 'lucide-react'

export function ConversationVisualizer() {
  const [activeStep, setActiveStep] = useState(0)

  const timeline = [
    {
      caller: '“Can I change my appointment to Friday?”',
      agentResponse: '“Of course. Let me check our availability for Friday.”',
      systemState: 'Querying Calendar API',
      action: 'Slot search: Friday 2:30 PM & 4:00 PM open',
      status: 'In Progress',
    },
    {
      caller: '“Friday at 2:30 PM works great for me.”',
      agentResponse: '“I have updated your appointment to Friday at 2:30 PM with Dr. Sarah. I just sent a confirmation text.”',
      systemState: 'Executing Reschedule & Twilio SMS',
      action: 'Appointment ID #9081 updated in EHR',
      status: 'Action Completed',
    },
    {
      caller: '“Awesome, thanks for your help!”',
      agentResponse: '“You are very welcome! Have a wonderful day.”',
      systemState: 'Call Wrap-up & Audit Logged',
      action: 'Call summary saved & sentiment score 0.98',
      status: 'Conversation Resolved',
    },
  ]

  return (
    <section className="section bg-card/40 border-y border-line" id="conversation-visualizer">
      <div className="container">
        <div className="section-heading">
          <p className="eyebrow">
            <span />
            Interactive Call Pipeline
          </p>
          <h2>
            Watch a conversation <em>unfold.</em>
          </h2>
          <p className="max-w-xl text-muted leading-7">
            Yuviz goes beyond speech. Watch how spoken requests trigger instant knowledge lookups, API actions, and verified resolutions.
          </p>
        </div>

        {/* Visualization Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 mt-10 items-center">
          {/* Conversation Transcript Stream Left */}
          <div className="lg:col-span-6 space-y-4">
            {timeline.map((item, idx) => (
              <div
                key={idx}
                onClick={() => setActiveStep(idx)}
                className={`p-5 rounded-2xl border transition-all cursor-pointer ${
                  activeStep === idx
                    ? 'border-lime bg-card shadow-md ring-1 ring-lime/20'
                    : 'border-line bg-panel/60 hover:bg-card/70'
                }`}
              >
                <div className="flex items-center justify-between text-xs font-mono mb-2">
                  <span className="text-muted">Turn 0{idx + 1}</span>
                  <span className={`px-2 py-0.5 rounded font-semibold ${activeStep === idx ? 'bg-lime text-background' : 'bg-line text-muted'}`}>
                    {item.status}
                  </span>
                </div>

                <div className="space-y-2">
                  <div className="flex items-start gap-2">
                    <span className="text-[10px] font-mono font-bold text-muted bg-panel px-1.5 py-0.5 rounded mt-0.5">CALLER</span>
                    <p className="text-sm font-medium text-foreground">{item.caller}</p>
                  </div>
                  <div className="flex items-start gap-2">
                    <span className="text-[10px] font-mono font-bold text-lime bg-lime-soft/60 px-1.5 py-0.5 rounded mt-0.5">YUVIZ</span>
                    <p className="text-sm text-foreground/90 leading-relaxed">{item.agentResponse}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Realtime Backend System Execution Display Right */}
          <div className="lg:col-span-6">
            <div className="border border-line rounded-2xl bg-panel p-8 min-h-[380px] flex flex-col justify-between shadow-sm relative overflow-hidden">
              <div className="flex items-center justify-between border-b border-line pb-4">
                <span className="text-xs font-mono uppercase tracking-widest text-muted">Backend Signal Monitor</span>
                <span className="text-xs font-mono text-lime flex items-center gap-1">
                  <span className="w-2 h-2 rounded-full bg-lime animate-ping" /> Synchronized
                </span>
              </div>

              <AnimatePresence mode="wait">
                <motion.div
                  key={activeStep}
                  initial={{ opacity: 0, scale: 0.97 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.97 }}
                  transition={{ duration: 0.3 }}
                  className="my-auto space-y-6 py-4"
                >
                  <div className="p-4 rounded-xl border border-line bg-card">
                    <span className="text-[11px] font-mono uppercase text-muted block mb-1">State Machine</span>
                    <p className="text-lg font-semibold text-foreground flex items-center gap-2">
                      <Cpu size={18} className="text-lime" />
                      {timeline[activeStep].systemState}
                    </p>
                  </div>

                  <div className="p-4 rounded-xl border border-lime/40 bg-lime-soft/20">
                    <span className="text-[11px] font-mono uppercase text-lime font-bold block mb-1">Executed Tool Action</span>
                    <p className="text-sm font-mono text-foreground font-medium flex items-center gap-2">
                      <CheckCircle size={16} className="text-lime" />
                      {timeline[activeStep].action}
                    </p>
                  </div>
                </motion.div>
              </AnimatePresence>

              <div className="border-t border-line pt-4 flex items-center justify-between text-xs font-mono text-muted">
                <span>Node: Voice Gateway &bull; Latency &lt;350ms</span>
                <span className="text-lime">Step {activeStep + 1} of 3</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

export function KnowledgeSection() {
  const [selectedDoc, setSelectedDoc] = useState(0)

  const docs = [
    {
      name: 'Return_Policy_2026.pdf',
      type: 'Policy Document',
      excerpt: 'Customers are eligible for a 100% full refund within 30 days of purchase upon providing order ID.',
      question: '“What is your refund policy?”',
      answer: '“You can request a full refund within 30 days of your purchase by providing your order number.”',
    },
    {
      name: 'Pricing_Plans.docx',
      type: 'Product Specs',
      excerpt: 'Starter plan is $49/mo including 500 voice minutes. Pro plan is $199/mo including unlimited seats.',
      question: '“How much is the Starter plan?”',
      answer: '“Our Starter plan is $49 per month and includes 500 voice minutes.”',
    },
    {
      name: 'Clinic_Hours_FAQ.txt',
      type: 'Operational FAQ',
      excerpt: 'We are open Monday through Friday from 8:00 AM to 6:00 PM, and Saturdays from 9:00 AM to 1:00 PM.',
      question: '“Are you open on Saturdays?”',
      answer: '“Yes, we are open on Saturdays from 9:00 AM to 1:00 PM.”',
    },
  ]

  return (
    <section className="section container" id="knowledge">
      <div className="section-heading">
        <p className="eyebrow">
          <span />
          Grounded RAG Knowledge
        </p>
        <h2>
          Your agent knows <em>your business.</em>
        </h2>
        <p className="max-w-xl text-muted leading-7">
          No hallucinated answers or generic AI scripts. Yuviz grounds every spoken response directly in your verified business files, FAQs, and documentation.
        </p>
      </div>

      {/* Interactive RAG Playground */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 mt-10 items-center">
        {/* Knowledge Source Selection Left */}
        <div className="lg:col-span-5 space-y-4">
          <p className="text-xs font-mono uppercase tracking-widest text-muted mb-2">Connected Knowledge Files</p>
          {docs.map((doc, idx) => (
            <div
              key={doc.name}
              onClick={() => setSelectedDoc(idx)}
              className={`p-4 rounded-xl border transition-all cursor-pointer ${
                selectedDoc === idx
                  ? 'border-lime bg-lime-soft/30 shadow-xs'
                  : 'border-line bg-panel hover:bg-card'
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-mono font-bold text-lime">{doc.type}</span>
                <span className="text-[10px] font-mono text-muted">RAG Synced</span>
              </div>
              <p className="text-sm font-semibold text-foreground">{doc.name}</p>
              <p className="text-xs text-muted mt-1 line-clamp-2">{doc.excerpt}</p>
            </div>
          ))}
        </div>

        {/* Live Grounded Response Right */}
        <div className="lg:col-span-7 border border-line rounded-2xl bg-panel p-8 min-h-[360px] flex flex-col justify-between shadow-sm">
          <div className="flex items-center justify-between border-b border-line pb-3">
            <span className="text-xs font-mono uppercase tracking-widest text-muted">Knowledge-Grounded QA Test</span>
            <span className="text-xs font-mono text-lime font-semibold">100% Grounded</span>
          </div>

          <div className="my-auto space-y-4">
            <div className="p-4 rounded-xl bg-card border border-line">
              <span className="text-[10px] font-mono uppercase text-muted font-bold block mb-1">Customer Question</span>
              <p className="text-base font-medium text-foreground">{docs[selectedDoc].question}</p>
            </div>

            <div className="p-4 rounded-xl bg-lime-soft/30 border border-lime/40">
              <span className="text-[10px] font-mono uppercase text-lime font-bold block mb-1">Yuviz Agent Grounded Answer</span>
              <p className="text-base text-foreground leading-relaxed font-medium">{docs[selectedDoc].answer}</p>
            </div>
          </div>

          <div className="border-t border-line pt-4 flex items-center justify-between text-xs font-mono text-muted">
            <span>Ground Truth File: {docs[selectedDoc].name}</span>
            <span className="text-lime">Zero Hallucination Guarantee</span>
          </div>
        </div>
      </div>
    </section>
  )
}
