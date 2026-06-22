import type { Produit, Releve, Rapport, Stats, AnomaliesResult, ChatMessage } from './types'

async function req<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options)
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`)
  return res.json() as Promise<T>
}

export const api = {
  produits: {
    list: () => req<Produit[]>('/api/produits'),
    create: (nom: string, prix_gallon: number) =>
      req<Produit>('/api/produits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom, prix_gallon }),
      }),
  },
  pompes: {
    add: (produit_id: number, nom: string) =>
      req<{ id: number; nom: string }>(`/api/produits/${produit_id}/pompes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom }),
      }),
    delete: (pompe_id: number) =>
      req<{ ok: boolean }>(`/api/pompes/${pompe_id}`, { method: 'DELETE' }),
  },
  releves: {
    list: (date: string, periode?: string, produit_id?: number) => {
      const p = new URLSearchParams({ date })
      if (periode) p.set('periode', periode)
      if (produit_id) p.set('produit_id', String(produit_id))
      return req<Releve[]>(`/api/releves?${p}`)
    },
    upsert: (data: {
      date: string; periode: string; pompe_id: number
      prix_gallon: number; metter_avant: number; metter_apres: number
    }) =>
      req<Releve>('/api/releves', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      }),
  },
  rapport: (date: string) => req<Rapport>(`/api/rapport?date=${date}`),
  stats: (params: { date_debut: string; date_fin: string; produit_id?: number; periode?: string }) => {
    const p = new URLSearchParams({ date_debut: params.date_debut, date_fin: params.date_fin })
    if (params.produit_id) p.set('produit_id', String(params.produit_id))
    if (params.periode) p.set('periode', params.periode)
    return req<Stats>(`/api/stats?${p}`)
  },
  anomalies: (date: string) => req<AnomaliesResult>(`/api/anomalies?date=${date}`),
  chat: (message: string, historique: ChatMessage[]) =>
    req<{ reponse: string; historique: ChatMessage[] }>('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, historique }),
    }),
}
