{{/*
  PATCH TEMPLATE — PROD
  More replicas, larger resources. Only the platform team edits this file.
  Same rules as staging.tpl: skip datastores, only set resources when the container
  doesn't already declare them, pull secret configured via $pullSecret (CronJobs too).
*/}}
{{ $pullSecret := "%%registry.pull_secret%%" }}{{/* <-- CONFIG: pull secret name for prod */}}
{{ range $i, $m := .Manifests }}
{{- $component := "" }}
{{- if $m.metadata }}{{ if $m.metadata.labels }}{{ with index $m.metadata.labels "app.kubernetes.io/component" }}{{ $component = . }}{{ end }}{{ end }}{{ end }}
{{ if and (eq $m.kind "Deployment") (ne $component "datastore") }}
- op: set
  path: {{ $i }}.spec.replicas
  value: %%env.replicas%%
- op: set
  path: {{ $i }}.metadata.labels.env
  value: prod
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
    requests: { cpu: %%env.cpu_request%%, memory: %%env.memory_request%% }
    limits: { memory: %%env.memory_limit%% }
{{- end }}
{{ end }}
{{ end }}
{{/* Pull secret — same as staging, name configured separately for prod in $pullSecret */}}
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
