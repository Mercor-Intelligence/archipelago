#!/usr/bin/env bash
# Download world data files from S3.
#
# World: accounting-world-801--(world_fa8052e0c4a54e50a09072fef5c756d0)
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - Access to the delivery S3 bucket

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo 'Downloading world files...'
aws s3 sync "s3://rl-studio-report-artifacts-prod/canonical-delivery/ws_53fa1e9838494bbf8962e88c4df2fac9/2026-Apr-10th_1-51am/code/docker/worlds/accounting-world-801--(world_fa8052e0c4a54e50a09072fef5c756d0)/files/" "$SCRIPT_DIR/files/" --quiet

echo 'Downloading golden response files...'
aws s3 sync "s3://rl-studio-report-artifacts-prod/canonical-delivery/ws_53fa1e9838494bbf8962e88c4df2fac9/2026-Apr-10th_1-51am/code/docker/worlds/accounting-world-801--(world_fa8052e0c4a54e50a09072fef5c756d0)/golden_response_files/" "$SCRIPT_DIR/golden_response_files/" --quiet

echo 'Downloading MCP server state files...'
aws s3 sync "s3://rl-studio-report-artifacts-prod/canonical-delivery/ws_53fa1e9838494bbf8962e88c4df2fac9/2026-Apr-10th_1-51am/code/docker/worlds/accounting-world-801--(world_fa8052e0c4a54e50a09072fef5c756d0)/tools/.apps_data/" "$SCRIPT_DIR/tools/.apps_data/" --quiet

echo 'Done.'
