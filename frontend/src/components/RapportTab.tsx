'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Search, TrendingUp, Droplets, Calendar, FileText } from 'lucide-react'
import toast from 'react-hot-toast'
import { api } from '@/lib/api'
import type { Produit, Stats } from '@/lib/types'

interface Props { produits: Produit[] }

export default function RapportTab({ produits }: Props) {
  const today    = new Date().toISOString().slice(0, 10)
  const firstDay = today.slice(0, 8) + '01'
  const [debut, setDebut]     = useState(firstDay)
  const [fin, setFin]         = useState(today)
  const [pid, setPid]         = useState('')
  const [stats, setStats]     = useState<Stats | null>(null)
  const [loading, setLoading] = useState(false)

  const generate = async () => {
    setLoading(true)
    try {
      const s = await api.stats({ date_debut: debut, date_fin: fin, produit_id: pid ? +pid : undefined })
      setStats(s)
      if (s.nb_releves === 0) toast('Aucune donnée sur cette période', { icon: '📭' })
    } catch { toast.error('Impossible de charger les statistiques') }
    finally { setLoading(false) }
  }

  const fieldStyle = { background: 'var(--bg)', border: '1px solid var(--b1)', color: 'var(--t0)' }

  const focusIn = (e: React.FocusEvent<HTMLInputElement | HTMLSelectElement>) => {
    e.target.style.borderColor = 'var(--blue2)'
    e.target.style.boxShadow   = '0 0 0 3px rgba(59,130,246,0.1)'
  }
  const focusOut = (e: React.FocusEvent<HTMLInputElement | HTMLSelectElement>) => {
    e.target.style.borderColor = 'var(--b1)'
    e.target.style.boxShadow   = 'none'
  }

  return (
    <div className="space-y-8">

      {/* ── Filters ── */}
      <div>
        <SectionLabel>Paramètres de la période</SectionLabel>
        <div className="rounded-xl p-5 mt-4" style={{ background: 'var(--bg)', border: '1px solid var(--b1)' }}>
          <div className="flex flex-wrap gap-4 items-end">

            <Field label="Date de début">
              <input type="date" value={debut} onChange={e => setDebut(e.target.value)}
                className="rounded-lg px-3 py-2.5 text-sm outline-none transition-all"
                style={fieldStyle}
                onFocus={focusIn as React.FocusEventHandler<HTMLInputElement>}
                onBlur={focusOut as React.FocusEventHandler<HTMLInputElement>}
              />
            </Field>

            <Field label="Date de fin">
              <input type="date" value={fin} onChange={e => setFin(e.target.value)}
                className="rounded-lg px-3 py-2.5 text-sm outline-none transition-all"
                style={fieldStyle}
                onFocus={focusIn as React.FocusEventHandler<HTMLInputElement>}
                onBlur={focusOut as React.FocusEventHandler<HTMLInputElement>}
              />
            </Field>

            <Field label="Produit">
              <select value={pid} onChange={e => setPid(e.target.value)}
                className="rounded-lg px-3 py-2.5 text-sm outline-none transition-all"
                style={{ ...fieldStyle, minWidth: '180px' }}
                onFocus={focusIn as React.FocusEventHandler<HTMLSelectElement>}
                onBlur={focusOut as React.FocusEventHandler<HTMLSelectElement>}>
                <option value="">Tous les produits</option>
                {produits.map(p => <option key={p.id} value={p.id}>{p.nom}</option>)}
              </select>
            </Field>

            <motion.button whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}
              onClick={generate} disabled={loading}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm text-white
                         disabled:opacity-50 transition-opacity"
              style={{ background: 'var(--blue)' }}>
              {loading
                ? <span className="w-4 h-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />
                : <Search size={14} />}
              {loading ? 'Chargement…' : 'Générer le rapport'}
            </motion.button>
          </div>
        </div>
      </div>

      {/* ── Results ── */}
      <AnimatePresence mode="wait">
        {stats && (
          <motion.div key="results"
            initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
            className="space-y-6">

            <SectionLabel>Résultats</SectionLabel>

            {/* KPI row */}
            <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))' }}>
              {[
                { Icon: TrendingUp, label: 'Total montant',  value: stats.total_montant.toLocaleString('fr-FR', { maximumFractionDigits: 2 }), unit: 'G',   colorHex: '#d97706' },
                { Icon: Droplets,   label: 'Total quantité', value: stats.total_quantite.toLocaleString('fr-FR', { maximumFractionDigits: 3 }), unit: 'gal', colorHex: '#0891b2' },
                { Icon: Calendar,   label: 'Jours couverts', value: String(stats.nb_jours_couverts), unit: 'j',   colorHex: '#1d4ed8' },
                { Icon: FileText,   label: 'Nb de relevés',  value: String(stats.nb_releves),        unit: '',    colorHex: '#7c3aed' },
              ].map(({ Icon, label, value, unit, colorHex }, i) => (
                <motion.div key={label}
                  initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.07 }}
                  className="rounded-xl p-5 flex items-start gap-4"
                  style={{ background: 'var(--card)', border: '1px solid var(--b1)' }}>
                  <div className="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0"
                    style={{ background: `${colorHex}14`, color: colorHex }}>
                    <Icon size={18} />
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: 'var(--t2)' }}>
                      {label}
                    </p>
                    <p className="text-xl font-black tnum mt-0.5 leading-none" style={{ color: 'var(--t0)' }}>
                      {value}
                      <span className="text-xs font-medium ml-1" style={{ color: 'var(--t1)' }}>{unit}</span>
                    </p>
                  </div>
                </motion.div>
              ))}
            </div>

            {stats.nb_releves === 0 ? (
              <div className="rounded-xl py-12 text-center"
                style={{ background: 'var(--bg)', border: '1px solid var(--b1)' }}>
                <p className="text-sm" style={{ color: 'var(--t1)' }}>Aucune donnée sur cette période.</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <BarChart title="Par produit"
                  rows={Object.entries(stats.par_produit).map(([n, d]) => ({ label: n, quantite: d.quantite, montant: d.montant }))} />
                <BarChart title="Par pompe"
                  rows={Object.entries(stats.par_pompe).map(([n, d]) => ({ label: `${n} · ${d.produit}`, quantite: d.quantite, montant: d.montant }))} />
                <BarChart title="Par période"
                  rows={Object.entries(stats.par_periode).map(([n, d]) => ({ label: n, quantite: d.quantite, montant: d.montant }))} />
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/* ── Section label ── */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-[11px] font-bold uppercase tracking-widest flex-shrink-0"
        style={{ color: 'var(--blue)' }}>{children}</span>
      <div className="flex-1 h-px" style={{ background: 'var(--b1)' }} />
    </div>
  )
}

