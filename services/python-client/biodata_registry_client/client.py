"""
:class:`BioDataRegistryClient` — the user-facing facade.

One method per OpenAPI ``operationId`` defined in
``openapi/openapi.yaml``. Methods are intentionally thin wrappers over
:func:`biodata_registry_client._http.send`: they handle URL templating
and parameter packing, then delegate the network I/O, auth, and error
decoding to the shared adapter.

Why this shape (and not, say, one class per resource):

* Matches what ``openapi-python-client --output-path
  biodata_registry_client/`` would emit for a small surface like ours.
  Keeping the shape parallel makes it a drop-in replacement when the
  generator becomes available in the build environment.
* Callers can grep for ``client.create_asset`` and trivially trace it
  to ``operationId: createAsset`` in the spec.
* No per-resource sub-clients to keep in sync as endpoints come and go.

The bodies passed to / returned from these methods are plain
``dict[str, Any]``. The OpenAPI-generated Pydantic models would slot in
here without changing call sites, because every method ultimately hands
its body to :mod:`json` — but for the PoC we keep the dependency
surface small (no aind-data-schema in the client package).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import requests

from biodata_registry_client._http import DEFAULT_TIMEOUT_S, send
from biodata_registry_client._token import CognitoTokenSource


class BioDataRegistryClient:
    """Pip-installable wrapper over the BioData Registry REST API.

    Construct once per long-lived caller; the client is cheap to keep
    around and benefits from connection pooling via the shared
    :class:`requests.Session`.

    Parameters
    ----------
    api_url:
        Base URL of the API Gateway, e.g.
        ``https://api.biodata-registry.alleninstitute.org``.
    cognito_user_pool_id:
        Cognito User Pool id (e.g. ``us-west-2_AbcDefGhi``).
    cognito_app_client_id:
        Cognito User Pool App Client id used for ``REFRESH_TOKEN_AUTH``.
    region:
        AWS region the user pool lives in.
    refresh_token:
        Long-lived Cognito refresh token. Required when ``id_token``
        is not pre-supplied; required for transparent refresh in
        steady state. The recommended source is the Cognito
        authentication response from a SAML federation flow.
    id_token:
        Pre-minted Cognito ID token. Optional — when supplied, used
        until expiry, then refreshed via ``refresh_token``.
    request_timeout_s:
        Per-call HTTP timeout. Default 30s, matching API Gateway's
        29s integration timeout plus a small connection-overhead
        margin.
    session:
        Optional :class:`requests.Session`. When ``None`` the client
        creates and owns its own session for connection pooling.
    token_source:
        Override the auto-constructed :class:`CognitoTokenSource`
        with a user-supplied one. Useful for sharing a token cache
        across multiple clients (e.g. a service that calls both
        ``BioDataRegistryClient`` and another Cognito-fronted
        service); also the test seam.
    """

    def __init__(
        self,
        *,
        api_url: str,
        cognito_user_pool_id: str = "",
        cognito_app_client_id: str = "",
        region: str = "",
        refresh_token: Optional[str] = None,
        id_token: Optional[str] = None,
        request_timeout_s: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
        token_source: Optional[CognitoTokenSource] = None,
    ) -> None:
        if not api_url:
            raise ValueError("api_url must be a non-empty URL")
        self._api_url = api_url
        self._timeout_s = request_timeout_s
        # Owned vs borrowed session: when the caller passes one in we
        # don't close it on __exit__. This matches the requests
        # convention.
        self._session_is_owned = session is None
        self._session = session if session is not None else requests.Session()

        if token_source is not None:
            self._tokens = token_source
        else:
            if refresh_token is None and id_token is None:
                raise ValueError(
                    "BioDataRegistryClient requires either an id_token "
                    "or a refresh_token (or both) for authentication."
                )
            self._tokens = CognitoTokenSource(
                cognito_user_pool_id=cognito_user_pool_id,
                cognito_app_client_id=cognito_app_client_id,
                region=region,
                refresh_token=refresh_token,
                id_token=id_token,
            )

    # -----------------------------------------------------------------
    # Lifecycle helpers.
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Release the owned :class:`requests.Session`, if any."""
        if self._session_is_owned:
            self._session.close()

    def __enter__(self) -> "BioDataRegistryClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -----------------------------------------------------------------
    # Internal request helper — keeps the per-method bodies one-liners.
    # -----------------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        query: Optional[Mapping[str, Any]] = None,
        expected_status: tuple[int, ...] = (200, 201, 202, 204),
        public: bool = False,
    ) -> Any:
        return send(
            base_url=self._api_url,
            method=method,
            path=path,
            token_source=None if public else self._tokens,
            json_body=json_body,
            query=query,
            expected_status=expected_status,
            timeout_s=self._timeout_s,
            session=self._session,
        )

    # =================================================================
    # Health
    # =================================================================

    def get_health(self) -> Mapping[str, Any]:
        """``GET /healthz`` (operationId: ``getHealth``). Public, no auth."""
        return self._send("GET", "/healthz", expected_status=(200,), public=True)

    # =================================================================
    # Assets (Registration_Lambda)
    # =================================================================

    def create_asset(self, asset: Mapping[str, Any]) -> Mapping[str, Any]:
        """``POST /assets`` (operationId: ``createAsset``).

        201 includes a ``warnings`` array when the synchronous duplicate
        check found likely matches; warnings are advisory and do not
        raise. The only 409 path is :class:`DuplicateEntity` from the
        ``data_asset_storage_uri_unique`` constraint.
        """
        return self._send("POST", "/assets", json_body=asset, expected_status=(201,))

    def list_assets(
        self,
        *,
        space_id: Optional[str] = None,
        lifecycle_state: Optional[str] = None,
        validation_status: Optional[str] = None,
        validated_only: Optional[bool] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``GET /assets`` (operationId: ``listAssets``)."""
        return self._send(
            "GET",
            "/assets",
            query={
                "space_id": space_id,
                "lifecycle_state": lifecycle_state,
                "validation_status": validation_status,
                "validated_only": validated_only,
                "limit": limit,
                "cursor": cursor,
            },
            expected_status=(200,),
        )

    def get_asset(self, asset_id: str) -> Mapping[str, Any]:
        """``GET /assets/{id}`` (operationId: ``getAsset``).

        Raises :class:`SensitiveAccessDenied` when the caller has
        structural visibility but lacks the sensitive-flag privilege
        (R8). Raises :class:`NotFound` when the asset is hidden by RLS
        — note that 404 may mean "exists but invisible", a deliberate
        design choice to avoid existence side-channels.
        """
        return self._send("GET", f"/assets/{asset_id}", expected_status=(200,))

    def update_asset(
        self, asset_id: str, asset: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """``PUT /assets/{id}`` (operationId: ``updateAsset``)."""
        return self._send(
            "PUT",
            f"/assets/{asset_id}",
            json_body=asset,
            expected_status=(200,),
        )

    # =================================================================
    # Entities (Registration_Lambda, polymorphic)
    # =================================================================

    def create_entity(
        self, entity_type: str, entity: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """``POST /entities/{type}`` (operationId: ``createEntity``)."""
        return self._send(
            "POST",
            f"/entities/{entity_type}",
            json_body=entity,
            expected_status=(201,),
        )

    def get_entity(self, entity_type: str, entity_id: str) -> Mapping[str, Any]:
        """``GET /entities/{type}/{id}`` (operationId: ``getEntity``)."""
        return self._send(
            "GET",
            f"/entities/{entity_type}/{entity_id}",
            expected_status=(200,),
        )

    def update_entity(
        self,
        entity_type: str,
        entity_id: str,
        entity: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """``PUT /entities/{type}/{id}`` (operationId: ``updateEntity``)."""
        return self._send(
            "PUT",
            f"/entities/{entity_type}/{entity_id}",
            json_body=entity,
            expected_status=(200,),
        )

    # =================================================================
    # Lifecycle (Lifecycle_Lambda)
    # =================================================================

    def publish_asset(self, asset_id: str) -> Mapping[str, Any]:
        """``POST /assets/{id}/publish`` (operationId: ``publishAsset``).

        Raises :class:`InvalidStateTransition` when the current state
        is not ``registered``; raises :class:`ValidationFailed` when
        ``validation_status != 'valid'``.
        """
        return self._send(
            "POST", f"/assets/{asset_id}/publish", expected_status=(200,)
        )

    def register_asset(self, asset_id: str) -> Mapping[str, Any]:
        """``POST /assets/{id}/register`` (operationId: ``registerAsset``)."""
        return self._send(
            "POST", f"/assets/{asset_id}/register", expected_status=(200,)
        )

    def archive_asset(self, asset_id: str) -> Mapping[str, Any]:
        """``POST /assets/{id}/archive`` (operationId: ``archiveAsset``)."""
        return self._send(
            "POST", f"/assets/{asset_id}/archive", expected_status=(200,)
        )

    # =================================================================
    # Validation (Validation_Lambda)
    # =================================================================

    def validate_metadata(
        self, *, entity_type: str, entity_id: str
    ) -> Mapping[str, Any]:
        """``POST /validate`` (operationId: ``validateMetadata``)."""
        return self._send(
            "POST",
            "/validate",
            json_body={"entity_type": entity_type, "entity_id": entity_id},
            expected_status=(200,),
        )

    def validate_metadata_dry_run(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """``POST /validate/dry-run`` (operationId: ``validateMetadataDryRun``)."""
        return self._send(
            "POST",
            "/validate/dry-run",
            json_body=payload,
            expected_status=(200,),
        )

    def create_custom_schema(
        self, *, org_id: str, name: str, json_schema: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """``POST /schemas/custom`` (operationId: ``createCustomSchema``)."""
        return self._send(
            "POST",
            "/schemas/custom",
            json_body={"org_id": org_id, "name": name, "json_schema": json_schema},
            expected_status=(201,),
        )

    def create_schema_version(
        self, schema_id: str, *, version: str, json_schema: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """``POST /schemas/{id}/versions`` (operationId: ``createSchemaVersion``)."""
        return self._send(
            "POST",
            f"/schemas/{schema_id}/versions",
            json_body={"version": version, "json_schema": json_schema},
            expected_status=(201,),
        )

    # =================================================================
    # Search (Search_Lambda)
    # =================================================================

    def search(
        self,
        *,
        q: Optional[str] = None,
        filters: Optional[str] = None,
        validated_only: Optional[bool] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``GET /search`` (operationId: ``search``).

        Public for ``lifecycle_state=published``; broadens to private
        data the authenticated caller can see when an ID token is
        attached.
        """
        return self._send(
            "GET",
            "/search",
            query={
                "q": q,
                "filters": filters,
                "validated_only": validated_only,
                "limit": limit,
                "cursor": cursor,
            },
            expected_status=(200,),
        )

    def suggest(self, *, prefix: str, limit: Optional[int] = None) -> Mapping[str, Any]:
        """``GET /suggest`` (operationId: ``suggest``)."""
        return self._send(
            "GET",
            "/suggest",
            query={"prefix": prefix, "limit": limit},
            expected_status=(200,),
        )

    def nl_search(self, query: str) -> Mapping[str, Any]:
        """``POST /search/nl`` (operationId: ``nlSearch``).

        Bedrock-backed; expect higher latency than other endpoints.
        The default 30s timeout normally suffices; pass a larger
        ``request_timeout_s`` at construction time for long generations.
        """
        return self._send(
            "POST",
            "/search/nl",
            json_body={"query": query},
            expected_status=(200,),
        )

    # =================================================================
    # Duplicates (Duplicates_Lambda)
    # =================================================================

    def list_duplicates(
        self,
        *,
        dismissed: Optional[bool] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``GET /duplicates`` (operationId: ``listDuplicates``)."""
        return self._send(
            "GET",
            "/duplicates",
            query={"dismissed": dismissed, "limit": limit, "cursor": cursor},
            expected_status=(200,),
        )

    def merge_duplicate(
        self, duplicate_id: str, *, survivor_id: str, absorbed_id: str
    ) -> Mapping[str, Any]:
        """``POST /duplicates/{id}/merge`` (operationId: ``mergeDuplicate``)."""
        return self._send(
            "POST",
            f"/duplicates/{duplicate_id}/merge",
            json_body={"survivor_id": survivor_id, "absorbed_id": absorbed_id},
            expected_status=(200,),
        )

    def dismiss_duplicate(self, duplicate_id: str) -> Mapping[str, Any]:
        """``POST /duplicates/{id}/dismiss`` (operationId: ``dismissDuplicate``)."""
        return self._send(
            "POST",
            f"/duplicates/{duplicate_id}/dismiss",
            expected_status=(200,),
        )

    # =================================================================
    # Governance (Governance_Lambda)
    # =================================================================

    def create_org(self, *, name: str, display_name: str) -> Mapping[str, Any]:
        """``POST /orgs`` (operationId: ``createOrg``)."""
        return self._send(
            "POST",
            "/orgs",
            json_body={"name": name, "display_name": display_name},
            expected_status=(201,),
        )

    def create_space(
        self,
        org_id: str,
        *,
        name: str,
        display_name: str,
        parent_space_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``POST /orgs/{id}/spaces`` (operationId: ``createSpace``)."""
        body: dict[str, Any] = {"name": name, "display_name": display_name}
        if parent_space_id is not None:
            body["parent_space_id"] = parent_space_id
        return self._send(
            "POST", f"/orgs/{org_id}/spaces", json_body=body, expected_status=(201,)
        )

    def assign_role(
        self,
        org_id: str,
        user_id: str,
        *,
        role: str,
        space_id: Optional[str] = None,
    ) -> None:
        """``PUT /orgs/{id}/users/{uid}/role`` (operationId: ``assignRole``).

        Returns ``None`` on success (204 No Content).
        """
        body: dict[str, Any] = {"role": role}
        if space_id is not None:
            body["space_id"] = space_id
        self._send(
            "PUT",
            f"/orgs/{org_id}/users/{user_id}/role",
            json_body=body,
            expected_status=(204,),
        )

    def create_sharing_grant(
        self, org_id: str, grant: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """``POST /orgs/{id}/sharing-grants`` (operationId: ``createSharingGrant``)."""
        return self._send(
            "POST",
            f"/orgs/{org_id}/sharing-grants",
            json_body=grant,
            expected_status=(201,),
        )

    def request_access(
        self,
        org_id: str,
        *,
        requested_role: str,
        requested_space_id: Optional[str] = None,
        justification: Optional[str] = None,
    ) -> None:
        """``POST /orgs/{id}/access-requests`` (operationId: ``requestAccess``)."""
        body: dict[str, Any] = {"requested_role": requested_role}
        if requested_space_id is not None:
            body["requested_space_id"] = requested_space_id
        if justification is not None:
            body["justification"] = justification
        self._send(
            "POST",
            f"/orgs/{org_id}/access-requests",
            json_body=body,
            expected_status=(202,),
        )

    def provision_user(
        self,
        org_id: str,
        *,
        email: str,
        role: Optional[str] = None,
        space_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``POST /orgs/{id}/users`` (operationId: ``provisionUser``)."""
        body: dict[str, Any] = {"email": email}
        if role is not None:
            body["role"] = role
        if space_id is not None:
            body["space_id"] = space_id
        return self._send(
            "POST",
            f"/orgs/{org_id}/users",
            json_body=body,
            expected_status=(201,),
        )

    # =================================================================
    # Revisions (Revisions_Lambda)
    # =================================================================

    def list_revisions(
        self,
        *,
        entity_type: str,
        entity_id: str,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``GET /revisions`` (operationId: ``listRevisions``)."""
        return self._send(
            "GET",
            "/revisions",
            query={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "limit": limit,
                "cursor": cursor,
            },
            expected_status=(200,),
        )

    def get_revision_at(
        self,
        *,
        entity_type: str,
        entity_id: str,
        revision_number: int,
    ) -> Mapping[str, Any]:
        """``GET /revisions/{entity_type}/{id}/at/{revision_number}`` (operationId: ``getRevisionAt``)."""
        return self._send(
            "GET",
            f"/revisions/{entity_type}/{entity_id}/at/{revision_number}",
            expected_status=(200,),
        )

    # =================================================================
    # Collections (Collections_Lambda)
    # =================================================================

    def create_collection(
        self,
        *,
        org_id: str,
        name: str,
        doi: Optional[str] = None,
    ) -> Mapping[str, Any]:
        """``POST /collections`` (operationId: ``createCollection``)."""
        body: dict[str, Any] = {"org_id": org_id, "name": name}
        if doi is not None:
            body["doi"] = doi
        return self._send(
            "POST", "/collections", json_body=body, expected_status=(201,)
        )

    def add_collection_asset(self, collection_id: str, *, data_asset_id: str) -> None:
        """``POST /collections/{id}/assets`` (operationId: ``addCollectionAsset``)."""
        self._send(
            "POST",
            f"/collections/{collection_id}/assets",
            json_body={"data_asset_id": data_asset_id},
            expected_status=(204,),
        )

    def add_collection_child(self, collection_id: str, *, child_id: str) -> None:
        """``POST /collections/{id}/children`` (operationId: ``addCollectionChild``).

        Raises :class:`InvalidHierarchy` when adding the child would
        produce a cycle.
        """
        self._send(
            "POST",
            f"/collections/{collection_id}/children",
            json_body={"child_id": child_id},
            expected_status=(204,),
        )

    def set_collection_doi(self, collection_id: str, *, doi: str) -> None:
        """``PUT /collections/{id}/doi`` (operationId: ``setCollectionDoi``)."""
        self._send(
            "PUT",
            f"/collections/{collection_id}/doi",
            json_body={"doi": doi},
            expected_status=(204,),
        )

    # =================================================================
    # Observability (Observability_Lambda)
    # =================================================================

    def get_asset_counts(self) -> Mapping[str, Any]:
        """``GET /metrics/asset-counts`` (operationId: ``getAssetCounts``)."""
        return self._send(
            "GET", "/metrics/asset-counts", expected_status=(200,)
        )

    def get_validation_distribution(self) -> Mapping[str, Any]:
        """``GET /metrics/validation-distribution`` (operationId: ``getValidationDistribution``)."""
        return self._send(
            "GET", "/metrics/validation-distribution", expected_status=(200,)
        )

    def get_growth(self, *, from_date: str, to_date: str) -> Mapping[str, Any]:
        """``GET /metrics/growth`` (operationId: ``getGrowth``).

        Note: ``from`` is a Python keyword so we accept ``from_date``
        and rename on the wire — same trick the OpenAPI generator
        uses.
        """
        return self._send(
            "GET",
            "/metrics/growth",
            query={"from": from_date, "to": to_date},
            expected_status=(200,),
        )

    # =================================================================
    # Agent (MetaData_Agent_Lambda — proxy to AgentCore)
    # =================================================================

    def agent_chat(
        self, *, conversation_id: str, message: str
    ) -> Mapping[str, Any]:
        """``POST /agent/chat`` (operationId: ``agentChat``)."""
        return self._send(
            "POST",
            "/agent/chat",
            json_body={"conversation_id": conversation_id, "message": message},
            expected_status=(200,),
        )


__all__ = ("BioDataRegistryClient",)
