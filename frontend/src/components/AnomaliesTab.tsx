'use client'

import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { XCircle, AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import type { AnomaliesResult } from '@/lib/types'

interface Props { date: string }

export default function AnomaliesTab({ date }: Props) {
  const [result, setResult]   = useState<AnomaliesResult | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try { setResult(await api.anomalies(date)) } catch {}
    finally { setLoading(false) }
  }, [date])

  useEffect(() => { load() }, [load])

  const errors   = result?.anomalies.filter(a => a.gravite === 'erreur') ?? []
  const warnings = result?.anomalies.filter(a => a.gravite === 'avertissement') ?? []

  return (
    <div className="space-y-5">

      {/* ── Section label + toolbar ── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-3">
            <span className="text-[10px] uppercase tracking-widest font-bold" style={{ color: 'var(--blue)' }}>
              DÉTECTION
            </span>
            <div className="w-24 h-px" style={{ background: 'var(--b1)' }} />
          </div>
          {result && (
            <>
              {errors.length > 0 && (
                <div className="flex items-center gap-1.5 text-[12px] font-semibold px-2.5 py-0.5 rounded-full"
                  style={{ background: 'rgba(220,38,38,0.08)', color: 'var(--red)', border: '1px solid rgba(220,38,38,0.2)' }}>
                  <XCircle size={13} /> {errors.length} erreur{errors.length > 1 ? 's' : ''}
                </div>
              )}
              {warnings.length > 0 && (
                <div className="flex items-center gap-1.5 text-[12px] font-semibold px-2.5 py-0.5 rounded-full"
                  style={{ background: 'rgba(217,119,6,0.08)', color: 'var(--amber)', border: '1px solid rgba(217,119,6,0.2)' }}>
                  <AlertTriangle size={13} /> {warnings.length} avertissement{warnings.length > 1 ? 's' : ''}
                </div>
              )}
              {result.nb_anomalies === 0 && (
                <div className="flex items-center gap-1.5 text-[12px] font-semibold px-2.5 py-0.5 rounded-full"
                  style={{ background: 'rgba(22,163,74,0.08)', color: 'var(--green)', border: '1px solid rgba(22,163,74,0.2)' }}>
                  <ShieldCheck size={13} /> Aucune anomalie
                </div>
              )}
            </>
          )}
        </div>
        <motion.button whileHover={{ scale: 1.04 }} whileTap={{ scale: 0.96 }}
          onClick={load} disabled={loading}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors"
          style={{ background: 'var(--c2)', border: '1px solid var(--b1)', color: 'var(--t1)' }}
          onMouseEnter={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--blue2)'; (e.currentTarget as HTMLElement).style.color = 'var(--blue)' }}
          onMouseLeave={e => { (e.currentTarget as HTMLElement).style.borderColor = 'var(--b1)'; (e.currentTarget as HTMLElement).style.color = 'var(--t1)' }}>
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          Actualiser
        </motion.button>
      </div>

      {/* ── Empty / success state ── */}
      <AnimatePresence>
        {result?.nb_anomalies === 0 && (
          <motion.div initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}
            className="flex flex-col items-center gap-5 py-20 text-center rounded-xl"
            style={{ background: 'var(--c2)', border: '1px solid var(--b1)' }}>
            <motion.div
              initial={{ scale: 0 }} animate={{ scale: 1 }}
              transition={{ type: 'spring', stiffness: 260, damping: 18, delay: 0.1 }}
              className="w-20 h-20 rounded-full flex items-center justify-center"
              style={{ background: 'rgba(22,163,74,0.1)', boxShadow: '0 0 32px rgba(22,163,74,0.12)' }}>
              <ShieldCheck size={40} style={{ color: 'var(--green)' }} />
            </motion.div>
            <div>
              <p className="font-bold text-base" style={{ color: 'var(--t0)' }}>Tous les compteurs sont cohérents</p>
              <p className="text-sm mt-1" style={{ color: 'var(--t1)' }}>
                Aucune anomalie détectée jusqu&apos;au {date}
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── Anomaly cards ── */}
      {result && result.nb_anomalies > 0 && (
        <motion.div className="space-y-3"
          initial="hidden" animate="visible"
          variants={{ visible: { transition: { staggerChildren: 0.06 } } }}>
          {result.anomalies.map((a, i) => {
            const isErr  = a.gravite === 'erreur'
            const clr    = isErr ? 'var(--red)'  : 'var(--amber)'
            const clrRaw = isErr ? '#dc2626'      : '#d97706'
            return (
              <motion.div key={i}
                variants={{ hidden: { opacity: 0, x: -10 }, visible: { opacity: 1, x: 0 } }}
                className="rounded-xl p-4 relative overflow-hidden"
                style={{
                  background: `color-mix(in srgb, ${clrRaw} 4%, var(--c1))`,
                  border: `1px solid color-mix(in srgb, ${clrRaw} 20%, var(--b1))`,
                }}>
                <div className="absolute left-0 top-0 bottom-0 w-1 rounded-r-full" style={{ background: clr }} />
                <div className="flex items-start gap-3 pl-3">
                  <div className="relative flex-shrink-0 mt-0.5">
                    {isErr
                      ? <XCircle size={17} style={{ color: clr }} />
                      : <AlertTriangle size={17} style={{ color: clr }} />}
                    {isErr && (
                      <span className="absolute inset-0 rounded-full animate-ping opacity-20"
                        style={{ background: clr }} />
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2 mb-1.5">
                      <span className="text-[9px] uppercase tracking-widest font-black px-2 py-0.5 rounded-full"
                        style={{ background: `color-mix(in srgb, ${clrRaw} 15%, transparent)`, color: clr }}>
                        {a.type}
                      </span>
                      <span className="font-bold text-[13px]" style={{ color: 'var(--t0)' }}>{a.pompe_nom}</span>
                      <span className="text-[11px]" style={{ color: 'var(--t2)' }}>· {a.date} — {a.periode}</span>
                    </div>
                    <p className="text-[13px] leading-relaxed" style={{ color: 'var(--t1)' }}>{a.message}</p>
                    <div className="flex flex-wrap gap-5 mt-2.5 text-[11px]" style={{ color: 'var(--t2)' }}>
                      <span>Min attendu :
                        <span className="font-mono font-semibold ml-1" style={{ color: 'var(--t0)' }}>
                          {a.valeur_attendue_min}
                        </span>
                      </span>
                      <span>Saisi :
                        <span className="font-mono font-bold ml-1" style={{ color: clr }}>
                          {a.valeur_saisie}
                        </span>
                      </span>
                    </div>
                  </div>
                </div>
              </motion.div>
            )
          })}
        </motion.div>
      )}
    </div>
  )
}
