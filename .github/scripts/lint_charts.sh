#!/usr/bin/env bash
# Lint and render every chart under charts/ with each ci/*-values.yaml, then
# check that invalid configurations fail to render. Works from any directory.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
charts_dir="$(cd "${script_dir}/../.." && pwd)/charts"

helm_bin="${HELM:-helm}"
command -v "${helm_bin}" >/dev/null || {
  echo "helm not found (set HELM to its path)" >&2
  exit 1
}
echo "Using $("${helm_bin}" version --short)"

log="$(mktemp)"
trap 'rm -f "${log}"' EXIT

failures=0
fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

# expect_render_failure <chart> <description> <expected stderr substring> [helm args...]
expect_render_failure() {
  local chart="$1" description="$2" expected="$3"
  shift 3
  if "${helm_bin}" template negative "${chart}" "$@" >/dev/null 2>"${log}"; then
    fail "${description}: rendered, but must fail"
  elif ! grep -Fq -- "${expected}" "${log}"; then
    fail "${description}: failed without the expected error '${expected}':"
    cat "${log}" >&2
  else
    echo "ok   ${description} (rejected as expected)"
  fi
}

shopt -s nullglob
charts=("${charts_dir}"/*/)
if ((${#charts[@]} == 0)); then
  echo "no charts found under ${charts_dir}" >&2
  exit 1
fi

for chart in "${charts[@]}"; do
  chart="${chart%/}"
  name="$(basename "${chart}")"
  echo "== ${name}"

  values_files=("${chart}"/ci/*-values.yaml)
  if ((${#values_files[@]} == 0)); then
    fail "${name}: no ci/*-values.yaml files"
    continue
  fi

  for values in "${values_files[@]}"; do
    case_name="${name}/ci/$(basename "${values}")"
    if ! "${helm_bin}" lint --strict "${chart}" --values "${values}"; then
      fail "helm lint ${case_name}"
    elif ! "${helm_bin}" template ci "${chart}" --values "${values}" \
      --api-versions monitoring.coreos.com/v1 >/dev/null; then
      fail "helm template ${case_name}"
    else
      echo "ok   ${case_name}"
    fi
  done

  if [[ "${name}" == "hyperlane-bullet-validator" ]]; then
    readonly_values="${chart}/ci/readonly-values.yaml"
    signer_values="${chart}/ci/signer-existing-secret-values.yaml"

    expect_render_failure "${chart}" "${name}: no values" \
      "validator.mode is required"
    expect_render_failure "${chart}" "${name}: signer without a key" \
      "validator.mode=signer needs a key" \
      --values "${signer_values}" --set validator.key.existingSecret=
    expect_render_failure "${chart}" "${name}: readonly with a key" \
      "validator.mode=readonly must not be given a key" \
      --values "${readonly_values}" --set validator.key.existingSecret=some-secret
    expect_render_failure "${chart}" "${name}: podLabels overriding a selector label" \
      "podLabels must not set app.kubernetes.io/name" \
      --values "${readonly_values}" --set 'podLabels.app\.kubernetes\.io/name=x'
  fi
done

if ((failures > 0)); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "All chart checks passed."
