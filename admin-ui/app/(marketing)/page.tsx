'use client'

import { Navbar } from '@/components/marketing/Navbar'
import { HeroSection } from '@/components/marketing/HeroTeaser'
import { TrustStrip, WhatIsYuviz } from '@/components/marketing/WhatIsYuviz'
import { WhyVoice, AgentTypes } from '@/components/marketing/WhyVoice'
import { AgentBuilderVisual } from '@/components/marketing/AgentBuilderVisual'
import { ProductShowcase, HowItWorks } from '@/components/marketing/ProductShowcase'
import { ConversationVisualizer, KnowledgeSection } from '@/components/marketing/ConversationVisualizer'
import { InboundOutbound, UseCaseMatrix } from '@/components/marketing/InboundOutbound'
import { BeforeAfter, ArchitectureSection } from '@/components/marketing/ArchitectureSection'
import { TranscriptDemo, Integrations } from '@/components/marketing/TranscriptDemo'
import { HumanHandoff, SecuritySection, WhyYuviz } from '@/components/marketing/HumanHandoff'
import { LiveDemo, FinalCTA, Footer } from '@/components/marketing/LiveDemo'

export default function Page() {
  return (
    <main id="top" className="min-h-screen bg-background text-foreground antialiased selection:bg-lime selection:text-ink">
      {/* 1. Sticky Translucent Navbar */}
      <Navbar />

      {/* 2. Preserved Hero + Interactive Voice Agent Teaser */}
      <HeroSection />

      {/* 3. Capability & Trust Strip */}
      <TrustStrip />

      {/* 4. What is Yuviz? ("Meet your AI voice team") */}
      <WhatIsYuviz />

      {/* 5. Why Voice? ("Some conversations are better spoken") */}
      <WhyVoice />

      {/* 6. AI Agent Types Grid ("Give every conversation an agent") */}
      <AgentTypes />

      {/* 7. Interactive Agent Builder Mockup */}
      <AgentBuilderVisual />

      {/* 8. Control Room Dashboard ("See every conversation clearly") */}
      <ProductShowcase />

      {/* 9. End-to-End Workflow ("From hello to done") */}
      <HowItWorks />

      {/* 10. Interactive Conversation Visualizer */}
      <ConversationVisualizer />

      {/* 11. Grounded RAG Knowledge Base */}
      <KnowledgeSection />

      {/* 12. Inbound vs Outbound Dual Telephony & Campaigns */}
      <InboundOutbound />

      {/* 13. Use Case Matrix */}
      <UseCaseMatrix />

      {/* 14. Before vs After Interactive Comparison */}
      <BeforeAfter />

      {/* 15. Real-Time Telephony Infrastructure Architecture */}
      <ArchitectureSection />

      {/* 16. Call Intelligence & Transcript Inspector */}
      <TranscriptDemo />

      {/* 17. Contextual Human Escalation & Handoff */}
      <HumanHandoff />

      {/* 18. Security & Data Protection */}
      <SecuritySection />

      {/* 19. Integration Ecosystem */}
      <Integrations />

      {/* 20. Core Principles ("Built around the conversation") */}
      <WhyYuviz />

      {/* 21. Full Voice Agent Playground / Live Demo */}
      <LiveDemo />

      {/* 22. High-Impact Final CTA */}
      <FinalCTA />

      {/* 23. Footer with Interactive Q&A Accordion */}
      <Footer />
    </main>
  )
}
