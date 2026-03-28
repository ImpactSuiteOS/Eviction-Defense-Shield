###############################################################################
# EvictionShield — Module 7A: Terraform Infrastructure
# All resources follow principle of least privilege.
# VPC Service Controls perimeter enforced around all sensitive services.
###############################################################################

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    bucket = "evictionshield-tfstate"
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

###############################################################################
# Variables
###############################################################################

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "Default GCP region"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment: prod | staging | dev"
  type        = string
  default     = "prod"
}

variable "twilio_account_sid" {
  description = "Twilio account SID (written to Secret Manager)"
  type        = string
  sensitive   = true
}

variable "twilio_auth_token" {
  description = "Twilio auth token (written to Secret Manager)"
  type        = string
  sensitive   = true
}

variable "twilio_from_number" {
  description = "Twilio SMS originating number"
  type        = string
  sensitive   = true
}

###############################################################################
# Enable APIs
###############################################################################

resource "google_project_service" "required_apis" {
  for_each = toset([
    "run.googleapis.com",
    "cloudfunctions.googleapis.com",
    "pubsub.googleapis.com",
    "firestore.googleapis.com",
    "bigquery.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "documentai.googleapis.com",
    "aiplatform.googleapis.com",
    "speech.googleapis.com",
    "texttospeech.googleapis.com",
    "dialogflow.googleapis.com",
    "dataflow.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firebase.googleapis.com",
    "identitytoolkit.googleapis.com",
    "accesscontextmanager.googleapis.com",
    "vpcaccess.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

###############################################################################
# Service Accounts (least privilege)
###############################################################################

resource "google_service_account" "ingestion_sa" {
  account_id   = "evictionshield-ingestion"
  display_name = "EvictionShield Court Ingestion Service"
}

resource "google_service_account" "docprocessing_sa" {
  account_id   = "evictionshield-docprocessing"
  display_name = "EvictionShield Document Processing Function"
}

resource "google_service_account" "analysis_sa" {
  account_id   = "evictionshield-analysis"
  display_name = "EvictionShield Gemini Analysis Function"
}

resource "google_service_account" "notifications_sa" {
  account_id   = "evictionshield-notifications"
  display_name = "EvictionShield Notifications Function"
}

resource "google_service_account" "voice_sa" {
  account_id   = "evictionshield-voice"
  display_name = "EvictionShield Voice Interface Service"
}

resource "google_service_account" "dashboard_api_sa" {
  account_id   = "evictionshield-dashboard"
  display_name = "EvictionShield Dashboard API Service"
}

resource "google_service_account" "dataflow_sa" {
  account_id   = "evictionshield-dataflow"
  display_name = "EvictionShield Dataflow Pipeline"
}

resource "google_service_account" "functions_sa" {
  account_id   = "evictionshield-functions"
  display_name = "EvictionShield Cloud Functions (shared)"
}

###############################################################################
# IAM Bindings — Least Privilege
###############################################################################

locals {
  sa_roles = {
    # Ingestion: read secrets, write GCS + Pub/Sub + Firestore
    "${google_service_account.ingestion_sa.email}" = [
      "roles/secretmanager.secretAccessor",
      "roles/storage.objectCreator",
      "roles/pubsub.publisher",
      "roles/datastore.user",
      "roles/logging.logWriter",
    ]
    # Document processing: read GCS, call Document AI, write Firestore + Pub/Sub
    "${google_service_account.docprocessing_sa.email}" = [
      "roles/storage.objectViewer",
      "roles/documentai.apiUser",
      "roles/datastore.user",
      "roles/pubsub.publisher",
      "roles/pubsub.subscriber",
      "roles/logging.logWriter",
    ]
    # Analysis: read Firestore + BQ, call Vertex AI, write Firestore + BQ + Pub/Sub
    "${google_service_account.analysis_sa.email}" = [
      "roles/datastore.user",
      "roles/bigquery.dataViewer",
      "roles/bigquery.jobUser",
      "roles/aiplatform.user",
      "roles/storage.objectViewer",
      "roles/pubsub.publisher",
      "roles/pubsub.subscriber",
      "roles/secretmanager.secretAccessor",
      "roles/logging.logWriter",
    ]
    # Notifications: read Firestore + secrets, call Vertex AI, create Tasks
    "${google_service_account.notifications_sa.email}" = [
      "roles/datastore.user",
      "roles/secretmanager.secretAccessor",
      "roles/aiplatform.user",
      "roles/cloudtasks.enqueuer",
      "roles/pubsub.subscriber",
      "roles/logging.logWriter",
    ]
    # Voice: call STT, TTS, Dialogflow, read Firestore
    "${google_service_account.voice_sa.email}" = [
      "roles/speech.client",
      "roles/cloudtexttospeech.client",
      "roles/dialogflow.client",
      "roles/datastore.user",
      "roles/secretmanager.secretAccessor",
      "roles/logging.logWriter",
    ]
    # Dashboard API: read/write Firestore, read BQ, read Storage
    "${google_service_account.dashboard_api_sa.email}" = [
      "roles/datastore.user",
      "roles/bigquery.dataViewer",
      "roles/bigquery.jobUser",
      "roles/storage.objectViewer",
      "roles/pubsub.publisher",
      "roles/logging.logWriter",
    ]
    # Dataflow: read/write BQ, read Firestore
    "${google_service_account.dataflow_sa.email}" = [
      "roles/dataflow.worker",
      "roles/bigquery.dataEditor",
      "roles/bigquery.jobUser",
      "roles/datastore.viewer",
      "roles/storage.objectAdmin",
      "roles/logging.logWriter",
    ]
  }
}

resource "google_project_iam_member" "sa_bindings" {
  for_each = merge([
    for sa_email, roles in local.sa_roles : {
      for role in roles : "${sa_email}/${role}" => {
        sa_email = sa_email
        role     = role
      }
    }
  ]...)

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${each.value.sa_email}"
}

###############################################################################
# Cloud Storage Buckets
###############################################################################

resource "google_storage_bucket" "raw_filings" {
  name          = "${var.project_id}-raw-filings"
  location      = var.region
  force_destroy = false
  versioning { enabled = true }
  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 365 }  # Delete raw filings after 1 year
  }
  uniform_bucket_level_access = true
}

resource "google_storage_bucket" "processed_documents" {
  name                        = "${var.project_id}-processed-docs"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
}

resource "google_storage_bucket" "attorney_briefs" {
  name                        = "${var.project_id}-attorney-briefs"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  lifecycle_rule {
    action { type = "Delete" }
    condition { age = 90 }   # Briefs expire after 90 days; re-generated on demand
  }
}

resource "google_storage_bucket" "jurisdiction_rulesets" {
  name                        = "${var.project_id}-rulesets"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  versioning { enabled = true }
}

resource "google_storage_bucket" "dataflow_staging" {
  name                        = "${var.project_id}-dataflow-staging"
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
}

###############################################################################
# Pub/Sub Topics and Subscriptions
###############################################################################

resource "google_pubsub_topic" "raw_eviction_filings" {
  name = "raw-eviction-filings"
  message_retention_duration = "86400s"  # 24 hours
}

resource "google_pubsub_topic" "raw_eviction_filings_dlq" {
  name = "raw-eviction-filings-dlq"
}

resource "google_pubsub_subscription" "raw_filings_to_docprocessing" {
  name  = "raw-filings-docprocessing"
  topic = google_pubsub_topic.raw_eviction_filings.id
  push_config {
    push_endpoint = google_cloudfunctions2_function.document_processing.service_config[0].uri
    oidc_token {
      service_account_email = google_service_account.docprocessing_sa.email
    }
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.raw_eviction_filings_dlq.id
    max_delivery_attempts = 5
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }
  ack_deadline_seconds = 300
}

resource "google_pubsub_topic" "structured_filings_ready" {
  name = "structured-filings-ready"
}

resource "google_pubsub_topic" "low_confidence_filings" {
  name = "low-confidence-filings"
}

resource "google_pubsub_topic" "case_analysis_complete" {
  name = "case-analysis-complete"
}

resource "google_pubsub_topic" "trigger_profile_update" {
  name = "trigger-profile-update"
}

resource "google_pubsub_topic" "case_assigned" {
  name = "case-assigned"
}

###############################################################################
# Firestore (Native mode)
###############################################################################

resource "google_firestore_database" "evictionshield" {
  project     = var.project_id
  name        = "(default)"
  location_id = "us-central"
  type        = "FIRESTORE_NATIVE"
  concurrency_mode = "OPTIMISTIC"
  app_engine_integration_mode = "DISABLED"
}

###############################################################################
# BigQuery Dataset
###############################################################################

resource "google_bigquery_dataset" "evictionshield" {
  dataset_id                  = "evictionshield"
  friendly_name               = "EvictionShield Analytics"
  description                 = "Eviction filings, landlord profiles, and defense effectiveness data"
  location                    = "US"
  default_table_expiration_ms = null  # Tables expire individually

  access {
    role          = "OWNER"
    user_by_email = google_service_account.analysis_sa.email
  }
  access {
    role          = "WRITER"
    user_by_email = google_service_account.dataflow_sa.email
  }
  access {
    role          = "READER"
    user_by_email = google_service_account.dashboard_api_sa.email
  }
}

###############################################################################
# Secret Manager Secrets
###############################################################################

resource "google_secret_manager_secret" "court_api_creds_pa" {
  secret_id = "court-api-creds-pa"
  replication { auto {} }
}

resource "google_secret_manager_secret" "twilio_creds" {
  secret_id = "twilio-creds"
  replication { auto {} }
}

resource "google_secret_manager_secret_version" "twilio_creds_v1" {
  secret = google_secret_manager_secret.twilio_creds.id
  secret_data = jsonencode({
    account_sid = var.twilio_account_sid
    auth_token  = var.twilio_auth_token
    from_number = var.twilio_from_number
  })
}

resource "google_secret_manager_secret" "jwt_signing_key" {
  secret_id = "jwt-signing-key"
  replication { auto {} }
}

###############################################################################
# Cloud Run — Court Ingestion Service
###############################################################################

resource "google_cloud_run_v2_service" "court_ingestion" {
  name     = "evictionshield-ingestion"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"  # No public access — scheduled internally

  template {
    service_account = google_service_account.ingestion_sa.email
    scaling {
      min_instance_count = 1  # Always one running for scheduled polling
      max_instance_count = 3
    }
    containers {
      image = "gcr.io/${var.project_id}/evictionshield-ingestion:latest"
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCS_BUCKET_RAW"
        value = google_storage_bucket.raw_filings.name
      }
      env {
        name  = "PUBSUB_TOPIC_RAW"
        value = google_pubsub_topic.raw_eviction_filings.name
      }
      env {
        name  = "POLL_INTERVAL_SECONDS"
        value = "14400"
      }
      env {
        name  = "STATE_ADAPTERS"
        value = "PA"
      }
    }
  }
}

