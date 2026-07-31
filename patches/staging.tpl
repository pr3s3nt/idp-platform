{{/*
  PATCH TEMPLATE — STAGING
  Applied to every manifest score-k8s generates (score-k8s init --patch-templates ...).
  This is the ONLY place environment differences live. App developers never touch it.
  Replaces the role of a Kustomize overlay.

  RULES:
  - Manifests labelled app.kubernetes.io/component=datastore (emitted by provisioners:
    postgres/redis/backup/...) are left alone for replicas/resources — a datastore
    declares its own sizing inside the provisioner catalog.
  - resources are only set on containers that DON'T already declare them, so anything
    a provisioner or a developer specified explicitly survives.
  - imagePullSecrets are injected into every pod template (Deployment/StatefulSet/CronJob).
    The secret name is configured once in $pullSecret below, per environment
    (staging.tpl and prod.tpl are separate files). The orchestrator creates that secret
    create-if-missing in each namespace.
*/}}
{{ $pullSecret := "registry-pull" }}{{/* <-- CONFIG: pull secret name for staging */}}
{{ range $i, $m := .Manifests }}
{{- $component := "" }}
{{- if $m.metadata }}{{ if $m.metadata.labels }}{{ with index $m.metadata.labels "app.kubernetes.io/component" }}{{ $component = . }}{{ end }}{{ end }}{{ end }}
{{ if and (eq $m.kind "Deployment") (ne $component "datastore") }}
- op: set
  path: {{ $i }}.spec.replicas
  value: 1
- op: set
  path: {{ $i }}.metadata.labels.env
  value: staging
{{/* House standard (Rancher): zero-downtime rolling update */}}
- op: set
  path: {{ $i }}.spec.strategy
  value:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }
{{ range $ci, $c := $m.spec.template.spec.containers }}
{{- if not $c.resources }}
- op: set
  path: {{ $i }}.spec.template.spec.containers.{{ $ci }}.resources
  value:
    requests: { cpu: 50m, memory: 64Mi }
    limits: { memory: 256Mi }
{{- end }}
{{ end }}
{{ end }}
{{/* Private registry -> the platform injects the pull secret into every pod template. */}}
{{ if or (eq $m.kind "Deployment") (eq $m.kind "StatefulSet") }}
- op: set
  path: {{ $i }}.spec.template.spec.imagePullSecrets
  value:
    - name: {{ $pullSecret }}
  description: Pull image from the private registry ({{ $pullSecret }})
{{ end }}
{{ if eq $m.kind "CronJob" }}
- op: set
  path: {{ $i }}.spec.jobTemplate.spec.template.spec.imagePullSecrets
  value:
    - name: {{ $pullSecret }}
  description: Pull image from the private registry ({{ $pullSecret }}) for CronJob
{{ end }}
{{ end }}
