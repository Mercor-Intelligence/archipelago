# Archipelago batch eval — local Mac + GCP VM workers
#
# One-liner quickstart:
#   make provision EVAL_PROJECT=seed-pro-pass3  # create bucket/project prefixes
#   make publish   EVAL_PROJECT=seed-pro-pass3  # sample tasks, prepare GCS queue state
#   make status    EVAL_PROJECT=seed-pro-pass3  # poll progress
#   make aggregate EVAL_PROJECT=seed-pro-pass3  # compute pass@1/pass@k/mean
#   make teardown  EVAL_PROJECT=seed-pro-pass3  # destroy VM + bucket
#
# Tunables:
#   N_TASKS=5 K=3 MODEL=doubao-seed-2-0-pro-260215
#   PROJECT=sotalab-prod ZONE=asia-east1-b
#
# Defaults below are the values used for the 2026-06-16 seed-pro-pass3 run.

PROJECT      ?= sotalab-prod
ZONE         ?= asia-east1-b
EVAL_PROJECT ?= seed-pro-pass3
N_TASKS      ?= 5
K            ?= 3
MODEL        ?= doubao-seed-2-0-pro-260215
WORKER_COUNT ?= 5
WORKER_NAME  ?= archipelago-eval-worker
WORKER_PREFIX ?= archipelago-eval-auto
TARGET_RUNNING ?= 16
MAX_RUNNING ?= 16
ZONES ?= us-central1-a,us-west1-a,asia-east1-b
MACHINE_TYPE ?= e2-standard-4
BOOT_DISK_SIZE ?= 150GB
QUEUE_NAME ?= pass5
ATTEMPT_START ?= 1
ATTEMPT_END ?= 5
TEMPERATURE ?= 0.7
MAX_STEPS ?= 200
TASK_IDS_GCS_URI ?= gs://$(BUCKET)/$(PROJECT_DIR)/state/task_ids.json
BUCKET       ?= sotalab-archipelago-eval
PROJECT_DIR  = eval-projects/$(EVAL_PROJECT)

# Artifact Registry config for the worker environment image
IMAGE_REPO   ?= asia-east1-docker.pkg.dev/$(PROJECT)/docker-repo
ENV_IMAGE    ?= $(IMAGE_REPO)/sotalab-apex-archipelago-environment-prod
ENV_IMAGE_TAG ?= latest

SCHEDULER = uv run python scripts/local_scheduler.py
AGGREGATOR = uv run python scripts/aggregate_results.py

.PHONY: help provision vm dynamic-workers worker-health bucket publish status aggregate teardown probe clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

## --- one-time bootstrap --------------------------------------------------

provision: bucket ## Provision GCS bucket + project prefixes (run once per project)

bucket: ## Create GCS bucket + project subdirectory
	gcloud storage buckets create gs://$(BUCKET) \
	  --project=$(PROJECT) --location=asia-east1 \
	  2>&1 | tail -1 || true
	gsutil -m cp /dev/null gs://$(BUCKET)/$(PROJECT_DIR)/state/.keep
	gsutil -m cp /dev/null gs://$(BUCKET)/$(PROJECT_DIR)/results/.keep
	gsutil -m cp /dev/null gs://$(BUCKET)/$(PROJECT_DIR)/logs/.keep

vm: ## Create N GCE worker VMs for manual/static runs (default 5)
	@for i in $$(seq 1 $(WORKER_COUNT)); do \
	  name=$(WORKER_NAME)-$$i; \
	  echo "[vm] creating $$name"; \
	  gcloud compute instances create $$name \
	    --project=$(PROJECT) --zone=$(ZONE) \
	    --machine-type=e2-standard-4 --boot-disk-size=100GB \
	    --image-family=debian-12 --image-project=debian-cloud \
	    --scopes=https://www.googleapis.com/auth/cloud-platform \
	    --metadata-from-file=startup-script=scripts/setup_worker.sh \
	    --metadata=ENV_IMAGE=$(ENV_IMAGE),ENV_IMAGE_TAG=$(ENV_IMAGE_TAG),EVAL_PROJECT_DIR=$(PROJECT_DIR),EVAL_BUCKET=$(BUCKET) 2>&1 | tail -2; \
	done

dynamic-workers: ## Scale dynamic GCS-queue workers up to TARGET_RUNNING, capped by MAX_RUNNING
	python3 scripts/launch_dynamic_workers.py \
	  --project $(PROJECT) \
	  --project-dir $(PROJECT_DIR) \
	  --bucket $(BUCKET) \
	  --worker-prefix $(WORKER_PREFIX) \
	  --target-running $(TARGET_RUNNING) \
	  --max-running $(MAX_RUNNING) \
	  --zones $(ZONES) \
	  --machine-type $(MACHINE_TYPE) \
	  --boot-disk-size $(BOOT_DISK_SIZE) \
	  --queue-name $(QUEUE_NAME) \
	  --attempt-start $(ATTEMPT_START) \
	  --attempt-end $(ATTEMPT_END) \
	  --model $(MODEL) \
	  --temperature $(TEMPERATURE) \
	  --max-steps $(MAX_STEPS) \
	  --task-ids-gcs-uri $(TASK_IDS_GCS_URI) \
	  --env-image $(ENV_IMAGE) \
	  --env-image-tag $(ENV_IMAGE_TAG)

worker-health: ## Check dynamic worker systemd/queue health by prefix
	python3 scripts/worker_healthcheck.py \
	  --project $(PROJECT) \
	  --worker-prefix $(WORKER_PREFIX)

## --- run / monitor -------------------------------------------------------

publish: ## Prepare N*K jobs in GCS state (samples tasks via HF)
	$(SCHEDULER) publish \
	  --project-dir $(PROJECT_DIR) \
	  --bucket $(BUCKET) \
	  --gcp-project $(PROJECT) \
	  --n-tasks $(N_TASKS) --k $(K) --model $(MODEL)

status: ## Print progress of published jobs from GCS state
	$(SCHEDULER) status \
	  --project-dir $(PROJECT_DIR) \
	  --bucket $(BUCKET) \
	  --gcp-project $(PROJECT)

aggregate: ## Download all grades.json, compute pass@1/pass@k/mean score
	$(AGGREGATOR) \
	  --project-dir $(PROJECT_DIR) \
	  --bucket $(BUCKET) \
	  --project $(PROJECT) --k $(K)

## --- teardown / utilities ------------------------------------------------

teardown: ## Destroy all GCP resources for this eval project
	./scripts/teardown.sh $(PROJECT) $(ZONE) $(BUCKET) $(WORKER_COUNT)

probe: ## Probe New API staging models availability
	./scripts/probe_new_api.sh

build-image: ## Build apex-test-environment image (needs GITHUB_TOKEN env var)
	docker build \
	  --secret id=github_token,env=GITHUB_TOKEN \
	  -t apex-test-environment:latest \
	  -t $(ENV_IMAGE):$(ENV_IMAGE_TAG) \
	  -f environment/Dockerfile .

push-image: build-image ## Push apex-test-environment image to Artifact Registry
	docker login -u oauth2accesstoken -p "$(shell gcloud auth print-access-token)" https://$(IMAGE_REPO)
	docker push $(ENV_IMAGE):$(ENV_IMAGE_TAG)

clean: ## Remove local state files for this project
	rm -rf ~/.archipelago-eval/$(EVAL_PROJECT)
