-- ================================================================
-- Suivi des Compteurs — Schéma PostgreSQL
-- Station de carburant · Haïti
-- Version : 1.0
-- ================================================================
-- Commandes de setup initial (en tant que superuser) :
--   CREATE USER station_user WITH PASSWORD 'votre_mot_de_passe';
--   CREATE DATABASE station_db OWNER station_user ENCODING 'UTF8';
--   \c station_db
--   \i schema.sql
-- ================================================================

-- ── Nettoyage (DROP dans l'ordre inverse des FK) ─────────────────
-- DROP TABLE IF EXISTS releves  CASCADE;
-- DROP TABLE IF EXISTS pompes   CASCADE;
-- DROP TABLE IF EXISTS produits CASCADE;

-- ── 1. PRODUITS ──────────────────────────────────────────────────
-- Un type de carburant vendu à la station : Gazoline, Diesel, etc.
CREATE TABLE IF NOT EXISTS produits (
    id            SERIAL          PRIMARY KEY,
    nom           VARCHAR(100)    NOT NULL,
    prix_gallon   NUMERIC(12, 3)  NOT NULL DEFAULT 0,
    actif         BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_produit_nom UNIQUE (nom),
    CONSTRAINT chk_prix_pos   CHECK  (prix_gallon >= 0)
);

COMMENT ON TABLE  produits             IS 'Types de carburant vendus à la station.';
COMMENT ON COLUMN produits.nom         IS 'Nom du produit : Gazoline, Diesel, Kérosène…';
COMMENT ON COLUMN produits.prix_gallon IS 'Prix courant par gallon (gourdes). Utilisé comme valeur par défaut à la saisie.';
COMMENT ON COLUMN produits.actif       IS 'FALSE = produit archivé, masqué dans l''interface.';


-- ── 2. POMPES ────────────────────────────────────────────────────
-- Une pompe / distributeur physique rattaché à un produit.
CREATE TABLE IF NOT EXISTS pompes (
    id          SERIAL          PRIMARY KEY,
    produit_id  INTEGER         NOT NULL,
    nom         VARCHAR(100)    NOT NULL,
    actif       BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_pompe_produit FOREIGN KEY (produit_id)
        REFERENCES produits(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT uq_pompe_nom UNIQUE (produit_id, nom)
);

COMMENT ON TABLE  pompes        IS 'Pompes/distributeurs physiques de la station.';
COMMENT ON COLUMN pompes.actif  IS 'FALSE = pompe hors service (conservée pour l''historique).';

CREATE INDEX IF NOT EXISTS idx_pompes_produit ON pompes(produit_id);
CREATE INDEX IF NOT EXISTS idx_pompes_actif   ON pompes(produit_id) WHERE actif = TRUE;


-- ── 3. RELEVES ───────────────────────────────────────────────────
-- Un relevé de compteur : 1 ligne par (date, periode, pompe).
-- La quantité vendue = metter_apres - metter_avant (en gallons).
-- Le montant        = quantite * prix_gallon (en gourdes).
CREATE TABLE IF NOT EXISTS releves (
    id            SERIAL          PRIMARY KEY,
    date          DATE            NOT NULL,
    periode       VARCHAR(20)     NOT NULL,
    pompe_id      INTEGER         NOT NULL,
    prix_gallon   NUMERIC(12, 3)  NOT NULL DEFAULT 0,
    metter_avant  NUMERIC(14, 3)  NOT NULL DEFAULT 0,
    metter_apres  NUMERIC(14, 3)  NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Unicité : un seul relevé par pompe, par période, par jour
    CONSTRAINT uq_releve UNIQUE (date, periode, pompe_id),

    -- Contraintes métier
    CONSTRAINT chk_periode       CHECK (periode IN ('Matin', 'Apres-midi')),
    CONSTRAINT chk_meter_ordre   CHECK (metter_apres >= metter_avant),
    CONSTRAINT chk_prix_pos      CHECK (prix_gallon >= 0),
    CONSTRAINT chk_meter_pos     CHECK (metter_avant >= 0),

    CONSTRAINT fk_releve_pompe FOREIGN KEY (pompe_id)
        REFERENCES pompes(id) ON DELETE RESTRICT ON UPDATE CASCADE
);

COMMENT ON TABLE  releves              IS 'Relevés de compteurs (deux par jour : Matin et Apres-midi).';
COMMENT ON COLUMN releves.date         IS 'Date du relevé.';
COMMENT ON COLUMN releves.periode      IS 'Période : ''Matin'' ou ''Apres-midi''.';
COMMENT ON COLUMN releves.metter_avant IS 'Lecture du compteur en DÉBUT de période (gallons cumulés).';
COMMENT ON COLUMN releves.metter_apres IS 'Lecture du compteur en FIN de période. Doit être >= metter_avant.';
COMMENT ON COLUMN releves.prix_gallon  IS 'Prix au gallon (gourdes) au moment du relevé. Peut différer du prix courant du produit.';

-- Index pour les requêtes fréquentes (stats, chatbot, graphique)
CREATE INDEX IF NOT EXISTS idx_releves_date         ON releves(date);
CREATE INDEX IF NOT EXISTS idx_releves_date_desc    ON releves(date DESC);
CREATE INDEX IF NOT EXISTS idx_releves_pompe        ON releves(pompe_id);
CREATE INDEX IF NOT EXISTS idx_releves_date_periode ON releves(date, periode);
CREATE INDEX IF NOT EXISTS idx_releves_date_pompe   ON releves(date, pompe_id);


-- ── 4. TRIGGER : updated_at auto ─────────────────────────────────
CREATE OR REPLACE FUNCTION trg_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS releves_set_updated_at ON releves;
CREATE TRIGGER releves_set_updated_at
    BEFORE UPDATE ON releves
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_updated_at();


-- ── 5. VUE : ventes_journalieres ─────────────────────────────────
-- Simplifie les requêtes d'agrégation (chatbot, tableau de bord)
CREATE OR REPLACE VIEW ventes_journalieres AS
SELECT
    r.date,
    r.periode,
    po.id                                        AS pompe_id,
    po.nom                                       AS pompe_nom,
    pr.id                                        AS produit_id,
    pr.nom                                       AS produit_nom,
    r.prix_gallon,
    r.metter_avant,
    r.metter_apres,
    (r.metter_apres - r.metter_avant)            AS quantite,
    (r.metter_apres - r.metter_avant)
        * r.prix_gallon                          AS montant_vente
FROM   releves   r
JOIN   pompes    po ON po.id = r.pompe_id
JOIN   produits  pr ON pr.id = po.produit_id;

COMMENT ON VIEW ventes_journalieres IS 'Relevés enrichis avec noms et calculs de quantité/montant.';


-- ── 6. DONNÉES INITIALES ─────────────────────────────────────────
-- Insère les produits et pompes de base. Idempotent (ON CONFLICT DO NOTHING).
INSERT INTO produits (nom, prix_gallon) VALUES
    ('Gazoline', 900.000),
    ('Diesel',  1000.000)
ON CONFLICT (nom) DO NOTHING;

DO $$
DECLARE
    gaz_id INTEGER;
    die_id INTEGER;
BEGIN
    SELECT id INTO gaz_id FROM produits WHERE nom = 'Gazoline';
    SELECT id INTO die_id FROM produits WHERE nom = 'Diesel';

    IF gaz_id IS NOT NULL THEN
        INSERT INTO pompes (produit_id, nom) VALUES
            (gaz_id, 'Gazoline 1'),
            (gaz_id, 'Gazoline 2')
        ON CONFLICT (produit_id, nom) DO NOTHING;
    END IF;

    IF die_id IS NOT NULL THEN
        INSERT INTO pompes (produit_id, nom) VALUES
            (die_id, 'Diesel 1'),
            (die_id, 'Diesel 2')
        ON CONFLICT (produit_id, nom) DO NOTHING;
    END IF;
END;
$$;
