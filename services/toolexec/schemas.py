"""Pydantic models for the Tool Execution Service HTTP API."""

from __future__ import annotations

import zoneinfo
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from . import graph


class CustomApiParamSpec(BaseModel):
    name: str
    location: Literal["body", "query", "header", "path"]
    json_type: Literal["string", "number", "integer", "boolean", "object", "array"]
    description: str = ""
    required: bool = True
    source: Literal["literal", "caller", "upstream"]
    literal_value: Any | None = None
    upstream_api_id: str | None = None
    upstream_json_path: str | None = None
    sensitive: bool = False


# Plaintext credentials, sealed to the tenant on write. Responses never carry
# one back; a stored credential is the string "[stored]" in auth_config.
AuthSecrets = dict[Literal["key_ref", "token_ref", "client_id_ref", "client_secret_ref"], SecretStr]


class CustomApiCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    endpoint_url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    body_style: Literal["json", "form"] = "json"
    auth_scheme: Literal["none", "api_key", "bearer", "oauth2_client_credentials"] = "none"
    auth_config: dict[str, Any] = {}
    auth_secrets: AuthSecrets | None = None
    side_effecting: bool = True
    idempotency_header: str | None = None
    timeout_ms: int | None = None
    sensitive_response_paths: list[str] = []
    success_template: str | None = None
    params: list[CustomApiParamSpec] = []


class CustomApiUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    endpoint_url: str | None = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    body_style: Literal["json", "form"] | None = None
    auth_scheme: Literal["none", "api_key", "bearer", "oauth2_client_credentials"] | None = None
    auth_config: dict[str, Any] | None = None
    auth_secrets: AuthSecrets | None = None
    side_effecting: bool | None = None
    idempotency_header: str | None = None
    timeout_ms: int | None = None
    sensitive_response_paths: list[str] | None = None
    success_template: str | None = None
    params: list[CustomApiParamSpec] | None = None


class AgentCustomApiEnable(BaseModel):
    enabled: bool = True


class OAuthAuthorizeRequest(BaseModel):
    # No redirect field: the only redirect URI ever sent to a provider is the
    # platform's own, from env.
    model_config = ConfigDict(extra="forbid")

    preset_key: str | None = None


class OAuthCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    code: str
    accounts_server: str | None = None


class ApiKeyConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=1, max_length=2048)


_CLOCK_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class CalendarBookingSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_key: Literal["calendar_booking"]
    # Placed in the endpoint URL, so nothing that could reshape the path.
    calendar_id: str = Field("primary", pattern=r"^[A-Za-z0-9_.@#-]{1,254}$")
    timezone: str = "UTC"
    day_start: str = Field("09:00", pattern=_CLOCK_PATTERN)
    day_end: str = Field("17:00", pattern=_CLOCK_PATTERN)
    slot_minutes: int = Field(30, ge=5, le=240)

    @field_validator("calendar_id")
    @classmethod
    def _no_dot_segment(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("calendar_id may not contain '..'")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown timezone") from None
        return value

    @model_validator(mode="after")
    def _day_has_length(self) -> "CalendarBookingSetup":
        if self.day_end <= self.day_start:
            raise ValueError("day_end must be after day_start")
        return self


class WhatsAppSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_key: Literal["whatsapp_confirmation"]
    provider: Literal["gupshup", "interakt", "meta"]
    api_key: SecretStr = Field(min_length=1, max_length=2048)
    template: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    language: str = Field("en", pattern=r"^[a-z]{2}(_[A-Z]{2})?$")
    # Variables the template takes; the model fills each one.
    param_count: int = Field(3, ge=0, le=5)
    source_number: str | None = Field(None, pattern=r"^\d{8,15}$")      # Gupshup
    app_name: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,100}$")  # Gupshup
    phone_number_id: str | None = Field(None, pattern=r"^\d{5,20}$")    # Meta

    @model_validator(mode="after")
    def _provider_fields(self) -> "WhatsAppSetup":
        needs = {
            "gupshup": {"source_number", "app_name"},
            "interakt": set(),
            "meta": {"phone_number_id"},
        }[self.provider]
        for field in ("source_number", "app_name", "phone_number_id"):
            if (getattr(self, field) is not None) != (field in needs):
                raise ValueError(f"{field} is {'required' if field in needs else 'not accepted'} for {self.provider}")
        return self


class SheetsLeadCaptureSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_key: Literal["sheets_lead_capture"]
    title: str = Field("Yuviz leads", min_length=1, max_length=100)


class SalesforceCrmSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_key: Literal["salesforce_crm"]


class HubspotCrmSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_key: Literal["hubspot_crm"]


class ZohoCrmSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_key: Literal["zoho_crm"]


PresetApplyRequest = Annotated[
    CalendarBookingSetup | WhatsAppSetup | SheetsLeadCaptureSetup
    | SalesforceCrmSetup | HubspotCrmSetup | ZohoCrmSetup,
    Field(discriminator="preset_key"),
]


class ChainExecuteRequest(BaseModel):
    tenant_id: str
    agent_id: str
    call_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    idempotency_key: str
    api_name: str
    caller_arguments: dict[str, Any] = {}
    # Whole-chain wall clock, derived from the turn's deadline; server
    # clamps to TOOLEXEC_MAX_CHAIN_BUDGET_MS (can only lower).
    chain_budget_ms: int
    # Untrusted; also re-clamped in the executor and graph.resolve_order().
    max_chain_depth: int = Field(ge=1, le=graph.MAX_CHAIN_LEVELS)
    # The call's own metadata, stamped by Conversation from the route. Three
    # independent fields, never one pre-chosen "remote number": toolexec picks
    # the remote party from the direction. call_direction is a plain str so an
    # unknown value fails closed on the steps that need it instead of 422-ing
    # the whole chain.
    caller_number: str = Field("", max_length=32)
    called_number: str = Field("", max_length=32)
    call_direction: str = Field("", max_length=16)


class ChainStepReport(BaseModel):
    api_name: str
    level: int
    status: Literal[
        "claimed", "success", "failed", "timeout", "skipped", "invalid_argument", "unavailable",
        "confirmation_required",
    ]
    # Param names sourced from an upstream response, for the admin-facing
    # chain history view.
    from_prior_step: list[str] = []


class ChainExecuteResponse(BaseModel):
    run_id: str
    chain_status: Literal[
        "success", "partial", "failed", "timeout", "invalid_argument", "unavailable", "rate_limited",
        "confirmation_required",
    ]
    steps: list[ChainStepReport] = []
    # Reported even when the chain as a whole failed.
    completed_steps: list[str] = []
    failed_step: ChainStepReport | None = None
    # Redacted final-step response, only on success; may be a JSON array.
    data: dict[str, Any] | list[Any] = {}
    missing_fields: list[dict] = []
    deterministic_response: str | None = None
    error: str | None = None
