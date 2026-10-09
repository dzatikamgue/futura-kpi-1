# Futura Performance — suivi mensuel des KPI du personnel

Application RH de FUTURA : chaque N+1 note ses N-1 chaque mois (critères de 0 à 100), avec un suivi trimestriel et annuel. L'accès est cloisonné par département et par projet.

## Fonctionnalités

| Domaine | Ce que fait l'application |
|---|---|
| **Notation mensuelle** | Grille KPI configurable (0–100 par critère, coefficients 1–5), note globale pondérée sur 100, commentaire par critère et appréciation générale, brouillon puis soumission. |
| **Multi-entités** | Plusieurs entreprises du groupe dans la même application : chacune a son personnel, ses départements, projets, postes, sa grille KPI, son logo et sa couleur. Onglets en haut pour passer d'une entité à l'autre. |
| **Accès par entité** | Un compte voit une, deux ou plusieurs entités, avec un rôle propre à chacune (Direction ou Collaborateur). La RH du groupe voit toutes les entités. Seuls la RH et les superadmins créent des entités. |
| **Qui note** | Uniquement le N+1 désigné par la RH sur chaque affectation, même si le salarié est sur le projet d'un autre responsable. Le responsable d'un projet / département consulte les notes de son périmètre sans pouvoir noter (sauf s'il est désigné N+1). |
| **Confidentialité** | Un collaborateur ne voit que ses N-1 désignés, les entités qu'il dirige et ses propres notes soumises. RH et Direction voient tout. |
| **Double rattachement** | Un salarié peut être affecté à un département **et** à un ou plusieurs projets, avec un N+1 par affectation. Chaque note reste visible dans son seul contexte. |
| **Suivi** | Vues mensuelle, trimestrielle et annuelle (carte de chaleur), filtres département / projet, exports Excel et PDF. |
| **Import du personnel** | Saisie manuelle, modèle Excel (lu sans IA), ou photo / PDF / Excel libre / texte collé lu par **Claude**. La RH valide sur un écran de vérification avant tout enregistrement. |
| **Saisie rapide** | Fiche salarié en un écran : poste, département, projet et N+1 choisis dans des listes déroulantes. Matricule automatique si vide. |
| **Comptes** | L'identifiant est l'e-mail de la fiche. Quand la RH désigne un N+1 qui a un e-mail, son compte est créé ; un compte existant est relié à la fiche portant le même e-mail. La RH génère les mots de passe en un clic. |
| **Sécurité** | Mots de passe hachés (jamais relisibles), attribués par le superadmin uniquement et non modifiables par les utilisateurs ; bouton pour afficher le mot de passe saisi ; CSRF, blocage après 5 échecs, en-têtes CSP/HSTS, journal des actions. |
| **Terrain** | Responsive (téléphone de chantier) ; la saisie est sauvegardée sur l'appareil en cas de coupure réseau ; polices et graphiques servis localement (aucun CDN). |

**Barème** (note globale et critères, sur 100) : Excellent ≥ 80 · Très bien ≥ 70 · Bien ≥ 60 · Passable ≥ 50 · Insuffisant < 50. Un critère noté sous 40 doit être justifié par un commentaire.

**Calendrier** : le mois en cours est ouvert à la saisie, ainsi que le mois précédent jusqu'au 10 (`SAISIE_JOUR_LIMITE`). Au-delà, seule la RH peut rouvrir une évaluation, avec un motif journalisé.

## Lancer en local

```bash
python -m venv .venv && source .venv/bin/activate      # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                    # puis éditer SECRET_KEY
export FLASK_APP=wsgi.py                                # Windows : set FLASK_APP=wsgi.py
flask db upgrade
flask demo                 # optionnel : données de démonstration (mot de passe Futura2026!)
flask run                  # http://127.0.0.1:5000
```

Comptes de démonstration : `christine.mballa@futura-demo.cm` (RH), `paul.nguema@futura-demo.cm` (Direction), `jean-marc.fotso@futura-demo.cm` (chef de projet BALI).

Tests : `pytest -q` (30 tests : périmètres d'accès, flux de notation, import, comptes, clé API, multi-entités, sécurité, exports).

## Déployer : GitHub → Railway

1. **GitHub** : créez un dépôt **privé**, puis :
   ```bash
   git init && git add . && git commit -m "Futura Performance v1"
   git branch -M main && git remote add origin https://github.com/<compte>/futura-kpi.git
   git push -u origin main
   ```
2. **Railway** : *New Project* → *Deploy from GitHub repo* → choisissez le dépôt.
3. Dans le projet : *+ New* → *Database* → **PostgreSQL**.
4. Dans le service web → *Variables* :

   | Variable | Valeur |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (référence Railway) |
   | `APP_ENV` | `production` |
   | `SECRET_KEY` | chaîne aléatoire longue (`python -c "import secrets;print(secrets.token_hex(32))"`) |
   | `ADMIN_EMAIL` | e-mail du compte RH initial |
   | `ADMIN_PASSWORD` | mot de passe initial (≥ 10 caractères), à changer à la 1re connexion |
   | `SUPERADMIN_EMAILS` | facultatif : e-mails des superadmins, séparés par des virgules (`ADMIN_EMAIL` est toujours superadmin) |
   | `ANTHROPIC_API_KEY` | facultatif : la clé peut aussi être saisie dans l'application (*Clé API Claude*) |
   | `ANTHROPIC_MODEL` | facultatif, par défaut `claude-sonnet-5-5` |

5. *Settings* → *Networking* → **Generate Domain**.

Au démarrage, `start.sh` applique les migrations, crée le compte RH initial et la grille KPI par défaut, puis lance gunicorn. Railway surveille `/sante`.

**Ne lancez jamais `flask demo` en production.**

## Premiers pas après déploiement

1. Connectez-vous avec `ADMIN_EMAIL`, puis changez le mot de passe. Créez votre propre fiche salarié **avec le même e-mail** : votre compte y est relié automatiquement et vous pouvez noter vos N-1.
2. **Clé API Claude** (menu Administration RH) : collez la clé `sk-ant-…` créée sur console.anthropic.com. Elle est vérifiée, puis stockée chiffrée. Si `ANTHROPIC_API_KEY` est définie sur Railway, elle reste prioritaire.
3. **Départements**, **Projets** (avec leur responsable) et **Postes**.
4. **Personnel** : importez la liste, ou saisissez chaque salarié (poste, département / projet, N+1 dans des listes).
5. **Comptes & accès** → « Générer les accès », puis transmettez identifiants et mots de passe temporaires.

**Clé API et SECRET_KEY** : la clé saisie dans l'application est chiffrée avec une clé dérivée de `SECRET_KEY`. Si vous changez `SECRET_KEY`, ressaisissez la clé API.

## Structure

```
app/
  models.py          Modèle de données (Employe, Affectation, Evaluation, EvaluationNote…)
  permissions.py     Règles de périmètre — toute liste passe par ici
  blueprints/        Écrans : auth, tableau_bord, evaluations, personnel, suivi, import, parametres, journal
  services/          notation, statistiques, exports, import_ia (Claude), comptes (liaison auto),
                     organisation (affectations, postes), reglages (clé API chiffrée), audit
  templates/ static/ Interface (design system dans static/css/app.css)
migrations/          Alembic (flask db migrate / upgrade)
tests/               pytest
```

Pour modifier le schéma : changez `models.py`, puis lancez `flask db migrate -m "description"` et versionnez le fichier généré.

## Multi-entités

- **Entité principale** : les données existantes (avant le multi-entités) forment l'entité principale (FUTURA). Elles ne sont pas réécrites : leur colonne `entite_id` reste vide, ce qui veut dire « entité principale ».
- **Créer une entité** : *Administration RH → Entités*. Saisissez le nom, un code court (préfixe des matricules, ex. `ABC-0001`) et le logo (PNG, JPG ou WEBP). La couleur de la charte est déduite du logo, modifiable. La grille KPI par défaut est copiée pour l'entité.
- **Rôles** :
  - *Superadmin* (variables `ADMIN_EMAIL`, `SUPERADMIN_EMAILS`) : tout, y compris nommer la RH.
  - *RH du groupe* : toutes les entités, administration, création d'entités.
  - *Direction* / *Collaborateur* : rôle par entité, réglé dans *Comptes & accès → Gérer*.
- **N+1 croisé** : la RH peut désigner comme N+1 un salarié d'une autre entité ; il obtient automatiquement un onglet vers cette entité, limité aux personnes qu'il note.

## Direction et RH visibles dans tout le groupe

- Les titulaires d'un poste de direction ou RH (directeur, directrice, DG, DGA, PDG, gérant, DRH, responsable ressources humaines…) et les comptes « RH du groupe » apparaissent dans le *Personnel* de **toutes** les entités, avec la mention « Groupe, rattaché à … ». Ils sont aussi proposés comme responsables de départements / projets et comme N+1 partout.
- *Postes* : cliquez sur « Entité » / « Tout le groupe » pour ajouter ou retirer un poste (ex. un « Directeur de chantier » qui ne concerne qu'une entité). Réglage mémorisé dans la table des réglages existante : aucune modification de structure.
- Ces personnes restent rattachées à leur entité d'origine : depuis une autre entité, elles ne sont pas incluses dans les actions groupées.

## Rattachement par listes déroulantes

- Création **et** modification d'une fiche : Poste, Département, Projet et N+1 se choisissent dans des listes. Changer le département ou le projet clôture l'ancienne affectation (historique conservé).
- Sur la fiche, « Ajouter » propose deux listes séparées (Département, Projet) et le N+1.

## Sélection et suppression du personnel

- *Personnel* : cochez des salariés (ou la case d'en-tête pour toute la page, puis « Sélectionner les N résultats » pour tout le filtre). Une barre d'actions apparaît : **Réactiver**, **Désactiver**, **Supprimer…**.
- Une fiche se supprime aussi seule depuis sa page (bouton **Supprimer**).
- **Désactiver** conserve tout l'historique (recommandé pour un départ). **Supprimer** est définitif : la fiche, ses évaluations reçues, ses affectations et son compte sont effacés. Les évaluations qu'elle a données à d'autres restent (sans nom d'évaluateur), ses N-1 passent « N+1 à désigner ».
- Avant suppression, l'écran récapitule l'impact ; il faut taper **SUPPRIMER** pour confirmer. Chaque suppression est inscrite au journal. Votre propre fiche et celles des superadmins sont protégées. Réservé à la RH et aux superadmins.

## Mots de passe

- Seul le **superadmin** attribue les mots de passe (*Comptes & accès* → « Attribuer / Changer le mot de passe », saisi ou généré). Il s'affiche une seule fois : transmettez-le.
- Les utilisateurs **ne peuvent pas** changer leur mot de passe. En cas de perte ou de doute, le superadmin en attribue un nouveau.
- Le superadmin choisit son propre mot de passe (*Mot de passe* en bas du menu).

## Mises à jour sans toucher aux données

Règle du projet : une mise à jour du code ne modifie jamais les données existantes.
- Les migrations sont **additives** uniquement (nouvelles tables / colonnes vides) ; aucune ligne existante n'est lue ni réécrite.
- Au démarrage, `flask init-admin` ne crée le compte RH et la grille KPI que s'ils n'existent pas ; il ne lie ni ne crée aucun autre compte.
- Les comptes ne sont créés / reliés que lorsque la RH agit (fiche enregistrée, N+1 désigné, import validé, bouton « Resynchroniser »).
- La liste des postes par défaut est proposée dans les menus sans être écrite en base.
