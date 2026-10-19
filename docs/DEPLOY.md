# Deploying the public demo

The demo is two containers: the **API** (FastAPI, workers in-process, SQLite seeded at start) on **Azure Container
Apps**, and the **UI** (Next.js) on **Vercel**. `.github/workflows/deploy.yml` builds and deploys both on every push
to `main` (and on demand), then runs `make smoke-demo` against the live URLs; it runs that smoke test every night
too and fails if it breaks. Until the resources, secrets and variables below exist, its deploy jobs are skipped with
a notice; nothing else in the repository depends on them.

Synthetic data only. The demo keeps its state in the container: **every restart resets it** (the UI and the
README say so). No model is called by default: the examples are decided from real model responses recorded in
`eval/cassettes/agent_v2_haiku_pipeline/dev.jsonl` and replayed, so no API key is needed.

Nothing here has been run by the author of this file: there was no access to Azure or Vercel. The images, the
compose file and `make smoke-demo` are tested in CI (`.github/workflows/ci.yml`, job `demo-images`); the commands
below follow the Azure CLI and Vercel CLI documentation and have not been executed.

## 0. What you need

- An Azure subscription where you can create resource groups and app registrations; the Azure CLI (`az`, 2.60 or
  later) logged in with `az login`.
- A Vercel account; Node.js 22.
- Admin rights on the GitHub repository (secrets and variables); optionally the GitHub CLI `gh`.

Pick the names once (the registry name must be globally unique, lowercase letters and digits):

```bash
SUB=<your subscription id>
RG=priorauth-demo
LOC=westeurope
ACR=<unique registry name, e.g. priorauthdemo1234>
ENVNAME=priorauth-demo-env
APP=priorauth-api
REPO=26sneakysnake/priorauth-agent
az account set --subscription "$SUB"
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

## 1. Azure: registry, environment, container app

Run from the root of a clone of the repository:

```bash
az group create --name "$RG" --location "$LOC"
az acr create --name "$ACR" --resource-group "$RG" --sku Basic
az containerapp env create --name "$ENVNAME" --resource-group "$RG" --location "$LOC"

# First image (the workflow builds the next ones). The build runs in Azure: no local Docker needed.
az acr build --registry "$ACR" --image priorauth-api:initial --file docker/api.Dockerfile .

az containerapp create --name "$APP" --resource-group "$RG" --environment "$ENVNAME" \
  --image "$ACR.azurecr.io/priorauth-api:initial" \
  --registry-server "$ACR.azurecr.io" --registry-identity system \
  --ingress external --target-port 8000 \
  --min-replicas 1 --max-replicas 1 --cpu 0.5 --memory 1.0Gi \
  --env-vars DEMO_LIVE_ENABLED=false DEMO_LIVE_TRUST_FORWARDED_FOR=true

API_URL="https://$(az containerapp show --name "$APP" --resource-group "$RG" \
  --query properties.configuration.ingress.fqdn --output tsv)"
curl -s "$API_URL/healthz"          # {"status":"ok"} once the examples are seeded (about 10 s after start)
```

**Replicas: keep `--min-replicas 1 --max-replicas 1`.**
- `max 1`: each replica has its own SQLite file and its own random token-signing secret, so a second replica would
  not know the first one's sessions or cases.
- `min 1`: a scale-to-zero demo would start cold (image pull and seeding) for the first visitor and lose its state
  each time. The cost is one always-on 0.5 vCPU / 1 GiB replica; to save it, set `--min-replicas 0` and accept a
  slow first request after idle periods.

Optional settings of the container app (`az containerapp update --name "$APP" --resource-group "$RG"
--set-env-vars ...`):

| Variable | Default | Effect |
|---|---|---|
| `PRIORAUTH_DEMO_REVIEWER_PASSWORD` | `synthetic-demo-only` | the public reviewer password (shown on the login page) |
| `PRIORAUTH_CORS_ORIGINS` | empty | browser origins allowed to call the API directly; not needed, see §4 |
| `DEMO_LIVE_ENABLED` | `false` | "your own record" mode with the real model, see §5 |

## 2. Azure: let the workflow deploy (OpenID Connect, no stored Azure password)

```bash
CLIENT_ID=$(az ad app create --display-name priorauth-demo-deploy --query appId --output tsv)
az ad sp create --id "$CLIENT_ID"
az role assignment create --assignee "$CLIENT_ID" --role Contributor \
  --scope "/subscriptions/$SUB/resourceGroups/$RG"