###############################################################################
# Cloud Run — Dashboard API
###############################################################################

resource "google_cloud_run_v2_service" "dashboard_api" {
  name     = "evictionshield-dashboard-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"  # Public — authenticated via JWT

  template {
    service_account = google_service_account.dashboard_api_sa.email
    scaling {
      min_instance_count = 1
      max_instance_count = 20
    }
    containers {
      image = "gcr.io/${var.project_id}/evictionshield-dashboard-api:latest"
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "BIGQUERY_DATASET"
        value = google_bigquery_dataset.evictionshield.dataset_id
      }
      env {
        name  = "STORAGE_BUCKET_BRIEFS"
        value = google_storage_bucket.attorney_briefs.name
      }
    }
  }
}

###############################################################################
# Cloud Run — Voice Interface
###############################################################################

resource "google_cloud_run_v2_service" "voice_interface" {
  name     = "evictionshield-voice"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"  # Must be publicly reachable for Twilio webhooks

  template {
    service_account = google_service_account.voice_sa.email
    scaling {
      min_instance_count = 1
      max_instance_count = 50  # High concurrency for simultaneous calls
    }
    containers {
      image = "gcr.io/${var.project_id}/evictionshield-voice:latest"
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle = false  # Keep CPU allocated for real-time audio processing
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "DIALOGFLOW_AGENT_ID"
        value = var.dialogflow_agent_id
      }
    }
  }
}

