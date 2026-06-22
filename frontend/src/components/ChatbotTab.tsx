'use client'

import { useState, useRef, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Send, Bot, User, Sparkles, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import type { ChatMessage } from '@/lib/types'

const SUGGESTIONS = [
  'Rapport de ce mois',
  'Combien de gallons de Diesel cette semaine ?',
  'Quelle pompe a vendu le plus ce mois ?',
  'Rapport du 1er au 15 juin 2026',
]

export default function ChatbotTab() {
  const [messages, setMessages]     = useState<ChatMessage[]>([])
  const [input, setInput]           = useState('')
  const [loading, setLoading]       = useState(false)
  const [historique, setHistorique] = useState<ChatMessage[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLTextAreaElement>(null)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, loading])

  const send = async (msg?: string) => {
    const text = (msg ?? input).trim()
    if (!text || loading) return
    setInput('')
    setMessages(p => [...p, { role: 'user', content: text }])
    setLoading(true)
    try {
      const res = await api.chat(text, historique)
      setHistorique(res.historique)
      setMessages(p => [...p, { role: 'assistant', content: res.reponse }])
    } catch (e: unknown) {
      setMessages(p => [...p, {
        role: 'assistant',
        content: `⚠ Erreur : ${e instanceof Error ? e.message : 'inconnue'}`,
      }])
    } finally {
      setLoading(false)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }

  const handleKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
  }

  const clear = () => { setMessages([]); setHistorique([]) }

  return (
    <div className="flex flex-col" style={{ height: '560px' }}>

      {/* ── Toolbar ── */}
      <div className="flex items-center justify-between pb-3 mb-3 flex-shrink-0"
        style={{ borderBottom: '1px solid var(--b1)' }}>
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-xl flex items-center justify-center"
            style={{ background: 'rgba(8,145,178,0.1)' }}>
            <Bot size={15} style={{ color: 'var(--teal)' }} />
          </div>
          <div>
            <p className="text-[13px] font-bold" style={{ color: 'var(--t0)' }}>Assistant IA</p>
            <p className="text-[10px]" style={{ color: 'var(--t2)' }}>claude-sonnet · base de données réelle</p>
          </div>
        </div>
        {messages.length > 0 && (
          <motion.button whileHover={{ scale: 1.06 }} whileTap={{ scale: 0.94 }}
            onClick={clear} title="Effacer la conversation"
            className="w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
            style={{ background: 'var(--c2)', border: '1px solid var(--b1)', color: 'var(--t2)' }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.color = 'var(--red)'; (e.currentTarget as HTMLElement).style.borderColor = 'rgba(220,38,38,0.3)' }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.color = 'var(--t2)'; (e.currentTarget as HTMLElement).style.borderColor = 'var(--b1)' }}>
            <Trash2 size={13} />
          </motion.button>
        )}
      </div>

      {/* ── Messages ── */}
      <div className="flex-1 overflow-y-auto space-y-3 pr-1"
        style={{ scrollbarWidth: 'thin', scrollbarColor: 'var(--b1) transparent' }}>

        {/* Welcome */}
        <AnimatePresence>
          {messages.length === 0 && (
            <motion.div key="welcome"
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              className="flex flex-col items-center justify-center gap-5 text-center"
              style={{ minHeight: '320px' }}>
              <motion.div
                animate={{ y: [0, -4, 0] }}
                transition={{ duration: 3, repeat: Infinity, ease: 'easeInOut' }}
                initial={{ scale: 0 }} whileInView={{ scale: 1 }}
                className="w-16 h-16 rounded-2xl flex items-center justify-center"
                style={{ background: 'rgba(8,145,178,0.08)', border: '1px solid rgba(8,145,178,0.2)' }}>
                <Sparkles size={32} style={{ color: 'var(--teal)' }} />
              </motion.div>
              <div>
                <p className="font-bold text-base" style={{ color: 'var(--t0)' }}>Rapports en langage naturel</p>
                <p className="text-sm mt-1.5 max-w-xs" style={{ color: 'var(--t1)' }}>
                  Posez vos questions sur les ventes. L&apos;IA interroge la vraie base de données.
                </p>
              </div>
              <div className="flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map(s => (
                  <motion.button key={s} whileHover={{ scale: 1.03 }} whileTap={{ scale: 0.97 }}
                    onClick={() => send(s)}
                    className="text-xs px-3.5 py-1.5 rounded-full transition-colors"
                    style={{ background: 'var(--c2)', border: '1px solid var(--b1)', color: 'var(--t1)' }}
                    onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--blue2)'; (e.currentTarget as HTMLElement).style.color = 'var(--blue)' }}
                    onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--b1)'; (e.currentTarget as HTMLElement).style.color = 'var(--t1)' }}>
                    {s}
                  </motion.button>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Bubbles */}
        {messages.map((m, i) => (
          <motion.div key={i}
            initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.18 }}
            className={`flex gap-2.5 ${m.role === 'user' ? 'flex-row-reverse' : ''}`}>
            <div className="w-7 h-7 rounded-xl flex-shrink-0 flex items-center justify-center mt-0.5 font-bold text-white text-[11px]"
              style={{
                background: m.role === 'user'
                  ? 'var(--blue)'
                  : 'linear-gradient(135deg, var(--teal3), var(--teal))',
              }}>
              {m.role === 'user' ? <User size={12} /> : <Bot size={12} />}
            </div>
            <div className={`max-w-[78%] px-4 py-2.5 text-[13px] leading-relaxed whitespace-pre-wrap break-words ${m.role === 'user' ? 'rounded-2xl rounded-tr-sm' : 'rounded-2xl rounded-tl-sm'}`}
              style={m.role === 'user'
                ? { background: 'var(--blue)', color: 'white' }
                : { background: 'var(--c2)', color: 'var(--t0)', border: '1px solid var(--b1)' }}>
              {m.content}
            </div>
          </motion.div>
        ))}

        {/* Typing indicator */}
        <AnimatePresence>
          {loading && (
            <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
              className="flex gap-2.5">
              <div className="w-7 h-7 rounded-xl flex-shrink-0 flex items-center justify-center text-white"
                style={{ background: 'linear-gradient(135deg, var(--teal3), var(--teal))' }}>
                <Bot size={12} />
              </div>
              <div className="px-4 py-3 rounded-2xl rounded-tl-sm flex items-center gap-1.5"
                style={{ background: 'var(--c2)', border: '1px solid var(--b1)' }}>
                {[0, 0.15, 0.3].map((delay, j) => (
                  <motion.span key={j} className="w-1.5 h-1.5 rounded-full"
                    style={{ background: 'var(--teal)' }}
                    animate={{ y: [0, -5, 0], opacity: [0.5, 1, 0.5] }}
                    transition={{ duration: 0.75, repeat: Infinity, delay }} />
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <div ref={bottomRef} />
      </div>

      {/* ── Input bar ── */}
      <div className="flex-shrink-0 pt-3" style={{ borderTop: '1px solid var(--b1)' }}>
        <div className="flex items-end gap-2 rounded-xl px-3 py-2"
          style={{ background: 'var(--c2)', border: '1px solid var(--b1)' }}
          onFocus={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--blue2)'}
          onBlur={e => (e.currentTarget as HTMLElement).style.borderColor = 'var(--b1)'}>
          <textarea ref={inputRef} value={input}
            onChange={e => setInput(e.target.value)} onKeyDown={handleKey}
            placeholder="Votre question… (Entrée pour envoyer, Maj+Entrée pour nouvelle ligne)"
            rows={1} disabled={loading}
            className="flex-1 bg-transparent outline-none resize-none text-[13px] leading-relaxed"
            style={{ color: 'var(--t0)', maxHeight: '120px', scrollbarWidth: 'thin' }} />
          <motion.button whileHover={{ scale: 1.06 }} whileTap={{ scale: 0.94 }}
            onClick={() => send()} disabled={!input.trim() || loading}
            className="flex-shrink-0 w-9 h-9 rounded-xl flex items-center justify-center font-bold disabled:opacity-30 text-white"
            style={{ background: 'var(--blue)' }}>
            <Send size={14} />
          </motion.button>
        </div>
      </div>
    </div>
  )
}
