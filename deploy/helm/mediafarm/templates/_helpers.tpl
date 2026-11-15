{{- define "mf.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mf.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "mf.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mf.labels" -}}
app.kubernetes.io/name: {{ include "mf.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mf.selector" -}}
app.kubernetes.io/name: {{ include "mf.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mf.secretName" -}}
{{- if .Values.existingSecret -}}{{ .Values.existingSecret }}{{- else -}}{{ include "mf.fullname" . }}-secrets{{- end -}}
{{- end -}}

{{- define "mf.image" -}}
{{- printf "%s/%s:%s" .root.Values.image.registry .name .root.Values.image.tag -}}
{{- end -}}