from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import jobs
from .config import load_settings
from .db import database, init_schema
from .policy import INSTRUCTIONS
from .search.models import (
    Availability,
    ExperienceCondition,
    ExperienceTopic,
    ObjectiveFilter,
    OfferFilter,
    Sentiment,
    SortSpec,
)
from .search.service import SearchService
from .validation import ValidationError

settings = load_settings()
with database(settings.db_path) as conn:
    init_schema(conn, settings.initial_table_path)
service = SearchService(settings)
mcp = FastMCP("meta-search", instructions=INSTRUCTIONS)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@mcp.tool(annotations=READ)
def get_search_guide(category: str | None = None) -> dict[str, Any]:
    """카테고리·동적 필터 키/연산자·관측값과 답변 정책을 확인한다."""
    return service.get_search_guide(category)


@mcp.tool(annotations=READ)
def search_products(
    category: str | None = None,
    filters: list[ObjectiveFilter] = [],
    offer_filter: OfferFilter | None = None,
    experience_conditions: list[ExperienceCondition] = [],
    sort: SortSpec | None = None,
    evidence_topics: list[ExperienceTopic] = [],
    limit: Annotated[int | None, Field(ge=1)] = None,
    user_request: str | None = None,
    interpretation_notes: list[str] = [],
) -> dict[str, Any]:
    """전체 조건을 받아 객관·판매·리뷰로 필터하고 정렬한다. applied_filters_markdown을 답변 앞에 그대로 표시한다."""
    return service.search_products(
        category,
        filters,
        offer_filter,
        experience_conditions,
        sort,
        evidence_topics,
        limit,
        user_request,
        interpretation_notes,
    )


@mcp.tool(annotations=READ)
def compare_products(
    product_ids: Annotated[list[int], Field(min_length=2, max_length=5)],
    attribute_keys: list[str] | None = None,
    experience_topics: list[ExperienceTopic] = [],
) -> dict[str, Any]:
    """같은 카테고리의 2~5개 상품을 객관 속성·판매 요약·체감 토픽으로 비교한다."""
    return service.compare_products(product_ids, attribute_keys, experience_topics)


@mcp.tool(annotations=READ)
def get_product_details(product_id: int, experience_topics: list[ExperienceTopic] = []) -> dict[str, Any]:
    """상품의 전체 객관 속성·상세 Markdown·판매처·주요 리뷰 속성을 반환한다."""
    return service.get_product_details(product_id, experience_topics)


@mcp.tool(annotations=READ)
def search_product_docs(
    query: str,
    product_ids: list[int] | None = None,
    category: str | None = None,
    max_results: Annotated[int, Field(ge=1, le=50)] = 15,
) -> dict[str, Any]:
    """상세 문서에서 공백/쉼표로 나눈 키워드가 있는 줄과 가장 가까운 앞 제목을 찾는다."""
    return service.search_product_docs(query, product_ids, category, max_results)


@mcp.tool(annotations=READ)
def find_review_attributes(
    category: str | None = None,
    query: str = "",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """리뷰 속성 표현을 문자 검색한다. matched_selector와 cluster_selector를 보고 범위를 직접 선택한다."""
    return service.find_review_attributes(category, query, limit, offset)


@mcp.tool(annotations=READ)
def list_review_states(
    aspect_variant_ids: Annotated[list[str], Field(min_length=1)],
    product_ids: list[int] | None = None,
    query: str = "",
    limit: Annotated[int, Field(ge=1, le=100)] = 30,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """선택한 원래 aspect 표현의 상태 ID·리뷰 수·감성 분포·예시를 조회한다."""
    return service.list_review_states(aspect_variant_ids, product_ids, query, limit, offset)


@mcp.tool(annotations=READ)
def get_review_evidence(
    aspect_variant_ids: Annotated[list[str], Field(min_length=1)],
    desired_state_variant_ids: Annotated[list[str] | None, Field(min_length=1)] = None,
    contrary_state_variant_ids: list[str] = [],
    product_ids: list[int] | None = None,
    sentiments: Annotated[list[Sentiment] | None, Field(min_length=1)] = None,
    group: Literal["all", "matching", "contrary", "other", "unknown_state"] = "all",
    include_full_review: bool = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """일치·상반·기타·상태 미기재 리뷰의 원문 근거를 확인한다. 한 리뷰에 여러 라벨을 허용한다."""
    return service.get_review_evidence(
        aspect_variant_ids,
        desired_state_variant_ids,
        contrary_state_variant_ids,
        product_ids,
        sentiments,
        group,
        include_full_review,
        limit,
        offset,
    )


@mcp.tool(annotations=READ)
def get_offers(
    product_ids: Annotated[list[int], Field(min_length=1)],
    availability: Annotated[list[Availability] | None, Field(min_length=1)] = None,
) -> dict[str, Any]:
    """판매처별 가격·배송·재고·확인 시각을 반환한다. 판매 정보는 확인 시점의 스냅샷이다."""
    return service.get_offers(product_ids, availability)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def register_product(url: str, category_hint: str | None = None) -> dict[str, Any]:
    """사용자가 상품 링크와 함께 등록을 명시적으로 요청했을 때만 호출한다. 호출 후 결과를 기다리지 말고 대화를 마친다. 완료되면 macOS 알림이 표시된다."""
    try:
        return jobs.register_product(settings, url, category_hint)
    except ValidationError as error:
        raise ToolError(str(error)) from error


@mcp.tool(annotations=READ)
def get_registration_status(job_id: str | None = None) -> dict[str, Any]:
    """등록 작업 하나 또는 최근 10개 작업의 단계·경고·실패 원인을 조회한다."""
    try:
        return jobs.get_registration_status(settings, job_id)
    except ValidationError as error:
        raise ToolError(str(error)) from error


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
