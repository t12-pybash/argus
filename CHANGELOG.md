## v0.2.0 (2026-06-27)

### Feat

- **diagnose**: RAG over Obsidian vault — search Qdrant for runbooks before LLM call
- **diagnose**: use deepseek-r1:7b for root cause analysis, strip <think> blocks from output
- **argus**: add 'argus model' command family (status, rollout, rollback)
- **status**: add KServe InferenceService health to argus status
- expand agent remediation action space
- rename package to argus, add agent command and CI/CD pipeline
- direct Qdrant upsert on save — bypass vault sync pipeline
- add sre diagnose command and enrich ask with pod exit reasons
- add problem pods, deployment readiness, disk usage, and Flux sync context to sre ask
- inject running pod context into sre ask
- replace rsync CronJob with git-based vault publisher
- add CPU and memory context to sre ask with node name mapping
- add Dockerfile and SRE_CONFIG_FILE support
- add incremental re-indexing to ingest_vault
- add sre incident command with vault note creation and cluster snapshot
- add sre runbook command with semantic vault search and optional LLM synthesis
- initial sre CLI with status, alert, ask commands

### Fix

- **diagnose**: exit early when pod has no evidence of problems
- remove unused subprocess import from agent
- update Dockerfile and CI for argus rename
- rename all sre references to argus
- strip bold markers from Slack remediation plan
- rename sre agent strings to argus agent
- escape jsonpath braces in diagnose f-string prompt
- improve diagnose prompt accuracy and filter low-score chunks from runbook --ask
- add safe.directory config for PVC ownership mismatch
- use deterministic MD5 point IDs in both vault ingest scripts
- replace qdrant-client with httpx REST calls for search_vault
- qdrant port 443, verify=False, batch upsert for vault ingestion
- **ask**: add optional api key auth, fix default model name to mistral
- **ask**: use LiteLLM OpenAI-compatible API instead of internal Ollama URL

### Perf

- parallelise status/diagnose/incident and add HTTP connection pooling
- batch pod kubectl calls and parallelise flux sync checks
- parallelise context gathering in sre ask
