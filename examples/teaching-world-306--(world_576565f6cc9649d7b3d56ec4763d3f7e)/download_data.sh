#!/usr/bin/env bash
# Download world data files from S3.
#
# World: teaching-world-306--(world_576565f6cc9649d7b3d56ec4763d3f7e)
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - Access to the delivery S3 bucket

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo 'Downloading world files...'
aws s3 sync "s3://rl-studio-report-artifacts-prod/canonical-delivery/ws_53fa1e9838494bbf8962e88c4df2fac9/2026-Apr-10th_1-51am/code/docker/worlds/teaching-world-306--(world_576565f6cc9649d7b3d56ec4763d3f7e)/files/" "$SCRIPT_DIR/files/" --quiet

echo 'Downloading golden response files...'
aws s3 sync "s3://rl-studio-report-artifacts-prod/canonical-delivery/ws_53fa1e9838494bbf8962e88c4df2fac9/2026-Apr-10th_1-51am/code/docker/worlds/teaching-world-306--(world_576565f6cc9649d7b3d56ec4763d3f7e)/golden_response_files/" "$SCRIPT_DIR/golden_response_files/" --quiet

echo 'Done.'
