###############################################################################
# Allen BioData Registry PoC — bedrock-kb module.
#
# Provisions a Bedrock Knowledge Base seeded with:
#   1. Registry DDL (migrations/*.sql)
#   2. JSONB field documentation
#   3. Example NL→SQL queries
#   4. Ontology term mappings (NCBI Taxonomy)
#   5. Registry validation data placeholders (Addgene, NCBI GenBank, MGI)
#
# Storage:
#   - S3 bucket as the KB's data source.
#   - OpenSearch Serverless vector collection backing the KB's embeddings.
#
# The Embedding_Backfill_Lambda calls the embedding model used by this KB
# directly (not via the KB API) — see the embedding_backfill_lambda module.
# This KB is consumed by Search_Lambda's POST /search/nl path (Task 28.4).
#
# Validates: R7.3, R18.3, R29.1, R29.2, R29.3, R29.4, R29.5, R29.6 |
# Design: §IaC.Terraform Modules (`bedrock-kb`).
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.5" }
    null   = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

locals {
  kb_name      = "${var.name_prefix}-bedrock-kb"
  bucket_name  = "${var.name_prefix}-bedrock-kb-${data.aws_caller_identity.current.account_id}"
  collection_name = "${var.name_prefix}-bedrock-kb"
  index_name   = "biodata-kb-index"
  embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"

  common_tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "bedrock-kb"
    },
    var.tags,
  )
}

data "aws_caller_identity" "current" {}

###############################################################################
# S3 bucket — KB data source.
###############################################################################

resource "aws_s3_bucket" "kb" {
  bucket        = local.bucket_name
  force_destroy = var.force_destroy

  tags = local.common_tags
}

resource "aws_s3_bucket_versioning" "kb" {
  bucket = aws_s3_bucket.kb.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "kb" {
  bucket = aws_s3_bucket.kb.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "kb" {
  bucket                  = aws_s3_bucket.kb.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

###############################################################################
# Seed the bucket with the curated KB content from var.seed_dir.
# We use a `null_resource` with `aws s3 sync` rather than per-object
# `aws_s3_object` resources so adding a new file in the seed dir doesn't
# require a Terraform code change.
###############################################################################

resource "null_resource" "seed_kb_content" {
  triggers = {
    seed_dir   = var.seed_dir
    bucket     = aws_s3_bucket.kb.id
    seed_hash  = sha1(join("", [
      for f in fileset(var.seed_dir, "**/*") : filesha1("${var.seed_dir}/${f}")
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SEED_DIR = var.seed_dir
      BUCKET   = aws_s3_bucket.kb.id
      AWS_REGION = var.region
    }
    command = <<-EOT
      set -euo pipefail
      aws s3 sync "$SEED_DIR" "s3://$BUCKET/" --delete --region "$AWS_REGION" --no-progress
    EOT
  }

  depends_on = [
    aws_s3_bucket_server_side_encryption_configuration.kb,
    aws_s3_bucket_public_access_block.kb,
  ]
}

###############################################################################
# OpenSearch Serverless — vector collection that backs the KB.
###############################################################################

resource "aws_opensearchserverless_security_policy" "kb_encryption" {
  name = "${var.name_prefix}-kb-enc"
  type = "encryption"
  policy = jsonencode({
    Rules = [{
      Resource     = ["collection/${local.collection_name}"]
      ResourceType = "collection"
    }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "kb_network" {
  name = "${var.name_prefix}-kb-net"
  type = "network"
  policy = jsonencode([{
    Description = "Public access for the bedrock-kb collection — guarded by the data access policy."
    Rules = [
      {
        ResourceType = "collection"
        Resource     = ["collection/${local.collection_name}"]
      },
      {
        ResourceType = "dashboard"
        Resource     = ["collection/${local.collection_name}"]
      },
    ]
    AllowFromPublic = true
  }])
}

resource "aws_opensearchserverless_collection" "kb" {
  name = local.collection_name
  type = "VECTORSEARCH"

  depends_on = [
    aws_opensearchserverless_security_policy.kb_encryption,
    aws_opensearchserverless_security_policy.kb_network,
  ]

  tags = local.common_tags
}

###############################################################################
# IAM — Bedrock KB execution role.
###############################################################################

resource "aws_iam_role" "kb" {
  name = "${var.name_prefix}-bedrock-kb"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "kb_s3_read" {
  name = "${var.name_prefix}-bedrock-kb-s3"
  role = aws_iam_role.kb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.kb.arn]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.kb.arn}/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy" "kb_bedrock_models" {
  name = "${var.name_prefix}-bedrock-kb-models"
  role = aws_iam_role.kb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock:InvokeModel"]
      Resource = [local.embedding_model_arn]
    }]
  })
}

resource "aws_iam_role_policy" "kb_aoss" {
  name = "${var.name_prefix}-bedrock-kb-aoss"
  role = aws_iam_role.kb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["aoss:APIAccessAll"]
      Resource = [aws_opensearchserverless_collection.kb.arn]
    }]
  })
}

###############################################################################
# Data access policy — grant the Bedrock KB role index-create + read/write.
###############################################################################

