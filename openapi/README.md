# Allen BioData Registry — OpenAPI Spec

This directory holds the **single source of truth** for the registry's
REST API surface.

```
openapi/
├── README.md                       ← you are here
├── openapi.yaml                    ← OpenAPI 3.0 spec (hand-authored)
└── components/
    └── schemas/
        ├── DataAsset.json          ← aind-data-schema placeholder/export
        ├── Subject.json            ← aind-data-schema placeholder/export
        ├── …
        ├── Organization.json       ← registry governance model
        ├── SharingGrant.json       ← registry governance model
        ├── ErrorResponse.json      ← Property 14 error shape
        ├── Role.json               ← registry enum
        └── …
```

The spec is referenced from three places:

1. **API Gateway** imports it via the Terraform `apigateway` module
   (Task 14.1) so all routes are declared once and Lambda
   integrations bind by `operationId`.
2. **Business Lambdas** validate every incoming request against it
   at runtime via `biodata_registry_shared.openapi_middleware` (Task
   12.1).
3. **The Python client** (`BioDataRegistryClient`, Task 13.2) is
   generated from it via `openapi-python-client` so SDK and server
   stay in lockstep.

## Schema regeneration workflow

The component JSON Schemas are **generated**, not hand-written. They
come from two Pydantic v2 sources:

| Schema family | Source |
|---|---|
| Biological models (`Subject`, `Instrument`, `Rig`, `Procedures`, `Session`, `Acquisition`, `Processing`, `QualityControl`, `DataDescription`, `DataAsset`) | The `aind-data-schema` package — the canonical AIND library. |
| Governance models (`Organization`, `Space`, `SharingGrant`, `EntityRevision`, `LifecycleTransition`, `DuplicateFlag`, `ErrorResponse`, `Warnings`, `DuplicateWarning`) | `services/openapi-types/biodata_registry_types.py` (this repo). |
| Enums (`Role`, `LifecycleState`, `ValidationStatus`, `ChangeSourceKind`, `EntityType`) | Same as governance — exported as standalone JSON Schema enums. |

To regenerate after a Pydantic-source change:

```bash
python scripts/export_schemas.py
```

This rewrites every file under `openapi/components/schemas/`. The
output is **deterministic** (sorted keys, 2-space indent, trailing
newline) so a no-op regeneration produces a clean `git diff`.

### PoC graceful degradation

The `aind-data-schema` package is not installed in the bare PoC
build (it ships inside the Lambda Layer for production runtimes —
see `services/shared-layer/requirements.txt`). When `export_schemas.py`
can't import a model, it writes a **placeholder schema** marked with:

```json
{
  "x-aind-placeholder": true,
  "x-aind-source": "aind-data-schema",
  "description": "… **PoC placeholder.** … install aind-data-schema>=2.7,<3 and re-run …",
  …
}
```

This keeps the OpenAPI spec valid (clients can still generate, the
middleware can still validate request shape), and CI can still gate
on the script's output. To produce real schemas, install the package
first:

```bash
pip install "aind-data-schema>=2.7,<3"
python scripts/export_schemas.py
```

## CI gate

The CI build runs:

```bash
python scripts/export_schemas.py --check
```

This regenerates schemas to a tempdir and compares them byte-for-byte
with the checked-in files. **Exit code is non-zero if anything has
drifted** — the build fails with a unified diff showing exactly what
changed. The intent: a Pydantic edit that doesn't include the
regenerated JSON files cannot merge.

To debug a `--check` failure locally:

```bash
python scripts/export_schemas.py             # regenerate
git diff openapi/components/schemas/         # see what changed
git add openapi/components/schemas/ && git commit
```

## Endpoint summary

The full surface is documented in `openapi.yaml`. Quick reference,
grouped by Lambda:

| Lambda | Endpoints | Auth |
|---|---|---|
| *(API GW mock)* | `GET /healthz` | **public** |
| Registration | `POST/GET /assets`, `GET/PUT /assets/{id}`, `POST /entities/{type}`, `GET/PUT /entities/{type}/{id}` | Cognito JWT |
| Lifecycle | `POST /assets/{id}/{publish,register,archive}` | Cognito JWT |
| Validation | `POST /validate`, `POST /validate/dry-run`, `POST /schemas/custom`, `POST /schemas/{id}/versions` | Cognito JWT |
| Search | `GET /search`, `GET /suggest`, `POST /search/nl` | **public for `lifecycle_state=published`**; Cognito JWT for broader visibility |
| Duplicates | `GET /duplicates`, `POST /duplicates/{id}/{merge,dismiss}` | Cognito JWT |
| Governance | `POST /orgs`, `POST /orgs/{id}/spaces`, `PUT /orgs/{id}/users/{uid}/role`, `POST /orgs/{id}/{sharing-grants,access-requests,users}` | Cognito JWT |
| Revisions | `GET /revisions`, `GET /revisions/{entity_type}/{id}/at/{revision_number}` | Cognito JWT |
| Collections | `POST /collections`, `POST /collections/{id}/{assets,children}`, `PUT /collections/{id}/doi` | Cognito JWT |
| Observability | `GET /metrics/{asset-counts,validation-distribution,growth}` | Cognito JWT |
| MetaData_Agent | `POST /agent/chat` | Cognito JWT |

Public endpoints are listed under the `x-public-endpoints` vendor
extension at the top of `openapi.yaml` so the Authorizer Lambda has
a single source of truth for the bypass list. Default security
applies CognitoJWT to all other operations (`security: - CognitoJWT: []`
at the spec root).

## Generating the Python client

```bash
# Once aind-data-schema is installed and the spec is committed:
pip install openapi-python-client
openapi-python-client generate --path openapi/openapi.yaml \
    --custom-template-path scripts/openapi-templates/  # optional, for typed exceptions
```

This produces a pip-installable package (e.g. `biodata-registry-client`)
that exposes:

* Pydantic models for every `$ref`'d schema.
* A typed client with one method per `operationId` (`createAsset`,
  `publishAsset`, `search`, …).
* A token-refresh interceptor (added in Task 13.2 — not part of the
  raw generator output).
* Typed exceptions that map server-returned `code` values to local
  classes (`ValidationFailed`, `InvalidStateTransition`, …) per R30.5
  / Property 14.

See `services/python-client/` (Task 13.2) for the wrapper repo that
adds the auth interceptor and exception mapping on top of the
generated package.

## Authoring guidelines

When you change the spec:

1. **Edit Pydantic first.** If you're touching a governance model,
   edit `services/openapi-types/biodata_registry_types.py`. Don't
   hand-edit the JSON files — the regen will overwrite you.
2. **Run the export.** `python scripts/export_schemas.py`.
3. **Run the tests.** `pytest scripts/test_export_schemas.py`.
4. **Update the YAML** if you're adding a new endpoint or response
   shape, referencing the schema with
   `$ref: './components/schemas/<Model>.json'`.
5. **Commit both** the YAML and the regenerated JSON together. The
   CI gate enforces this — orphaned edits will fail the build.

## References

* Design doc: `.kiro/specs/allen-biodata-registry-poc/design.md`
  §External Interfaces.API Gateway REST.
* Requirements: R14 (REST API), R15 (Python client), R30
  (Property 14 error shape).
* Tasks: 13.1 (this work), 13.2 (Python client), 14.1 (API Gateway
  Terraform module).