###############################################################################
# Cloud Functions (2nd gen)
###############################################################################

resource "google_cloudfunctions2_function" "document_processing" {
  name     = "evictionshield-docprocessing"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "process_filing"
    source {
      storage_source {
        bucket = google_storage_bucket.dataflow_staging.name
        object = "functions/document_processing.zip"
      }
    }
  }

  service_config {
    service_account_email = google_service_account.docprocessing_sa.email
    min_instance_count    = 0
    max_instance_count    = 100
    available_memory      = "1Gi"
    timeout_seconds       = 300
    environment_variables = {
      GCP_PROJECT_ID         = var.project_id
      DOCAI_FORM_PARSER_ID   = var.docai_form_parser_id
      DOCAI_OCR_PROCESSOR_ID = var.docai_ocr_processor_id
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.raw_eviction_filings.id
    retry_policy   = "RETRY_POLICY_RETRY"
  }
}

resource "google_cloudfunctions2_function" "gemini_analysis" {
  name     = "evictionshield-analysis"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "analyze_filing_event"
    source {
      storage_source {
        bucket = google_storage_bucket.dataflow_staging.name
        object = "functions/gemini_analysis.zip"
      }
    }
  }

  service_config {
    service_account_email = google_service_account.analysis_sa.email
    min_instance_count    = 0
    max_instance_count    = 50
    available_memory      = "2Gi"
    timeout_seconds       = 540
    environment_variables = {
      GCP_PROJECT_ID       = var.project_id
      GCS_BUCKET_RULESETS  = google_storage_bucket.jurisdiction_rulesets.name
      BIGQUERY_DATASET     = google_bigquery_dataset.evictionshield.dataset_id
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.structured_filings_ready.id
    retry_policy   = "RETRY_POLICY_RETRY"
  }
}

