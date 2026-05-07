####
## Init file to create buckets for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

# --- Enabling Strict Shell Mode
set -eu

# --- Waiting For S3 Gateway
echo "Waiting for S3 endpoint..."

until aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3api list-buckets >/dev/null 2>&1; do
  sleep 2
done

# --- Creating Buckets
echo "Creating buckets (idempotent)..."

# SeaweedFS S3 returns a non-zero exit code when the bucket already exists.
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$LANDING_BUCKET"    || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$ARTIFACTS_BUCKET"  || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$DQREPORTS_BUCKET"  || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$DQFAILURES_BUCKET" || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$AUDIT_BUCKET"      || true

# --- Finishing Bucket Initialization
echo "Done."
