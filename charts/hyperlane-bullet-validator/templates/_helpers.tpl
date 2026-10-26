{{/*
Chart name, truncated to the 63-character DNS label limit.
*/}}
{{- define "hyperlane-bullet-validator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. A release name that already contains the chart name
is used as is.
*/}}
{{- define "hyperlane-bullet-validator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart name and version, for the helm.sh/chart label.
*/}}
{{- define "hyperlane-bullet-validator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "hyperlane-bullet-validator.labels" -}}
helm.sh/chart: {{ include "hyperlane-bullet-validator.chart" . }}
{{ include "hyperlane-bullet-validator.selectorLabels" . }}
app.kubernetes.io/version: {{ include "hyperlane-bullet-validator.versionLabel" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels. Immutable on a Deployment, so never add to these.
*/}}
{{- define "hyperlane-bullet-validator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hyperlane-bullet-validator.name" . | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "hyperlane-bullet-validator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "hyperlane-bullet-validator.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image tag: `image.tag`, else the chart's appVersion.
*/}}
{{- define "hyperlane-bullet-validator.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag }}
{{- end }}

{{/*
The image tag as a label value: at most 63 alphanumerics, "-", "_" or ".",
starting and ending with an alphanumeric. Other characters become "-". A tag
such as "_custom" is valid for an image but not for a label. The digest never
appears here: it is not a label value and the tag still names the version.
*/}}
{{- define "hyperlane-bullet-validator.versionLabel" -}}
{{- $tag := include "hyperlane-bullet-validator.imageTag" . }}
{{- $tag = regexReplaceAll "[^-A-Za-z0-9_.]" $tag "-" | trunc 63 }}
{{- regexReplaceAll "^[^A-Za-z0-9]+|[^A-Za-z0-9]+$" $tag "" }}
{{- end }}

{{/*
Image reference. A digest pins the image and takes precedence over the tag.
*/}}
{{- define "hyperlane-bullet-validator.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (include "hyperlane-bullet-validator.imageTag" .) }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding the validator key, and the key within it. Empty when
no key is configured (readonly mode).
*/}}
{{- define "hyperlane-bullet-validator.keySecretName" -}}
{{- if .Values.validator.key.existingSecret }}
{{- .Values.validator.key.existingSecret }}
{{- else if .Values.validator.key.value }}
{{- include "hyperlane-bullet-validator.fullname" . }}
{{- end }}
{{- end }}

{{- define "hyperlane-bullet-validator.keySecretKey" -}}
{{- if .Values.validator.key.existingSecret }}
{{- .Values.validator.key.existingSecretKey }}
{{- else }}
{{- "validator-key" }}
{{- end }}
{{- end }}

{{/*
Fail-guards, rendered (to nothing) by every template. The validator binary
falls back to the mainnet configuration embedded in it for any setting that is
not passed, so every setting that selects a network is required here.
*/}}
{{- define "hyperlane-bullet-validator.validate" -}}
{{- $v := .Values }}
{{- $mode := required "validator.mode is required: set it to \"signer\" (sign and upload checkpoints) or \"readonly\" (no key, writes nothing)" $v.validator.mode }}
{{- $_ := required "sequencerUrl is required (e.g. https://rollup.mainnet.bullet.xyz)" $v.sequencerUrl }}
{{- $_ := required "s3.bucket is required" $v.s3.bucket }}
{{- $_ := required "s3.region is required" $v.s3.region }}
{{- $_ := required "s3.prefix is required" $v.s3.prefix }}
{{- if le (int64 $v.mailboxDomain) 0 }}
{{- fail "mailboxDomain is required (Bullet mainnet: 5555, Bullet testnet: 5556)" }}
{{- end }}
{{- $hasKey := or $v.validator.key.existingSecret $v.validator.key.value }}
{{- if and $v.validator.key.existingSecret $v.validator.key.value }}
{{- fail "set only one of validator.key.existingSecret and validator.key.value" }}
{{- end }}
{{- if and (eq $mode "signer") (not $hasKey) }}
{{- fail "validator.mode=signer needs a key: set validator.key.existingSecret (recommended) or validator.key.value" }}
{{- end }}
{{- if and (eq $mode "readonly") $hasKey }}
{{- fail "validator.mode=readonly must not be given a key: unset validator.key.existingSecret and validator.key.value, or use validator.mode=signer" }}
{{- end }}
{{- if eq (int64 $v.ports.metrics) (int64 $v.ports.health) }}
{{- fail "ports.metrics and ports.health must differ" }}
{{- end }}
{{- range $v.extraEnv }}
{{- if or (hasPrefix "HYPERLANE_VALIDATOR_" .name) (eq .name "RUST_LOG") }}
{{- fail (printf "extraEnv must not set %s: the chart manages HYPERLANE_VALIDATOR_* and RUST_LOG through its values" .name) }}
{{- end }}
{{- end }}
{{- $selectorLabels := include "hyperlane-bullet-validator.selectorLabels" . | fromYaml }}
{{- range $key, $_ := $v.podLabels }}
{{- if hasKey $selectorLabels $key }}
{{- fail (printf "podLabels must not set %s: the Deployment and Service select pods by it" $key) }}
{{- end }}
{{- end }}
{{- if and $v.serviceMonitor.enabled (not $v.service.enabled) }}
{{- fail "serviceMonitor.enabled requires service.enabled" }}
{{- end }}
{{- end }}