az ad app federated-credential create --id "$CLIENT_ID" --parameters "{
  \"name\": \"github-main\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:$REPO:ref:refs/heads/main\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
TENANT_ID=$(az account show --query tenantId --output tsv)
```

The federated credential covers workflows running on `main` (pushes and manual runs from `main`).

## 3. Vercel: the UI project

```bash
npm install --global vercel@62.2.0
vercel login
vercel link            # at the repository root; create a new project, e.g. priorauth-review
```

Then in the Vercel dashboard, project **Settings**:

- **General → Root Directory**: `ui`. Framework preset: Next.js. Node.js version: 22.x.
- **Environment Variables** (Production): `PRIORAUTH_API_URL` = the `API_URL` of §1 (`https://...`, no trailing
  slash). The UI's `/api/*` route forwards to it at request time.
- **Git**: do not connect the repository (or disable automatic deployments), so that only the workflow deploys.
- **Account Settings → Tokens**: create a token for GitHub Actions.

`vercel link` wrote `.vercel/project.json` (git-ignored): its `orgId` and `projectId` are the two IDs below. The UI
URL is `https://<project>.vercel.app` (shown by `vercel ls` or in the dashboard).

## 4. CORS between the UI and the API

None is needed: the browser only talks to the UI's origin, and the UI's server-side route `/api/[...path]` forwards
to the API (it also forwards `X-Forwarded-For`, which Vercel sets to the visitor's address, for the live mode's
per-IP limit). The API's own `/docs` page is served by the API, same origin. Set `PRIORAUTH_CORS_ORIGINS` (comma-
separated origins, e.g. `https://priorauth-review.vercel.app`) only if a browser page on another origin must call
the API directly.

## 5. Live mode ("your own record"), optional and off by default

It sends a visitor's text to the real model. Leave it off unless you accept the spend:

```bash
az containerapp secret set --name "$APP" --resource-group "$RG" --secrets anthropic-key=<your API key>
az containerapp update --name "$APP" --resource-group "$RG" --set-env-vars \
  PRIORAUTH_ANTHROPIC_API_KEY=secretref:anthropic-key DEMO_LIVE_ENABLED=true \
  DEMO_LIVE_DAILY_CAP_USD=1.0 DEMO_LIVE_PER_IP_PER_HOUR=5 DEMO_LIVE_MAX_CHARS=12000
```

Guards (`src/priorauth/demo/live.py`): daily spend cap for all visitors, per-IP rate limit, text size limit,
fail-closed (an error or a cap gives no decision and sends the visitor back to the examples), nothing stored. The
counters are in memory: a restart resets them.

## 6. GitHub: secrets and variables

Repository **Settings → Secrets and variables → Actions** (or with `gh`):

```bash
gh secret set AZURE_CLIENT_ID --body "$CLIENT_ID" --repo "$REPO"
gh secret set AZURE_TENANT_ID --body "$TENANT_ID" --repo "$REPO"
gh secret set AZURE_SUBSCRIPTION_ID --body "$SUB" --repo "$REPO"
gh secret set VERCEL_TOKEN --repo "$REPO"            # paste the token
gh secret set VERCEL_ORG_ID --body "<orgId>" --repo "$REPO"
gh secret set VERCEL_PROJECT_ID --body "<projectId>" --repo "$REPO"
gh variable set AZURE_RESOURCE_GROUP --body "$RG" --repo "$REPO"
gh variable set AZURE_ACR_NAME --body "$ACR" --repo "$REPO"
gh variable set AZURE_CONTAINERAPP_NAME --body "$APP" --repo "$REPO"
gh variable set DEMO_API_URL --body "$API_URL" --repo "$REPO"
gh variable set DEMO_UI_URL --body "https://<project>.vercel.app" --repo "$REPO"
```

## 7. Deploy and check

- **Actions → Deploy demo → Run workflow** (or push to `main`). Jobs: `API -> Azure Container Apps`,
  `UI -> Vercel`, then `smoke test of the live demo`, which must be green.
- From any machine: `make smoke-demo URL=$API_URL UI_URL=https://<project>.vercel.app`.
- Open the UI: the login page shows the demo account; **Try it** runs the examples.
- The nightly run (04:17 UTC) repeats the smoke test and fails if the demo is down or wrong.

## 8. Teardown

```bash
az group delete --name "$RG" --yes
az ad app delete --id "$CLIENT_ID"
vercel remove <project> --yes
```
