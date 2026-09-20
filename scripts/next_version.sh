#!/usr/bin/env bash
# The version this push produces, computed from Angular-convention commit subjects since the
# last tag: feat -> minor, fix/perf -> patch, a ! or BREAKING CHANGE -> major, anything else ->
# no release. Two callers, one rule: .github/workflows/release.yml tags what this prints when it
# is new; the production-deploy job in ci.yml stamps it into the landing footer and the deck
# BEFORE the release job has tagged it, so the deployed pages never sit one version behind.
#
#   scripts/next_version.sh          prints the version (vX.Y.Z), new or current
#   scripts/next_version.sh --bump   prints the bump kind: major | minor | patch | none
#
# Needs the tags and the full history (a shallow clone prints v0.0.0 / none).
set -euo pipefail
LAST=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
if [ -z "$LAST" ]; then RANGE=""; CUR="0.0.0"; else RANGE="${LAST}..HEAD"; CUR="${LAST#v}"; fi
LOG=$(git log --format=%B $RANGE 2>/dev/null || true)

BUMP=none
if [ -n "$(echo "$LOG" | tr -d '[:space:]')" ]; then
  echo "$LOG" | grep -qE '^(feat|fix|perf)(\(.+\))?!:|BREAKING CHANGE:' && BUMP=major
  [ "$BUMP" = none ] && echo "$LOG" | grep -qE '^feat(\(.+\))?:'        && BUMP=minor
  [ "$BUMP" = none ] && echo "$LOG" | grep -qE '^(fix|perf)(\(.+\))?:'  && BUMP=patch
fi

if [ "${1:-}" = "--bump" ]; then echo "$BUMP"; exit 0; fi
if [ "$BUMP" = none ]; then echo "${LAST:-v0.0.0}"; exit 0; fi

IFS=. read -r MA MI PA <<< "$CUR"
case "$BUMP" in
  major) MA=$((MA+1)); MI=0; PA=0 ;;
  minor) MI=$((MI+1)); PA=0 ;;
  patch) PA=$((PA+1)) ;;
esac
echo "v${MA}.${MI}.${PA}"
