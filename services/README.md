# Futura Services généraux

Module du portail Futura (même connexion que KPI, Flotte et Factures).

**Personnel** — visas, cartes de séjour (alerte avant expiration, suivi du renouvellement ; le nouveau document remplace l'ancien), billets d'avion (saisis par la RH).
**Logements** — loyers (échéances calculées depuis le bail : seuls les paiements sont saisis), factures d'électricité (ENEO) et d'eau (CAMWATER), réparations signalées par les occupants.

Rôles : administrateur, gestionnaire (RH / services généraux, saisie), direction (consultation), occupant (voit son logement, signale les pannes).

## Lancer en local
```bash
pip install -r requirements.txt
flask --app app seed-demo        # données de démonstration (mot de passe : Futura2026)
flask --app app run --debug
```

## Production (Railway)
Variables : `SECRET_KEY`, `DATABASE_URL` (PostgreSQL), `ADMIN_EMAIL` / `ADMIN_PASSWORD` (premier démarrage),
`SSO_SECRET` (identique à `MODULE_SERVICES_SECRET` du portail KPI), `PORTAIL_URL` (lien retour vers le portail).
Côté portail KPI : `MODULE_SERVICES_URL` et `MODULE_SERVICES_SECRET`.

Au démarrage, `migrer()` crée les tables manquantes sans toucher aux données existantes.
Réglage des délais d'alerte : menu « Réglages des alertes » (administrateur).

## Tests
```bash
python -m pytest -q
```
