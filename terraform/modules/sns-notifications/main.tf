###############################################################################
# Allen BioData Registry PoC — sns-notifications module.
#
# Provides per-Org SNS topic infrastructure. Topics are *created at runtime*
# by the Governance_Lambda when a new Organization is created (POST /orgs)
# rather than statically by Terraform — this is required because Orgs are
# data, not infrastructure, and customers will create them through the API.
#
# This module's static surface:
#   1. A naming-convention output (`topic_name_pattern`) so Governance_Lambda
#      and downstream Lambdas (Duplicates, Lifecycle) agree on topic names.
#   2. An IAM policy document (`policy_for_topic_publishers`) attached to
#      Lambdas that publish to per-Org topics (Duplicates_Lambda for new
#      duplicate_flag rows; Lifecycle_Lambda for state transitions).
#   3. A "default" demo topic for the seeded org (so QC3 doesn't depend on
#      the customer creating an org through the API first).
#   4. KMS-grant + IAM permissions for Governance_Lambda to call
#      `sns:CreateTopic`, `sns:Subscribe`, `sns:SetTopicAttributes` for any
#      topic matching the naming convention.
#
# Validates: R3.6 | Design: §IaC.Terraform Modules (`sns-notifications`),
# §Components.User Onboarding Flow.
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

locals {
  topic_prefix       = "${var.name_prefix}-notifications-"
  default_topic_name = "${local.topic_prefix}seed"

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "sns-notifications"
    },
    var.tags,
  )
}

###############################################################################
# Default demo topic — seeded for QC3 demo and as the fallback target if a
# downstream Lambda needs to publish before an Org-specific topic exists.
###############################################################################

resource "aws_sns_topic" "default" {
  name = local.default_topic_name
  # AWS-managed SNS encryption (alias/aws/sns) — same trust model as the
  # SQS queue, sufficient for the PoC. Customer-managed KMS keys are an
  # easy upgrade.
  kms_master_key_id = "alias/aws/sns"

  tags = local.common_tags
}

# An optional default subscription — subscribes the operator email if the
# variable is set. Useful for demo purposes; in production each Org admin's
# verified Cognito email is subscribed by Governance_Lambda at runtime.
resource "aws_sns_topic_subscription" "default_email" {
  count = length(var.default_subscribers)

  topic_arn = aws_sns_topic.default.arn
  protocol  = "email"
  endpoint  = var.default_subscribers[count.index]
}

###############################################################################
# IAM — policy document a publishing Lambda (Duplicates_Lambda,
# Lifecycle_Lambda) can attach to publish to any topic with the convention
# prefix. Wildcards on Resource keep the policy stable as new Orgs are
# created without re-applying Terraform.
###############################################################################

data "aws_caller_identity" "current" {}

locals {
  topic_arn_wildcard = "arn:aws:sns:${var.region}:${data.aws_caller_identity.current.account_id}:${local.topic_prefix}*"
}

# ---------------------------------------------------------------------------
# Publisher policy — Duplicates / Lifecycle / Validation Lambdas attach
# this to publish notifications to *any* per-Org topic.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "publisher" {
  statement {
    sid       = "PublishToAnyOrgTopic"
    effect    = "Allow"
    actions   = ["sns:Publish", "sns:GetTopicAttributes"]
    resources = [local.topic_arn_wildcard]
  }
  # The default seed topic is also covered by the wildcard, but listed
  # explicitly so policy attachments work even before the wildcard
  # convention has any topics created.
  statement {
    sid       = "PublishToDefaultTopic"
    effect    = "Allow"
    actions   = ["sns:Publish", "sns:GetTopicAttributes"]
    resources = [aws_sns_topic.default.arn]
  }
}

resource "aws_iam_policy" "publisher" {
  name        = "${var.name_prefix}-sns-publisher"
  description = "Allows publishing to per-Org notification topics following the ${local.topic_prefix}* naming convention."
  policy      = data.aws_iam_policy_document.publisher.json

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Manager policy — Governance_Lambda attaches this to create topics +
# manage subscriptions for new Orgs.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "manager" {
  statement {
    sid    = "ManageOrgTopics"
    effect = "Allow"
    actions = [
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:Subscribe",
      "sns:Unsubscribe",
      "sns:SetTopicAttributes",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:TagResource",
    ]
    resources = [local.topic_arn_wildcard]
  }
  # SNS:CreateTopic + SNS:Subscribe also need a wildcard on subscription
  # ARNs (which are derived from the topic ARN at runtime). The ARN
  # pattern below covers `arn:aws:sns:.../topic_prefix*:subscription-id`.
  statement {
    sid     = "ManageSubscriptionsAcrossPrefix"
    effect  = "Allow"
    actions = ["sns:Subscribe", "sns:Unsubscribe", "sns:SetSubscriptionAttributes"]
    resources = [
      "arn:aws:sns:${var.region}:${data.aws_caller_identity.current.account_id}:${local.topic_prefix}*:*",
    ]
  }
  # Required to use the AWS-managed SNS KMS key when creating topics.
  statement {
    sid    = "UseSnsKmsKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:DescribeKey",
    ]
    resources = ["*"]
    condition {
      test     = "ForAnyValue:StringEquals"
      variable = "kms:ResourceAliases"
      values   = ["alias/aws/sns"]
    }
  }
}

resource "aws_iam_policy" "manager" {
  name        = "${var.name_prefix}-sns-manager"
  description = "Allows Governance_Lambda to manage per-Org notification topics."
  policy      = data.aws_iam_policy_document.manager.json

  tags = local.common_tags
}
