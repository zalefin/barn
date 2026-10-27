#!/bin/bash
# Local checks: pytest, terraform fmt/validate on every root, shellcheck on the
# bootstrap scripts (the user-data template is rendered with dummy values first).
set -euo pipefail
cd "$(dirname "$0")/.."
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$PWD/.state/plugin-cache}"
mkdir -p "$TF_PLUGIN_CACHE_DIR"

uv run pytest -q

terraform fmt -check -recursive terraform
for root in terraform/platform terraform/modules/developer-policy terraform/volume terraform/instance terraform/test-users; do
  TF_DATA_DIR=".state/validate/$(basename "$root")" terraform -chdir="$root" init -backend=false -input=false >/dev/null
  TF_DATA_DIR=".state/validate/$(basename "$root")" terraform -chdir="$root" validate -no-color
done

rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
uv run python - bootstrap/user-data.sh.tftpl > "$rendered" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
text = re.sub(r"(?<!\$)\$\{[a-z_]+\}", "x", text).replace("$${", "${")
print(text)
PY
shellcheck -s bash "$rendered" bootstrap/idle-check.sh
echo "all checks passed"
