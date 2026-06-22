'use client'

import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Check, Save, Loader2, AlertCircle, Layers } from 'lucide-react'
import toast from 'react-hot-toast'
import { api } from '@/lib/api'
import type { Produit } from '@/lib/types'

const PERIODES = ['Matin', 'Apres-midi'] as const
const LABEL: Record<string, string> = { Matin: 'Matin', 'Apres-midi': 'Après-midi' }

interface Row { px: string; av: string; ap: string; saving: boolean; saved: boolean }
interface Props { date: string; produits: Produit[]; onSaved: () => void }

export default function RelevesTab({ date, produits, onSaved }: Props) {
  const [rows, setRows] = useState<Record<string, Row>>({})

  const key = (per: string, pid: number) => `${per}::${pid}`

  const load = useCallback(async () => {
    try {
      const releves = await api.releves.list(date)
      const next: Record<string, Row> = {}
      for (const p of produits)
        for (const po of p.pompes)
          for (const per of PERIODES)
            next[key(per, po.id)] = { px: String(p.prix_gallon), av: '', ap: '', saving: false, saved: false }
      for (const r of releves) {
        const k = key(r.periode, r.pompe_id)
        next[k] = { px: String(r.prix_gallon), av: String(r.metter_avant), ap: String(r.metter_apres), saving: false, saved: true }
      }
      setRows(next)
    } catch { toast.error('Erreur de chargement') }
  }, [date, produits])

  useEffect(() => { load() }, [load])

  const upd = (k: string, f: 'px' | 'av' | 'ap', v: string) =>
    setRows(p => ({ ...p, [k]: { ...p[k], [f]: v, saved: false } }))

  const qte = (r: Row) => {
    const a = parseFloat(r.av) || 0, b = parseFloat(r.ap) || 0
    return Math.round((b - a) * 1000) / 1000
  }
  const mnt = (r: Row) => Math.round(qte(r) * (parseFloat(r.px) || 0) * 100) / 100
  const isError = (r: Row) => {
    const a = parseFloat(r.av), b = parseFloat(r.ap)
    return !isNaN(a) && !isNaN(b) && b < a
  }

  const save = async (per: string, pompe_id: number, defPx: number) => {
    const k = key(per, pompe_id)
    const r = rows[k]; if (!r) return
    if (isError(r)) { toast.error('Meter après < Meter avant — impossible'); return }
    setRows(p => ({ ...p, [k]: { ...p[k], saving: true } }))
    try {
      await api.releves.upsert({
        date, periode: per, pompe_id,
        prix_gallon: parseFloat(r.px) || defPx,
        metter_avant: parseFloat(r.av) || 0,
        metter_apres: parseFloat(r.ap) || 0,
      })
      setRows(p => ({ ...p, [k]: { ...p[k], saving: false, saved: true } }))
      toast.success('Relevé sauvegardé')
      onSaved()
    } catch (e) {
      setRows(p => ({ ...p, [k]: { ...p[k], saving: false } }))
      toast.error(`Erreur : ${e instanceof Error ? e.message : 'inconnue'}`)
    }
  }

  if (produits.length === 0) return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <div className="w-16 h-16 rounded-2xl flex items-center justify-center"
        style={{ background: 'var(--bg)', border: '1px solid var(--b1)' }}>
        <Layers size={28} style={{ color: 'var(--t2)' }} />
      </div>
      <div>
        <p className="text-base font-semibold" style={{ color: 'var(--t0)' }}>Aucun produit configuré</p>
        <p className="text-sm mt-1 max-w-xs" style={{ color: 'var(--t1)' }}>
          Allez dans Configuration → Produits &amp; Pompes pour commencer.
        </p>
      </div>
    </div>
  )

  return (
    <div className="space-y-8">
      {PERIODES.map((per, pi) => (
        <motion.section key={per}
          initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
          transition={{ delay: pi * 0.08 }}>

          {/* Period heading */}
          <div className="flex items-center gap-4 mb-5">
            <div className="flex items-center gap-2.5">
              <div className="w-2 h-2 rounded-full" style={{ background: 'var(--blue)' }} />
              <h3 className="text-[15px] font-bold" style={{ color: 'var(--t0)' }}>{LABEL[per]}</h3>
            </div>
            <div className="flex-1 h-px" style={{ background: 'var(--b1)' }} />
          </div>

          {/* Products */}
          <div className="space-y-5">
            {produits.map(p => (
              <div key={p.id}>

                {/* Product label */}
                <div className="flex items-center gap-2 mb-3">
                  <span className="text-[11px] font-bold uppercase tracking-widest"
                    style={{ color: 'var(--blue)' }}>{p.nom}</span>
                  <span className="text-xs" style={{ color: 'var(--t2)' }}>
                    · {p.prix_gallon.toLocaleString('fr-FR')} G/gal
                  </span>
                </div>

                {/* Table */}
                <div className="rounded-xl overflow-hidden"
                  style={{ border: '1px solid var(--b1)' }}>
                  <div className="overflow-x-auto">
                    <table className="w-full border-collapse">
                      <thead>
                        <tr style={{ background: 'var(--bg)' }}>
                          {[
                            { label: 'Pompe',       align: 'left'  },
                            { label: 'Prix / gal',  align: 'right' },
                            { label: 'Meter avant', align: 'right' },
                            { label: 'Meter après', align: 'right' },
                            { label: 'Qté (gal)',   align: 'right' },
                            { label: 'Montant (G)', align: 'right' },
                            { label: '',            align: 'right' },
                          ].map(h => (
                            <th key={h.label}
                              className={`px-5 py-3 text-left text-[10px] font-bold uppercase tracking-widest whitespace-nowrap`}
                              style={{ color: 'var(--t2)', borderBottom: '1px solid var(--b1)', textAlign: h.align as 'left' | 'right' }}>
                              {h.label}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {p.pompes.map((po, ri) => {
                          const k = key(per, po.id)
                          const r = rows[k] ?? { px: String(p.prix_gallon), av: '', ap: '', saving: false, saved: false }
                          const q = qte(r), m = mnt(r), err = isError(r)
                          return (
                            <motion.tr key={po.id}
                              initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                              transition={{ delay: ri * 0.04 }}
                              style={{ borderBottom: '1px solid var(--b0)' }}
                              onMouseEnter={e => (e.currentTarget as HTMLElement).style.background = 'var(--bg)'}
                              onMouseLeave={e => (e.currentTarget as HTMLElement).style.background = 'transparent'}>

                              {/* Pump name */}
                              <td className="px-5 py-4">
                                <span className="text-sm font-semibold" style={{ color: 'var(--t0)' }}>
                                  {po.nom}
                                </span>
                              </td>

                              {/* Inputs */}
                              <td className="px-5 py-4"><NumInput value={r.px} onChange={v => upd(k, 'px', v)} /></td>
                              <td className="px-5 py-4"><NumInput value={r.av} onChange={v => upd(k, 'av', v)} /></td>
                              <td className="px-5 py-4"><NumInput value={r.ap} onChange={v => upd(k, 'ap', v)} error={err} /></td>

                              {/* Calculated */}
                              <td className="px-5 py-4 text-right">
                                <span className="text-sm font-bold font-mono tnum"
                                  style={{ color: q < 0 ? 'var(--red)' : q > 0 ? 'var(--teal)' : 'var(--t2)' }}>
                                  {q.toFixed(3)}
                                </span>
                              </td>
                              <td className="px-5 py-4 text-right">
                                <span className="text-sm font-bold font-mono tnum" style={{ color: 'var(--amber)' }}>
                                  {m.toLocaleString('fr-FR', { maximumFractionDigits: 2 })}
                                </span>
                              </td>

                              {/* Action */}
                              <td className="px-5 py-4">
                                <AnimatePresence mode="wait">
                                  {r.saving ? (
                                    <motion.span key="s" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                                      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                                      style={{ background: 'var(--bg)', color: 'var(--t1)' }}>
                                      <Loader2 size={11} className="animate-spin" /> Sauvegarde…
                                    </motion.span>
                                  ) : r.saved ? (
                                    <motion.span key="d" initial={{ scale: 0.8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
                                      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold"
                                      style={{ background: 'rgba(22,163,74,0.08)', color: 'var(--green)', border: '1px solid rgba(22,163,74,0.18)' }}>
                                      <Check size={11} /> Sauvé
                                    </motion.span>
                                  ) : (
                                    <motion.button key="b" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                                      whileHover={{ scale: 1.03 }} whileTap={{ scale: 0.97 }}
                                      onClick={() => save(per, po.id, p.prix_gallon)}
                                      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold text-white whitespace-nowrap"
                                      style={{ background: 'var(--blue)' }}>
                                      <Save size={11} /> Sauver
                                    </motion.button>
                                  )}
                                </AnimatePresence>
                              </td>
                            </motion.tr>
                          )
                        })}
                        {p.pompes.length === 0 && (
                          <tr>
                            <td colSpan={7} className="px-5 py-5">
                              <span className="flex items-center gap-2 text-sm" style={{ color: 'var(--t2)' }}>
                                <AlertCircle size={14} />
                                Aucune pompe pour {p.nom}. Configurez-en dans le menu.
                              </span>
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </motion.section>
      ))}
    </div>
  )
}

function NumInput({ value, onChange, error }: { value: string; onChange: (v: string) => void; error?: boolean }) {
  const bNormal = error ? 'rgba(220,38,38,0.4)' : 'var(--b1)'
  const bFocus  = error ? 'var(--red)'           : 'var(--blue2)'
  const shadow  = error ? 'rgba(220,38,38,0.1)'  : 'rgba(59,130,246,0.1)'

  return (
    <input type="number" value={value} onChange={e => onChange(e.target.value)} step="0.001"
      className="w-28 rounded-lg px-3 py-2 text-right text-sm font-mono tnum outline-none transition-all"
      style={{ background: 'var(--bg)', border: `1px solid ${bNormal}`, color: 'var(--t0)' }}
      onFocus={e => { e.target.style.borderColor = bFocus; e.target.style.boxShadow = `0 0 0 3px ${shadow}` }}
      onBlur={e => { e.target.style.borderColor = bNormal; e.target.style.boxShadow = 'none' }}
    />
  )
}
