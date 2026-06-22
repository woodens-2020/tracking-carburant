'use client'

import { useState, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Fuel, BarChart3, AlertTriangle, Bot, Settings,
  Wifi, WifiOff, Menu, ChevronRight, TrendingUp,
} from 'lucide-react'
import { api } from '@/lib/api'
import type { Produit, Rapport } from '@/lib/types'
import RelevesTab   from '@/components/RelevesTab'
import RapportTab   from '@/components/RapportTab'
import AnomaliesTab from '@/components/AnomaliesTab'
import ChatbotTab   from '@/components/ChatbotTab'
import GestionModal from '@/components/GestionModal'

type Tab = 'releves' | 'rapport' | 'anomalies' | 'chatbot'

const NAV: { section: string; items: { id: Tab; label: string; Icon: React.ElementType }[] }[] = [
  {
    section: 'SUIVI CARBURANT',
    items: [
      { id: 'releves',   label: 'Relevés',     Icon: Fuel },
      { id: 'rapport',   label: 'Rapport',     Icon: BarChart3 },
      { id: 'anomalies', label: 'Anomalies',   Icon: AlertTriangle },
    ],
  },
  {
    section: 'OUTILS',
    items: [
      { id: 'chatbot', label: 'Assistant IA', Icon: Bot },
    ],
  },
]

const PAGE_TITLE: Record<Tab, string> = {
  releves:   'Relevés du jour',
  rapport:   'Rapport & Statistiques',
  anomalies: 'Détection des anomalies',
  chatbot:   'Assistant IA',
}

const PAGE_DESC: Record<Tab, string> = {
  releves:   'Saisie des lectures de compteurs par période',
  rapport:   'Analyse des ventes sur une période donnée',
  anomalies: 'Vérification de la cohérence des données',
  chatbot:   'Interrogez la base de données en langage naturel',
}

