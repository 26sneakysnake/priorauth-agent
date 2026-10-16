# CLAUDE.md — priorauth-agent

Projet de preuve de travail pour une candidature « AI Engineer / Member of Technical Staff » dans une startup
d'agents IA pour l'assurance santé américaine. Auteur : Seifeddine BEN RHOUMA. Le dépôt sera lu par une fondatrice
qui valide « sur preuves concrètes de développement ». Chaque ligne du dépôt doit se défendre en entretien.

## Règles non négociables
1. **Données 100 % synthétiques.** Aucun vrai patient, aucune vraie note, aucun scraping de données médicales.
   Noms, identifiants et dates sont fictifs. Le README le dit en première section.
2. **Le LLM ne décide jamais seul.** L'agent extrait des faits avec preuve (page + citation). Un moteur de politique
   déterministe décide : `APPROVE` uniquement si tous les critères sont satisfaits, vérifiés et sans conflit.
   Sinon `PEND` avec les raisons. Il n'existe pas de décision `DENY` dans le code.
3. **Aucune affirmation sans preuve.** Toute phrase du README (chiffre, « conforme », « testé ») renvoie à un test, un
   fichier de résultats ou un workflow CI. Pas de chiffre écrit à la main : le README est généré depuis `results/`.
4. **Jamais « conforme HIPAA ».** Écrire « aligné sur les principes de la HIPAA Security Rule, données synthétiques,
   sans prétention de conformité ou de certification ». Idem pour FHIR : « R4, validé avec fhir.resources, non certifié ».
5. **Les limites sont publiées.** `docs/LIMITS.md` liste ce qui échoue (cas mal traités, taille des jeux de test,
   ce qui n'est pas fait). Les résultats sont donnés avec intervalle de confiance de Wilson à 95 %.
6. **Jeu de test figé.** `data/test/` n'est jamais utilisé pour régler un prompt ou un seuil. On itère sur `data/dev/`.
7. **Reproductible sans clé API.** Les appels LLM sont enregistrés (cassettes) et rejoués en CI. Le mode live est un
   workflow manuel avec plafond de coût.

## Stack
Python 3.12, uv, FastAPI, Pydantic v2, SQLAlchemy (SQLite en test, Postgres en compose et en cluster), asyncio,
Anthropic SDK (tool use, boucle manuelle), `fhir.resources`, OpenTelemetry, pytest, ruff, mypy.
UI : Next.js (App Router), TypeScript strict, Tailwind, client généré par `openapi-typescript`, Playwright.
Infra : Docker, docker compose, Kubernetes (kustomize, testé avec kind), GitHub Actions, Azure Container Apps (démo).
Code, commentaires, README et messages de commit en **anglais**.

## Méthode de travail (à chaque phase)
- Lire `PLAN.md`, exécuter **une seule phase** par session, en mode plan d'abord.
- Écrire les tests avec le code. Une phase est terminée quand ses critères d'acceptation passent.
- `make lint test` doit rester vert avant chaque commit. Un commit par tâche, message clair.
- Fin de phase : mettre à jour `docs/PROGRESS.md` (fait, mesuré, reste à faire) puis committer.
- Si une tâche dépasse 150 % de son budget temps, s'arrêter, le signaler et proposer de couper (voir « Seuils de coupe »).
- Ne jamais modifier `data/test/` ni `results/` à la main.

## Commandes cibles (Makefile)
`make setup` · `make lint` · `make test` · `make data` · `make eval-offline` · `make eval-live` ·
`make gate` · `make run` · `make up` · `make k8s-smoke` · `make readme`
