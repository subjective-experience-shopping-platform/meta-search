from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sentiment = Literal["positive", "negative", "mixed", "neutral", "unknown"]
Availability = Literal["available", "sold_out", "discontinued", "unknown"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ObjectiveFilter(InputModel):
    key: str
    op: Literal["eq", "ne", "in", "not_in", "gte", "lte", "between", "contains_any", "contains_all", "exists"]
    value: Any = None


class OfferFilter(InputModel):
    max_price: int | None = Field(None, ge=0)
    min_price: int | None = Field(None, ge=0)
    currency: str = Field("KRW", pattern=r"^[A-Z]{3}$")
    availability: list[Availability] | None = Field(None, min_length=1)
    max_shipping_fee: int | None = Field(None, ge=0)
    max_shipping_days: int | None = Field(None, ge=0)
    retailers: list[str] | None = Field(None, min_length=1)

    @model_validator(mode="after")
    def ranges(self):
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price는 max_price보다 클 수 없습니다")
        return self


class ExperienceTopic(InputModel):
    topic_id: str = Field(min_length=1)
    aspect_variant_ids: list[str] = Field(min_length=1)
    desired_state_variant_ids: list[str] | None = Field(None, min_length=1)
    contrary_state_variant_ids: list[str] = Field(default_factory=list)


class ExperienceCondition(InputModel):
    condition_id: str = Field(min_length=1)
    aspect_variant_ids: list[str] = Field(min_length=1)
    desired_state_variant_ids: list[str] | None = Field(None, min_length=1)
    contrary_state_variant_ids: list[str] = Field(default_factory=list)
    sentiments: list[Sentiment] | None = Field(None, min_length=1)
    min_matching_reviews: int = Field(1, ge=1)
    max_contrary_reviews: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def contrary_required(self):
        if self.max_contrary_reviews is not None and not self.contrary_state_variant_ids:
            raise ValueError("max_contrary_reviews에는 contrary_state_variant_ids가 필요합니다")
        return self


class SortSpec(InputModel):
    by: Literal["price", "attribute", "experience"] = "price"
    key: str | None = None
    aspect_variant_ids: list[str] | None = Field(None, min_length=1)
    metric: Literal["positive_wilson_lower", "state_wilson_lower"] = "positive_wilson_lower"
    desired_state_variant_ids: list[str] | None = Field(None, min_length=1)
    order: Literal["asc", "desc"] | None = None

    @model_validator(mode="after")
    def required_selectors(self):
        if self.by == "attribute" and not self.key:
            raise ValueError("attribute 정렬에는 key가 필요합니다")
        if self.by == "experience" and not self.aspect_variant_ids:
            raise ValueError("experience 정렬에는 aspect_variant_ids가 필요합니다")
        if (
            self.by == "experience"
            and self.metric == "state_wilson_lower"
            and not self.desired_state_variant_ids
        ):
            raise ValueError("state_wilson_lower에는 desired_state_variant_ids가 필요합니다")
        return self
