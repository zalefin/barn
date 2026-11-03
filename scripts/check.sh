#!/bin/bash
# Local checks: pytest, terraform fmt/validate on the packaged roots, shellcheck on
# the bootstrap scripts (the user-data template is rendered with dummy values first).
set -euo pipefail
cd "$(dirname "$0")/.."
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$PWD/.state/plugin-cache}"
mkdir -p "$TF_PLUGIN_CACHE_DIR"

uv run pytest -q

pkg=src/barn
terraform fmt -check -recursive "$pkg/terraform"
for root in "$pkg/terraform/volume" "$pkg/terraform/instance"; do
  TF_DATA_DIR="$PWD/.state/validate/$(basename "$root")" terraform -chdir="$root" init -backend=false -input=false >/dev/null
  TF_DATA_DIR="$PWD/.state/validate/$(basename "$root")" terraform -chdir="$root" validate -no-color
done

rendered=$(mktemp)
trap 'rm -f "$rendered"' EXIT
uv run python - "$pkg/bootstrap/user-data.sh.tftpl" > "$rendered" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
text = re.sub(r"(?<!\$)\$\{[a-z_]+\}", "x", text).replace("$${", "${")
print(text)
PY
# shellcheck from PATH, or the shellcheck-py wheel through uvx (nothing installed).
if command -v shellcheck >/dev/null; then sc=(shellcheck); else sc=(uvx --from shellcheck-py shellcheck); fi
"${sc[@]}" -s bash "$rendered" "$pkg/bootstrap/idle-check.sh" "$pkg/bootstrap/startup-default.sh" "$pkg/templates/startup.sh"
echo "all checks passed"
