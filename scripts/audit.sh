#!/usr/bin/env bash
# Audit from a fresh clone: run the targets a reviewer would run, then check that they changed nothing,
# that the frozen splits are intact and that no secret is in any commit.
#
#   scripts/audit.sh [REPOSITORY] [REF]     defaults: this checkout's origin, main
#
# Needs git, make, uv and curl; downloads gitleaks 8.28.0 (checksum verified) unless GITLEAKS points to one.
# Runs offline from the clone onwards except for uv's package downloads: no API key is read or needed.
set -euo pipefail

repo=${1:-$(git -C "$(dirname "$0")/.." remote get-url origin)}
ref=${2:-main}
work=$(mktemp -d)
echo "audit directory: $work"

step() { printf '\n==> %s\n' "$*"; }

step "fresh clone of $repo at $ref (full history)"
git clone --quiet "$repo" "$work/repo"
cd "$work/repo"
git checkout --quiet "$ref"
git log -1 --format='commit %H (%cd)' --date=iso-strict

step "make setup lint test eval-offline gate"
env -u PRIORAUTH_ANTHROPIC_API_KEY -u ANTHROPIC_API_KEY LLM_MODE=replay make setup lint test eval-offline gate

step "nothing the targets wrote differs from the commit (results/ replayed byte for byte)"
if [ -n "$(git status --porcelain)" ]; then
  git status --short
  echo "FAIL: the working tree changed"
  exit 1
fi
echo "working tree clean"

step "data/ equals a fresh generation, and the frozen digests match their splits"
make data-check

step "the frozen digests were committed once and never changed"
for f in data/test.sha256 data/robust.sha256; do
  commits=$(git log --format=%h -- "$f")
  if [ "$(echo "$commits" | wc -l)" -ne 1 ]; then
    echo "FAIL: $f was changed after it was first committed: $commits"
    exit 1
  fi
  echo "$f: committed once ($commits), $(cut -c1-16 "$f")..."
done

step "secret scan of every commit and of the working tree (gitleaks)"
gitleaks=${GITLEAKS:-}
if [ -z "$gitleaks" ]; then
  v=8.28.0
  base=https://github.com/gitleaks/gitleaks/releases/download/v$v
  (
    cd "$work"
    curl -sSfLO "$base/gitleaks_${v}_linux_x64.tar.gz"
    curl -sSfLO "$base/gitleaks_${v}_checksums.txt"
    sha256sum --check --ignore-missing "gitleaks_${v}_checksums.txt"
    tar -xzf "gitleaks_${v}_linux_x64.tar.gz" gitleaks
  )
  gitleaks=$work/gitleaks
fi
"$gitleaks" version
echo "commits scanned: $(git rev-list --all | wc -l)"
"$gitleaks" git --redact --no-banner --exit-code 1 .
"$gitleaks" dir --redact --no-banner --exit-code 1 .

step "audit passed for $(git rev-parse --short HEAD)"
