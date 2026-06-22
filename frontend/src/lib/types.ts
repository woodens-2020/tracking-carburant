export interface Pompe {
  id: number
  nom: string
}

export interface Produit {
  id: number
  nom: string
  prix_gallon: number
  pompes: Pompe[]
}

export interface Releve {
  id: number
  date: string
  periode: string
  pompe_id: number
  pompe_nom: string
  produit_id: number
  produit_nom: string
  prix_gallon: number
  metter_avant: number
  metter_apres: number
  quantite: number
  montant_vente: number
}

export interface RapportPeriode {
  periode: string
  releves: Releve[]
  total_cash: number
}

export interface RapportProduit {
  produit_id: number
  produit_nom: string
  periodes: RapportPeriode[]
  total_cash_produit: number
}

export interface Rapport {
  date: string
  produits: RapportProduit[]
  total_cash: number
}

export interface StatsProduit {
  quantite: number
  montant: number
}

export interface StatsPompe {
  produit: string
  quantite: number
  montant: number
}

export interface StatsPeriode {
  quantite: number
  montant: number
}

export interface Stats {
  date_debut: string
  date_fin: string
  filtre_produit_id: number | null
  filtre_periode: string | null
  nb_jours_couverts: number
  nb_releves: number
  total_quantite: number
  total_montant: number
  par_produit: Record<string, StatsProduit>
  par_pompe: Record<string, StatsPompe>
  par_periode: Record<string, StatsPeriode>
}

export interface Anomalie {
  type: string
  gravite: 'erreur' | 'avertissement'
  pompe_nom: string
  date: string
  periode: string
  valeur_attendue_min: number
  valeur_saisie: number
  message: string
}

export interface AnomaliesResult {
  date: string
  nb_anomalies: number
  anomalies: Anomalie[]
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}
