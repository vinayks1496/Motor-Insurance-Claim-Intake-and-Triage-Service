from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LossKind(StrEnum):
    collision = "collision"
    theft = "theft"
    fire = "fire"
    flood = "flood"
    third_party = "third_party"
    other = "other"


class VehicleUse(StrEnum):
    private = "private"
    commuting = "commuting"
    professional = "professional"
    delivery = "delivery"
    commercial = "commercial"
    unknown = "unknown"


class Channel(StrEnum):
    web = "web"
    mobile = "mobile"
    call_centre = "call_centre"
    agent = "agent"


class FNOLRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_reference: Annotated[str, Field(min_length=3, max_length=64)]
    policy_number: Annotated[str, Field(min_length=5, max_length=40)]
    insurer: Annotated[str, Field(min_length=2, max_length=80)] = "New India Assurance"
    product: Annotated[str, Field(min_length=2, max_length=100)] = "Private Car Base Package Policy"
    registration_number: Annotated[str, Field(min_length=6, max_length=16)]
    loss_date: date
    reported_at: date
    loss_description: Annotated[str, Field(min_length=20, max_length=4000)]
    loss_kind: LossKind = LossKind.other
    vehicle_use: VehicleUse = VehicleUse.unknown
    channel: Channel = Channel.web
    estimate_inr: Annotated[float | None, Field(ge=0, le=50_000_000, allow_inf_nan=False)] = None
    injury_reported: bool = False
    injury_count: Annotated[int, Field(ge=0, le=99)] = 0
    police_reported: bool = False
    policy_start_date: date | None = None
    policy_end_date: date | None = None

    @field_validator("registration_number")
    @classmethod
    def normalize_registration(cls, value: str) -> str:
        normalized = re.sub(r"[^A-Z0-9]", "", value.upper())
        if not re.fullmatch(r"[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}", normalized):
            raise ValueError("must be a valid Indian vehicle registration format")
        return normalized

    @field_validator("insurer")
    @classmethod
    def normalize_insurer(cls, value: str) -> str:
        if value.lower().replace(" ", "") in {"newindiaassurance", "newindia"}:
            return "New India Assurance"
        if value.lower() in {"other", "outside corpus"}:
            return "Other"
        raise ValueError("must be New India Assurance or Other")

    @field_validator("product")
    @classmethod
    def normalize_product(cls, value: str) -> str:
        normalized = " ".join(value.lower().split())
        if normalized in {"private car base package policy", "private car package policy"}:
            return "Private Car Base Package Policy"
        if normalized in {"other", "outside corpus"}:
            return "Other"
        raise ValueError("must be Private Car Base Package Policy or Other")

    @field_validator("policy_number")
    @classmethod
    def valid_policy_number_characters(cls, value: str) -> str:
        normalized = value.upper()
        if not re.fullmatch(r"[A-Z0-9/-]+", normalized):
            raise ValueError("may contain letters, digits, hyphens, and slashes only")
        return normalized

    @field_validator("loss_date", "reported_at", "policy_start_date", "policy_end_date")
    @classmethod
    def date_not_in_future(cls, value: date | None) -> date | None:
        if value and value > datetime.now(timezone.utc).date():
            raise ValueError("date cannot be in the future")
        return value

    @model_validator(mode="after")
    def validate_related_fields(self) -> Self:
        if self.reported_at < self.loss_date:
            raise ValueError("reported_at must be on or after loss_date")
        if self.injury_reported and self.injury_count < 1:
            raise ValueError("injury_count must be at least 1 when injury_reported is true")
        if not self.injury_reported and self.injury_count > 0:
            raise ValueError("injury_reported must be true when injury_count is greater than 0")
        if self.policy_start_date and self.loss_date < self.policy_start_date:
            raise ValueError("loss_date is before the supplied policy_start_date")
        if self.policy_end_date and self.loss_date > self.policy_end_date:
            raise ValueError("loss_date is after the supplied policy_end_date")
        if self.policy_start_date and self.policy_end_date and self.policy_start_date > self.policy_end_date:
            raise ValueError("policy_start_date must be on or before policy_end_date")
        return self


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    satisfactory: bool
    reason: Annotated[str, Field(min_length=4, max_length=1000)]


class DemoOutageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    duration_seconds: Annotated[int, Field(ge=10, le=300)] = 60
