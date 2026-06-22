'use client'

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Plus, Trash2, Package, Gauge } from 'lucide-react'
import toast from 'react-hot-toast'
import { api } from '@/lib/api'
import type { Produit } from '@/lib/types'

interface Props { produits: Produit[]; onClose: () => void; onSaved: () => void }

export default function GestionModal({ produits, onClose, onSaved }: Props) {
  const [newNom, setNewNom]   = useState('')
  const [newPrix, setNewPrix] = useState('')
  const [pompeInputs, setPompeInputs] = useState<Record<number, string>>({})
  const [deleting, setDeleting] = useState<number | null>(null)

  const addProduit = async () => {
    const nom = newNom.trim()
    if (!nom) return
    try {
      await api.produits.create(nom, parseFloat(newPrix) || 0)
      setNewNom(''); setNewPrix('')
      onSaved()
      toast.success(`Produit "${nom}" ajouté`)
    } catch (e: unknown) { toast.error(e instanceof Error ? e.message : 'Erreur') }
  }

  const addPompe = async (pid: number, pNom: string) => {
    const nom = (pompeInputs[pid] ?? '').trim()
    if (!nom) return
    try {
      await api.pompes.add(pid, nom)
      setPompeInputs(p => ({ ...p, [pid]: '' }))
      onSaved()
      toast.success(`Pompe "${nom}" ajoutée à ${pNom}`)
    } catch (e: unknown) { toast.error(e instanceof Error ? e.message : 'Erreur') }
  }

  const delPompe = async (pompe_id: number, nom: string) => {
    setDeleting(pompe_id)
    try {
      await api.pompes.delete(pompe_id)
      onSaved()
      toast.success(`Pompe "${nom}" supprimée`)
    } catch (e: unknown) { toast.error(e instanceof Error ? e.message : 'Erreur') }
    finally { setDeleting(null) }
  }

  /* shared field input styles */
  const field = { background: 'var(--c2)', border: '1px solid var(--b1)', color: 'var(--t0)' }
  const onFocus = (e: React.FocusEvent<HTMLInputElement>) => {
    e.target.style.borderColor = 'var(--blue2)'
    e.target.style.boxShadow   = '0 0 0 3px rgba(37,99,235,0.1)'
  }
  const onBlur = (e: React.FocusEvent<HTMLInputElement>) => {
    e.target.style.borderColor = 'var(--b1)'
    e.target.style.boxShadow   = 'none'
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">

      {/* Backdrop */}
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        className="absolute inset-0"
        style={{ background: 'rgba(15,23,42,0.5)', backdropFilter: 'blur(8px)' }}
        onClick={onClose} />

      {/* Panel — styled like ERP modal */}
      <motion.div
        initial={{ scale: 0.94, opacity: 0, y: 16 }}
        animate={{ scale: 1,    opacity: 1, y: 0 }}
        exit={{   scale: 0.94, opacity: 0, y: 16 }}
        transition={{ type: 'spring', stiffness: 420, damping: 32 }}
        className="relative w-full max-w-lg max-h-[88vh] flex flex-col rounded-2xl shadow-2xl z-10"
        style={{ background: 'var(--c1)', border: '1px solid var(--b1)' }}>

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 flex-shrink-0"
          style={{ borderBottom: '1px solid var(--b1)' }}>
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center"
              style={{ background: 'rgba(30,64,175,0.1)' }}>
              <Package size={16} style={{ color: 'var(--blue)' }} />
            </div>
            <div>
              <h2 className="font-bold text-[15px]" style={{ color: 'var(--t0)' }}>
                Produits &amp; Pompes
              </h2>
              <p className="text-[11px]" style={{ color: 'var(--t2)' }}>
                Gérer les produits et leur pompes associées
              </p>
            </div>
          </div>
          <button onClick={onClose}
            className="w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
            style={{ color: 'var(--t2)' }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--c2)'; (e.currentTarget as HTMLElement).style.color = 'var(--t0)' }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = 'transparent'; (e.currentTarget as HTMLElement).style.color = 'var(--t2)' }}>
            <X size={17} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6"
          style={{ scrollbarWidth: 'thin', scrollbarColor: 'var(--b1) transparent' }}>

          {/* ── Nouveau produit ── */}
          <div>
            <div className="flex items-center gap-3 mb-4">
              <span className="text-[10px] uppercase tracking-widest font-bold" style={{ color: 'var(--blue)' }}>
                NOUVEAU PRODUIT
              </span>
              <div className="flex-1 h-px" style={{ background: 'var(--b1)' }} />
            </div>

            <div className="grid grid-cols-2 gap-3 mb-3">
              <div className="flex flex-col gap-1">
                <label className="text-[9px] uppercase tracking-widest font-bold" style={{ color: 'var(--t2)' }}>
                  Nom du produit *
                </label>
                <input value={newNom} onChange={e => setNewNom(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && addProduit()}
                  onFocus={onFocus} onBlur={onBlur}
                  placeholder="Ex: Gazoline, Diesel…"
                  className="rounded-lg px-3 py-2.5 text-sm outline-none transition-all"
                  style={field} />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[9px] uppercase tracking-widest font-bold" style={{ color: 'var(--t2)' }}>
                  Prix / gallon (G)
                </label>
                <input value={newPrix} onChange={e => setNewPrix(e.target.value)}
                  type="number" step="0.01"
                  onFocus={onFocus} onBlur={onBlur}
                  placeholder="0.00"
                  className="rounded-lg px-3 py-2.5 text-sm outline-none transition-all"
                  style={field} />
              </div>
            </div>

            <motion.button whileHover={{ scale: 1.02 }} whileTap={{ scale: 0.98 }}
              onClick={addProduit}
              className="flex items-center gap-2 px-4 py-2.5 rounded-xl font-bold text-sm text-white"
              style={{ background: 'var(--blue)' }}>
              <Plus size={15} /> Ajouter le produit
            </motion.button>
          </div>

          {/* ── Liste des produits ── */}
          {produits.length > 0 && (
            <div>
              <div className="flex items-center gap-3 mb-4">
                <span className="text-[10px] uppercase tracking-widest font-bold" style={{ color: 'var(--blue)' }}>
                  PRODUITS EXISTANTS
                </span>
                <div className="flex-1 h-px" style={{ background: 'var(--b1)' }} />
              </div>

              <AnimatePresence>
                {produits.map((p, pi) => (
                  <motion.div key={p.id}
                    initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }}
                    transition={{ delay: pi * 0.06 }}
                    className="rounded-xl overflow-hidden mb-3"
                    style={{ border: '1px solid var(--b1)' }}>

                    {/* Product header */}
                    <div className="flex items-center gap-3 px-4 py-3"
                      style={{ background: 'var(--c2)', borderBottom: '1px solid var(--b1)' }}>
                      <div className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0"
                        style={{ background: 'rgba(30,64,175,0.08)' }}>
                        <Gauge size={13} style={{ color: 'var(--blue)' }} />
                      </div>
                      <div className="flex-1 min-w-0">
                        <p className="font-bold text-sm" style={{ color: 'var(--t0)' }}>{p.nom}</p>
                        <p className="text-[11px]" style={{ color: 'var(--t2)' }}>
                          {p.prix_gallon.toLocaleString('fr-FR')} G/gal ·{' '}
                          {p.pompes.length} pompe{p.pompes.length !== 1 ? 's' : ''}
                        </p>
                      </div>
                    </div>

                    <div className="p-3 space-y-1.5" style={{ background: 'var(--c1)' }}>
                      {/* Pompes list */}
                      <AnimatePresence>
                        {p.pompes.map(pompe => (
                          <motion.div key={pompe.id} layout
                            initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }}
                            exit={{ opacity: 0, height: 0 }}
                            className="flex items-center justify-between px-3 py-2 rounded-lg group transition-colors"
                            style={{ background: 'var(--c2)' }}
                            onMouseEnter={e => (e.currentTarget as HTMLElement).style.background = 'var(--c3)'}
                            onMouseLeave={e => (e.currentTarget as HTMLElement).style.background = 'var(--c2)'}>
                            <span className="text-[13px] font-medium" style={{ color: 'var(--t0)' }}>
                              {pompe.nom}
                            </span>
                            <motion.button whileHover={{ scale: 1.1 }} whileTap={{ scale: 0.9 }}
                              onClick={() => delPompe(pompe.id, pompe.nom)}
                              disabled={deleting === pompe.id}
                              title={`Supprimer ${pompe.nom}`}
                              className="opacity-0 group-hover:opacity-100 transition-opacity"
                              style={{ color: 'var(--t2)' }}
                              onMouseEnter={e => (e.currentTarget as HTMLElement).style.color = 'var(--red)'}
                              onMouseLeave={e => (e.currentTarget as HTMLElement).style.color = 'var(--t2)'}>
                              <Trash2 size={13} className={deleting === pompe.id ? 'animate-spin' : ''} />
                            </motion.button>
                          </motion.div>
                        ))}
                      </AnimatePresence>

                      {p.pompes.length === 0 && (
                        <p className="text-xs px-3 py-2" style={{ color: 'var(--t2)' }}>Aucune pompe.</p>
                      )}

                      {/* Add pompe */}
                      <div className="flex gap-2 pt-1.5">
                        <input value={pompeInputs[p.id] ?? ''}
                          onChange={e => setPompeInputs(pr => ({ ...pr, [p.id]: e.target.value }))}
                          onKeyDown={e => e.key === 'Enter' && addPompe(p.id, p.nom)}
                          onFocus={onFocus} onBlur={onBlur}
                          placeholder={`Ajouter une pompe à ${p.nom}`}
                          className="flex-1 rounded-lg px-3 py-2 text-sm outline-none transition-all"
                          style={field} />
                        <motion.button whileHover={{ scale: 1.06 }} whileTap={{ scale: 0.94 }}
                          onClick={() => addPompe(p.id, p.nom)}
                          className="px-3 py-2 rounded-lg font-bold text-white"
                          style={{ background: 'var(--blue)' }}>
                          <Plus size={15} />
                        </motion.button>
                      </div>
                    </div>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          )}

          {produits.length === 0 && (
            <p className="text-center text-sm py-8" style={{ color: 'var(--t2)' }}>
              Aucun produit — ajoutez-en un ci-dessus.
            </p>
          )}
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 flex items-center justify-end px-6 py-4"
          style={{ borderTop: '1px solid var(--b1)', background: 'var(--c2)' }}>
          <button onClick={onClose}
            className="px-5 py-2.5 rounded-xl font-semibold text-sm transition-colors"
            style={{ border: '1px solid var(--b1)', color: 'var(--t1)', background: 'var(--c1)' }}
            onMouseEnter={e => (e.currentTarget as HTMLElement).style.background = 'var(--c3)'}
            onMouseLeave={e => (e.currentTarget as HTMLElement).style.background = 'var(--c1)'}>
            Fermer
          </button>
        </div>
      </motion.div>
    </div>
  )
}
