####
## Init file to create buckets for Agentic Data Quality Triage
## Author: Mario Caesar // caesarmario87@gmail.com
####

set -eu

echo "Waiting for S3 endpoint..."
until aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3api list-buckets >/dev/null 2>&1; do
  sleep 2
done

echo "Creating buckets (idempotent)..."
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$LANDING_BUCKET"   || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$ARTIFACTS_BUCKET" || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$DQREPORTS_BUCKET" || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$DQFAILURES_BUCKET" || true
aws --endpoint-url "$S3_ENDPOINT_URL_INTERNAL" s3 mb "s3://$AUDIT_BUCKET"     || true

echo "Done."