resource "aws_opensearchserverless_access_policy" "kb_data_access" {
  name = "${var.name_prefix}-kb-access"
  type = "data"
  policy = jsonencode([{
    Description = "Bedrock KB role + operator can manage and query the collection."
    Rules = [
      {
        Resource     = ["index/${local.collection_name}/*"]
        ResourceType = "index"
        Permission = [
          "aoss:CreateIndex",
          "aoss:DeleteIndex",
          "aoss:UpdateIndex",
          "aoss:DescribeIndex",
          "aoss:ReadDocument",
          "aoss:WriteDocument",
        ]
      },
      {
        Resource     = ["collection/${local.collection_name}"]
        ResourceType = "collection"
        Permission = [
          "aoss:CreateCollectionItems",
          "aoss:UpdateCollectionItems",
          "aoss:DescribeCollectionItems",
        ]
      },
    ]
    Principal = compact([
      aws_iam_role.kb.arn,
      var.operator_arn,
    ])
  }])

  depends_on = [
    aws_opensearchserverless_collection.kb,
  ]
}

###############################################################################
# OpenSearch index — Bedrock KB requires the index to exist before the
# create-knowledge-base API succeeds. We create it with a small Python
# script via null_resource, using the `requests-aws4auth` library that's
# already bundled with the indexing Lambda layer.
###############################################################################

resource "null_resource" "kb_index" {
  triggers = {
    collection_id = aws_opensearchserverless_collection.kb.id
    index_name    = local.index_name
    # Bumping this trigger forces re-creation if the access policy was just
    # updated (AOSS policies take ~30s to propagate).
    access_policy_id = aws_opensearchserverless_access_policy.kb_data_access.id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      AWS_REGION   = var.region
      ENDPOINT     = aws_opensearchserverless_collection.kb.collection_endpoint
      INDEX_NAME   = local.index_name
    }
    command = <<-EOT
      set -euo pipefail
      # Sleep to allow AOSS access policy propagation before the index PUT.
      echo "waiting 30s for AOSS access policy propagation..."
      sleep 30
      # `requests-aws4auth` handles SigV4 + session-token correctly for the
      # AOSS data plane. botocore's SigV4Auth omits the session token header
      # which AOSS rejects with 403.
      python3 -m pip install --quiet --user 'requests' 'requests-aws4auth' 2>&1 | tail -3 || true
      python3 - <<'PYEOF'
import json, os, time
import boto3, requests
from requests_aws4auth import AWS4Auth

region   = os.environ["AWS_REGION"]
endpoint = os.environ["ENDPOINT"]
index    = os.environ["INDEX_NAME"]
url      = f"{endpoint}/{index}"

session = boto3.Session()
creds   = session.get_credentials().get_frozen_credentials()
auth    = AWS4Auth(creds.access_key, creds.secret_key, region, "aoss",
                   session_token=creds.token)

body = {
    "settings": {"index.knn": True},
    "mappings": {
        "properties": {
            "AMAZON_BEDROCK_TEXT_CHUNK":       {"type": "text"},
            "AMAZON_BEDROCK_METADATA":         {"type": "text"},
            "bedrock-knowledge-base-default-vector": {
                "type":      "knn_vector",
                "dimension": 1024,
                "method": {
                    "engine":     "faiss",
                    "name":       "hnsw",
                    "space_type": "l2",
                },
            },
        }
    },
}

# Retry up to 60 attempts (2 minutes) for AOSS access policy propagation.
for attempt in range(60):
    try:
        r = requests.put(url, auth=auth, json=body, timeout=15)
        if r.status_code == 200:
            print(f"index create ok: {r.status_code} {r.text[:120]}")
            break
        if "resource_already_exists_exception" in r.text:
            print("index already exists — proceeding")
            break
        print(f"attempt {attempt}: HTTP {r.status_code} {r.text[:200]}")
    except requests.RequestException as exc:
        print(f"attempt {attempt}: {exc}")
    time.sleep(2)
else:
    raise SystemExit("failed to create KB index after retries")
PYEOF
    EOT
  }

  depends_on = [
    aws_opensearchserverless_access_policy.kb_data_access,
    aws_opensearchserverless_collection.kb,
  ]
}

###############################################################################
# Bedrock Knowledge Base.
###############################################################################

resource "aws_bedrockagent_knowledge_base" "this" {
  name     = local.kb_name
  role_arn = aws_iam_role.kb.arn
  description = "Allen BioData Registry KB — registry DDL + ontology + example NL→SQL queries."

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.kb.arn
      vector_index_name = local.index_name
      field_mapping {
        vector_field   = "bedrock-knowledge-base-default-vector"
        text_field     = "AMAZON_BEDROCK_TEXT_CHUNK"
        metadata_field = "AMAZON_BEDROCK_METADATA"
      }
    }
  }

  tags = local.common_tags

  depends_on = [
    null_resource.kb_index,
    aws_iam_role_policy.kb_aoss,
    aws_iam_role_policy.kb_bedrock_models,
    aws_iam_role_policy.kb_s3_read,
  ]
}

resource "aws_bedrockagent_data_source" "s3" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.this.id
  name              = "${local.kb_name}-s3"
  description       = "S3 bucket containing registry DDL, ontology mappings, and NL→SQL examples."

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn = aws_s3_bucket.kb.arn
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"
      fixed_size_chunking_configuration {
        max_tokens         = 512
        overlap_percentage = 20
      }
    }
  }

  depends_on = [
    null_resource.seed_kb_content,
  ]
}