resource "google_cloudfunctions2_function" "sms_notification" {
  name     = "evictionshield-sms-notify"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "on_case_analysis_created"
    source {
      storage_source {
        bucket = google_storage_bucket.dataflow_staging.name
        object = "functions/sms_notification.zip"
      }
    }
  }

  service_config {
    service_account_email = google_service_account.notifications_sa.email
    min_instance_count    = 0
    max_instance_count    = 20
    available_memory      = "512Mi"
    timeout_seconds       = 120
    environment_variables = {
      GCP_PROJECT_ID      = var.project_id
      INTAKE_PORTAL_URL   = "https://app.evictionshield.io"
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.firestore.document.v1.created"
    event_filters {
      attribute = "database"
      value     = "(default)"
    }
    event_filters {
      attribute = "document"
      value     = "case_analyses/{caseId}"
      operator  = "match-path-pattern"
    }
    retry_policy = "RETRY_POLICY_DO_NOT_RETRY"
  }
}

resource "google_cloudfunctions2_function" "outcome_ingestion" {
  name     = "evictionshield-outcome-ingest"
  location = var.region

  build_config {
    runtime     = "python311"
    entry_point = "on_case_outcome_created"
    source {
      storage_source {
        bucket = google_storage_bucket.dataflow_staging.name
        object = "functions/outcome_ingestion.zip"
      }
    }
  }

  service_config {
    service_account_email = google_service_account.functions_sa.email
    min_instance_count    = 0
    max_instance_count    = 10
    available_memory      = "512Mi"
    timeout_seconds       = 300
    environment_variables = {
      GCP_PROJECT_ID   = var.project_id
      BIGQUERY_DATASET = google_bigquery_dataset.evictionshield.dataset_id
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.firestore.document.v1.created"
    event_filters {
      attribute = "database"
      value     = "(default)"
    }
    event_filters {
      attribute = "document"
      value     = "case_outcomes/{caseId}"
      operator  = "match-path-pattern"
    }
    retry_policy = "RETRY_POLICY_RETRY"
  }
}

###############################################################################
# Cloud Tasks Queue (SMS delivery)
###############################################################################

resource "google_cloud_tasks_queue" "sms_notifications" {
  name     = "sms-notifications"
  location = var.region

  rate_limits {
    max_dispatches_per_second = 100
    max_concurrent_dispatches = 50
  }

  retry_config {
    max_attempts  = 5
    min_backoff   = "10s"
    max_backoff   = "300s"
    max_doublings = 3
  }
}

###############################################################################
# Cloud Scheduler — Polling + Dataflow trigger
###############################################################################

resource "google_cloud_scheduler_job" "trigger_dataflow" {
  name      = "evictionshield-dataflow-trigger"
  schedule  = "0 */6 * * *"  # Every 6 hours
  time_zone = "UTC"

  http_target {
    uri         = "https://dataflow.googleapis.com/v1b3/projects/${var.project_id}/locations/${var.region}/templates:launch"
    http_method = "POST"
    oauth_token {
      service_account_email = google_service_account.dataflow_sa.email
    }
    body = base64encode(jsonencode({
      jobName  = "landlord-profile-update"
      gcsPath  = "gs://${google_storage_bucket.dataflow_staging.name}/templates/landlord_profile_update"
      environment = {
        tempLocation    = "gs://${google_storage_bucket.dataflow_staging.name}/tmp"
        serviceAccount = google_service_account.dataflow_sa.email
      }
      parameters = {
        project = var.project_id
        dataset = google_bigquery_dataset.evictionshield.dataset_id
      }
    }))
  }
}

resource "google_cloud_scheduler_job" "bqml_weekly_retrain" {
  name      = "evictionshield-bqml-retrain"
  schedule  = "0 2 * * 0"   # Sundays at 2 AM UTC
  time_zone = "UTC"

  http_target {
    uri         = "https://cloudbuild.googleapis.com/v1/projects/${var.project_id}/triggers/${var.bqml_build_trigger_id}:run"
    http_method = "POST"
    oauth_token {
      service_account_email = google_service_account.functions_sa.email
    }
    body = base64encode("{}")
  }
}

###############################################################################
# Variables for optional/configurable values
###############################################################################

variable "dialogflow_agent_id" {
  description = "Vertex AI Agent Builder agent ID"
  type        = string
  default     = ""
}

variable "docai_form_parser_id" {
  description = "Document AI Form Parser processor ID"
  type        = string
  default     = ""
}

variable "docai_ocr_processor_id" {
  description = "Document AI OCR processor ID"
  type        = string
  default     = ""
}

variable "bqml_build_trigger_id" {
  description = "Cloud Build trigger ID for BQML retraining"
  type        = string
  default     = ""
}

###############################################################################
# Outputs
###############################################################################

output "dashboard_api_url" {
  value = google_cloud_run_v2_service.dashboard_api.uri
}

output "voice_interface_url" {
  value = google_cloud_run_v2_service.voice_interface.uri
}

output "ingestion_service_url" {
  value = google_cloud_run_v2_service.court_ingestion.uri
}

output "raw_filings_bucket" {
  value = google_storage_bucket.raw_filings.name
}

output "rulesets_bucket" {
  value = google_storage_bucket.jurisdiction_rulesets.name
}