export default function Page() {
  const today = new Date().toISOString().slice(0, 10)
  const [date, setDate]             = useState(today)
  const [activeTab, setActiveTab]   = useState<Tab>('releves')
  const [produits, setProduits]     = useState<Produit[]>([])
  const [rapport, setRapport]       = useState<Rapport | null>(null)
  const [showModal, setShowModal]   = useState(false)
  const [backendOk, setBackendOk]   = useState(true)
  const [ready, setReady]           = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(true)

  const loadProduits = useCallback(async () => {
    try { setProduits(await api.produits.list()); setBackendOk(true) }
    catch { setBackendOk(false) }
  }, [])

  const loadRapport = useCallback(async () => {
    try { setRapport(await api.rapport(date)) } catch {}
  }, [date])

  useEffect(() => {
    Promise.all([loadProduits(), loadRapport()]).finally(() => setReady(true))
  }, [loadProduits, loadRapport])

  useEffect(() => { if (ready) loadRapport() }, [date, ready, loadRapport])

  return (
    <div className="flex overflow-hidden" style={{ height: '100dvh', background: 'var(--bg)' }}>

      {/* ════ SIDEBAR ════ */}
      <AnimatePresence initial={false}>
        {sidebarOpen && (
          <motion.aside key="sb"
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 240, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
            className="flex-shrink-0 flex flex-col overflow-hidden"
            style={{ background: 'var(--sb)', height: '100%' }}>

            {/* Brand */}
            <div className="flex items-center gap-3 px-5 py-5 flex-shrink-0"
              style={{ borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
              <div className="w-9 h-9 rounded-xl flex-shrink-0 flex items-center justify-center
                              font-black text-[13px] tracking-tight select-none"
                style={{ background: 'var(--sb-bar)', color: 'white' }}>
                SM
              </div>
              <div>
                <p className="font-bold text-[14px] leading-tight" style={{ color: 'white' }}>
                  Suivi Meters
                </p>
                <p className="text-[11px] mt-0.5" style={{ color: 'var(--sb-t2)' }}>
                  Station v1.0
                </p>
              </div>
            </div>

            {/* Navigation */}
            <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-6"
              style={{ scrollbarWidth: 'none' }}>
              {NAV.map(sec => (
                <div key={sec.section}>
                  <p className="text-[10px] font-bold uppercase tracking-widest px-2 mb-2"
                    style={{ color: 'var(--sb-t2)' }}>
                    {sec.section}
                  </p>
                  <div className="space-y-0.5">
                    {sec.items.map(({ id, label, Icon }) => {
                      const active = activeTab === id
                      return (
                        <button key={id} onClick={() => setActiveTab(id)}
                          className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg
                                     text-[13px] font-medium relative text-left outline-none transition-colors"
                          style={{
                            background: active ? 'var(--sb-a)' : 'transparent',
                            color: active ? 'white' : 'var(--sb-t)',
                          }}
                          onMouseEnter={e => { if (!active) (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.05)' }}
                          onMouseLeave={e => { if (!active) (e.currentTarget as HTMLElement).style.background = 'transparent' }}>
                          {active && (
                            <span className="absolute left-0 top-2 bottom-2 w-0.5 rounded-r"
                              style={{ background: 'var(--sb-bar)' }} />
                          )}
                          <Icon size={15} className="flex-shrink-0 opacity-80" />
                          {label}
                        </button>
                      )
                    })}
                  </div>
                </div>
              ))}

              <div>
                <p className="text-[10px] font-bold uppercase tracking-widest px-2 mb-2"
                  style={{ color: 'var(--sb-t2)' }}>CONFIGURATION</p>
                <button onClick={() => setShowModal(true)}
                  className="w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg
                             text-[13px] font-medium text-left outline-none transition-colors"
                  style={{ color: 'var(--sb-t)' }}
                  onMouseEnter={e => (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.05)'}
                  onMouseLeave={e => (e.currentTarget as HTMLElement).style.background = 'transparent'}>
                  <Settings size={15} className="flex-shrink-0 opacity-80" />
                  Produits &amp; Pompes
                </button>
              </div>
            </nav>

            {/* User footer */}
            <div className="flex-shrink-0 px-5 py-4"
              style={{ borderTop: '1px solid rgba(255,255,255,0.07)' }}>
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center
                                font-bold text-[13px]"
                  style={{ background: 'rgba(255,255,255,0.15)', color: 'white' }}>
                  A
                </div>
                <div className="overflow-hidden">
                  <p className="text-[13px] font-semibold truncate" style={{ color: 'white' }}>Admin</p>
                  <p className="text-[11px] truncate" style={{ color: 'var(--sb-t2)' }}>admin@station.ht</p>
                </div>
              </div>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>

      {/* ════ MAIN ════ */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">

        {/* Top header */}
        <header className="flex-shrink-0 flex items-center gap-4 px-6 h-[56px]"
          style={{ background: 'var(--card)', borderBottom: '1px solid var(--b1)', zIndex: 10 }}>

          {/* Menu toggle */}
          <button onClick={() => setSidebarOpen(o => !o)}
            className="w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
            style={{ color: 'var(--t2)' }}
            onMouseEnter={e => {
              (e.currentTarget as HTMLElement).style.background = 'var(--bg)'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--t0)'
            }}
            onMouseLeave={e => {
              (e.currentTarget as HTMLElement).style.background = 'transparent'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--t2)'
            }}>
            <Menu size={18} />
          </button>

          {/* Breadcrumb */}
          <div className="flex items-center gap-1.5">
            <span className="text-sm" style={{ color: 'var(--t2)' }}>Station</span>
            <ChevronRight size={13} style={{ color: 'var(--b2)' }} />
            <span className="text-sm font-semibold" style={{ color: 'var(--t0)' }}>
              {PAGE_TITLE[activeTab].split(' ')[0]}
            </span>
          </div>

          <div className="flex-1" />

          {/* Status badge */}
          <div className="hidden sm:flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-full"
            style={{
              background: backendOk ? 'rgba(22,163,74,0.07)' : 'rgba(220,38,38,0.07)',
              color:      backendOk ? 'var(--green)'          : 'var(--red)',
              border:     `1px solid ${backendOk ? 'rgba(22,163,74,0.18)' : 'rgba(220,38,38,0.18)'}`,
            }}>
            {backendOk ? <Wifi size={12} /> : <WifiOff size={12} />}
            {backendOk ? 'Connecté' : 'Hors ligne'}
          </div>

          {/* Date */}
          <div className="flex items-center gap-2.5">
            <span className="hidden sm:block text-[10px] font-bold uppercase tracking-widest"
              style={{ color: 'var(--t2)' }}>Date</span>
            <input type="date" value={date} onChange={e => setDate(e.target.value)}
              className="rounded-lg px-3 py-1.5 text-sm outline-none transition-all"
              style={{ background: 'var(--bg)', border: '1px solid var(--b1)', color: 'var(--t0)' }}
              onFocus={e => { e.target.style.borderColor = 'var(--blue2)'; e.target.style.boxShadow = '0 0 0 3px rgba(59,130,246,0.1)' }}
              onBlur={e => { e.target.style.borderColor = 'var(--b1)'; e.target.style.boxShadow = 'none' }}
            />
          </div>
        </header>

        {/* Scrollable content */}
        <main className="flex-1 overflow-y-auto" style={{ background: 'var(--bg)' }}>
          <div className="p-6 space-y-6">

            {/* Page title block */}
            <div>
              <h1 className="text-[22px] font-bold leading-tight" style={{ color: 'var(--t0)' }}>
                {PAGE_TITLE[activeTab]}
              </h1>
              <p className="text-sm mt-1" style={{ color: 'var(--t1)' }}>
                {PAGE_DESC[activeTab]}
              </p>
            </div>

            {/* KPI cards (relevés only) */}
            <AnimatePresence>
              {activeTab === 'releves' && (
                <motion.div key="kpis"
                  initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
                  {!ready ? (
                    <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(200px,1fr))' }}>
                      {[1, 2, 3].map(i => <div key={i} className="shimmer h-[88px] rounded-xl" />)}
                    </div>
                  ) : rapport ? (
                    <motion.div
                      className="grid gap-4"
                      style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(200px,1fr))' }}
                      initial="hidden" animate="visible"
                      variants={{ visible: { transition: { staggerChildren: 0.07 } } }}>
                      <KpiCard
                        label="Ventes totales" value={rapport.total_cash} unit="G"
                        colorHex="#f59e0b" Icon={TrendingUp} />
                      {rapport.produits.map((p, i) => (
                        <KpiCard key={p.produit_id}
                          label={p.produit_nom} value={p.total_cash_produit} unit="G"
                          colorHex={['#3b82f6', '#0891b2', '#7c3aed'][i % 3]}
                          Icon={[Fuel, BarChart3, Fuel][i % 3] as React.ElementType} />
                      ))}
                    </motion.div>
                  ) : null}
                </motion.div>
              )}
            </AnimatePresence>

            {/* Content card */}
            <div className="rounded-xl overflow-hidden"
              style={{ background: 'var(--card)', border: '1px solid var(--b1)' }}>
              <div className="p-6">
                <AnimatePresence mode="wait">
                  <motion.div key={activeTab}
                    initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -4 }}
                    transition={{ duration: 0.14, ease: 'easeOut' }}>
                    {activeTab === 'releves'   && <RelevesTab   date={date} produits={produits} onSaved={loadRapport} />}
                    {activeTab === 'rapport'   && <RapportTab   produits={produits} />}
                    {activeTab === 'anomalies' && <AnomaliesTab date={date} />}
                    {activeTab === 'chatbot'   && <ChatbotTab />}
                  </motion.div>
                </AnimatePresence>
              </div>
            </div>

          </div>
        </main>
      </div>

      {/* Modal */}
      <AnimatePresence>
        {showModal && (
          <GestionModal produits={produits} onClose={() => setShowModal(false)}
            onSaved={() => { loadProduits(); loadRapport() }} />
        )}
      </AnimatePresence>
    </div>
  )
}

/* ── KPI card with icon + count-up ── */
function KpiCard({
  label, value, unit, colorHex, Icon,
}: { label: string; value: number; unit: string; colorHex: string; Icon: React.ElementType }) {
  const [n, setN] = useState(0)
  const raf = useRef<number>(0)

  useEffect(() => {
    const t0 = performance.now(), dur = 900
    const tick = (now: number) => {
      const p = Math.min((now - t0) / dur, 1)
      setN(value * (1 - (1 - p) ** 3))
      if (p < 1) raf.current = requestAnimationFrame(tick)
    }
    raf.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf.current)
  }, [value])

  return (
    <motion.div
      variants={{ hidden: { opacity: 0, y: 12 }, visible: { opacity: 1, y: 0 } }}
      className="rounded-xl p-5 flex items-start gap-4"
      style={{ background: 'var(--card)', border: '1px solid var(--b1)' }}
      whileHover={{ boxShadow: '0 4px 20px rgba(0,0,0,0.06)' }}>
      <div className="w-11 h-11 rounded-xl flex items-center justify-center flex-shrink-0"
        style={{ background: `${colorHex}18`, color: colorHex }}>
        <Icon size={20} />
      </div>
      <div className="min-w-0">
        <p className="text-[11px] font-semibold uppercase tracking-wide truncate"
          style={{ color: 'var(--t2)' }}>{label}</p>
        <p className="text-[26px] font-black tnum leading-none mt-1" style={{ color: 'var(--t0)' }}>
          {n.toLocaleString('fr-FR', { maximumFractionDigits: 0 })}
          <span className="text-sm font-medium ml-1.5" style={{ color: 'var(--t1)' }}>{unit}</span>
        </p>
      </div>
    </motion.div>
  )
}
