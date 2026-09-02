#!/bin/sh
# Print the CHANGELOG.md section for one version (e.g. `scripts/release-notes.sh v0.0.7`).
# Used by the Publish workflow as the GitHub Release body. Fails if the section is missing
# so a release is never published with empty or wrong notes.
set -eu
tag="${1:?usage: release-notes.sh vX.Y.Z}"
changelog="$(dirname "$0")/../CHANGELOG.md"

notes="$(awk -v tag="$tag" '
  /^## / { in_section = ($2 == tag); next }
  in_section { print }
' "$changelog")"

if [ -z "$(printf '%s' "$notes" | tr -d '[:space:]')" ]; then
  echo "error: no '## $tag' section in CHANGELOG.md" >&2
  exit 1
fi
printf '%s\n' "$notes"
