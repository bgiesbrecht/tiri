# Databricks Apps — deployment guide for Tiri
#
# Prerequisites (one-time per workspace)
# ─────────────────────────────────────────
#
# 1. Provision Unity Catalog resources
#
#    The store and vector index live in main.tiri. Create the schema:
#
#      CREATE SCHEMA IF NOT EXISTS main.tiri;
#
#    The Delta KV store table is created automatically by DatabricksStoreProvider
#    on first write. No manual table creation needed.
#
#    The Vector Search index must be created manually before load-room:
#
#      a. Create a Vector Search endpoint in the Databricks UI
#         (Compute > Vector Search > Create endpoint)
#         Name it anything — you'll reference it as DB_VECTOR_ENDPOINT.
#
#      b. The index (main.tiri.example_index) is created automatically
#         by DatabricksVectorProvider on first upsert (i.e. when you run
#         load-room for the first time). The endpoint must exist first.
#
# 2. Create the Apps secret scope
#
#      databricks secrets create-scope tiri
#      databricks secrets put-secret tiri warehouse_id    --string-value <warehouse-id>
#      databricks secrets put-secret tiri vector_endpoint --string-value <endpoint-name>
#
# 3. Add a production extras group to pyproject.toml (if not already present):
#
#      [project.optional-dependencies]
#      production = [
#          "httpx>=0.27",
#          "databricks-sdk>=0.30",
#          "fastapi>=0.115",
#          "uvicorn[standard]>=0.30",
#          "pyyaml>=6.0",
#      ]
#
# Deploy
# ──────
#
# 1. Copy the sample config to tiri.toml (gitignored):
#
#      cp deploy/tiri.toml.production tiri.toml
#
# 2. Create the App (one-time):
#
#      databricks apps create tiri --description "Tiri data reasoning system"
#
# 3. Deploy:
#
#      databricks apps deploy tiri --source-code-path .
#
# 4. Load rooms (run once after first deploy, or whenever room configs change):
#
#      export DATABRICKS_HOST=<your-workspace>.azuredatabricks.net
#      export DATABRICKS_TOKEN=<your-token>
#      export DB_WAREHOUSE_ID=<warehouse-id>
#      export DB_VECTOR_ENDPOINT=<endpoint-name>
#
#      python -m tiri.cli load-room demo/tpch_sales_config.json
#      python -m tiri.cli load-room demo/tpch_supply_config.json
#
# Authentication in the App
# ─────────────────────────
#
# Databricks Apps automatically injects X-Forwarded-Access-Token with the
# logged-in user's OAuth token. Tiri's auth.py reads this header as a
# fallback when no Authorization: Bearer header is present. Per-user Unity
# Catalog enforcement (EXT-6) applies automatically — no client-side changes
# needed.
#
# The App URL is:
#   https://<workspace>.azuredatabricks.net/apps/tiri
