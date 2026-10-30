{{- define "lab2-jaeger.selectorLabels" -}}
app.kubernetes.io/name: lab2-jaeger
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "lab2-jaeger.labels" -}}
{{ include "lab2-jaeger.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end }}