/* ── Field wrapper ── */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[10px] font-bold uppercase tracking-widest" style={{ color: 'var(--t2)' }}>
        {label}
      </label>
      {children}
    </div>
  )
}

/* ── Bar chart ── */
function BarChart({ title, rows }: { title: string; rows: { label: string; quantite: number; montant: number }[] }) {
  const max = Math.max(...rows.map(r => r.montant), 1)
  return (
    <div className="rounded-xl overflow-hidden" style={{ background: 'var(--card)', border: '1px solid var(--b1)' }}>
      <div className="px-5 py-3.5" style={{ borderBottom: '1px solid var(--b1)', background: 'var(--bg)' }}>
        <p className="text-[11px] font-bold uppercase tracking-widest" style={{ color: 'var(--blue)' }}>
          {title}
        </p>
      </div>
      <div>
        {rows.map((r, i) => (
          <div key={r.label} className="px-5 py-4" style={{ borderBottom: i < rows.length - 1 ? '1px solid var(--b0)' : 'none' }}>
            <div className="flex justify-between items-center mb-2">
              <span className="text-sm font-medium" style={{ color: 'var(--t0)' }}>{r.label}</span>
              <div className="text-right">
                <span className="text-sm font-bold font-mono tnum" style={{ color: 'var(--amber)' }}>
                  {r.montant.toLocaleString('fr-FR', { maximumFractionDigits: 2 })} G
                </span>
              </div>
            </div>
            <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'var(--b1)' }}>
              <motion.div className="h-full rounded-full"
                style={{ background: 'linear-gradient(90deg, var(--blue), var(--blue2))' }}
                initial={{ width: 0 }}
                animate={{ width: `${(r.montant / max) * 100}%` }}
                transition={{ duration: 0.7, delay: i * 0.06, ease: [0.16, 1, 0.3, 1] }} />
            </div>
            <p className="text-xs mt-1.5 tnum" style={{ color: 'var(--t2)' }}>
              {r.quantite.toLocaleString('fr-FR', { maximumFractionDigits: 3 })} gal
            </p>
          </div>
        ))}
      </div>
    </div>
  )
}
