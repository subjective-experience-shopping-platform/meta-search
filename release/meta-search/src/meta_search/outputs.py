"""Strict second-pass LLM DTOs; semantic stage checks remain deterministic Python."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Value = str | int | float | bool | list[str]
ValueType = Literal["string", "number", "boolean", "string_list"]
Sentiment = Literal["positive", "negative", "mixed", "neutral", "unknown"]


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Review(Output):
    body: str
    rating: float | int | None
    written_at: str | None


class PageSnapshot(Output):
    page_ok: bool
    failure_reason: str | None
    page_title: str
    product_name: str
    brand_text: str | None
    price_text: str | None
    spec_text: str
    description_text: str
    review_count_on_page: int | None
    reviews: list[Review]


class Category(Output):
    category_id: str
    display_name: str
    is_new: bool


class Identity(Output):
    name: str
    brand: str
    model: str


class Attribute(Output):
    key: str
    display_name: str
    value_type: ValueType
    unit: str | None
    value: Value


class ObjectiveExtraction(Output):
    category: Category
    identity: Identity
    attributes: list[Attribute]
    details_markdown: str


class ResearchedValue(Output):
    key: str
    value: Value | None
    source_url: str | None
    evidence: str


class GapResearch(Output):
    values: list[ResearchedValue]
    notes_markdown: str


class Criteria(Output):
    search_value: bool
    category_scope: bool
    comparable: bool


class Assignment(Output):
    field_id: str
    value: Value
    value_operation: Literal["keep", "alias"]


class Group(Output):
    canonical_key: str
    display_name: str
    definition: str
    value_type: ValueType
    unit: str | None
    criteria: Criteria
    tier: Literal["core", "extra"]
    reason: str
    assignments: list[Assignment]


class NeedsReview(Output):
    field_id: str
    reason: str


class NormalizationPlan(Output):
    category: str
    base_version: int
    groups: list[Group]
    needs_review: list[NeedsReview]


class OpinionUnit(Output):
    raw_aspect: str
    raw_status: str | None
    excerpt: str
    opinion: str
    sentiment: Sentiment


class OpinionUnits(Output):
    opinion_units: list[OpinionUnit]


class Offer(Output):
    seller: str
    retailer: str
    url: str
    price: int
    currency: str
    shipping_fee: int | None
    shipping_days: int | None
    availability: Literal["available", "sold_out", "discontinued", "unknown"]
    evidence: str


class SalesOffers(Output):
    offers: list[Offer]


OUTPUTS = {
    "page_snapshot.schema.json": PageSnapshot,
    "objective_extraction.schema.json": ObjectiveExtraction,
    "gap_research.schema.json": GapResearch,
    "normalization_plan.schema.json": NormalizationPlan,
    "opinion_units.schema.json": OpinionUnits,
    "sales_offers.schema.json": SalesOffers,
}
