"""Candidate tool wiring for the Greenhouse MCP server."""

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from mcp_auth import get_current_user, require_scopes
from schemas import (
    AddCandidateNoteInput,
    AddCandidateTagInput,
    AddCandidateTagOutput,
    CandidateNoteOutput,
    CandidateOutput,
    CandidateSearchResultOutput,
    CreateCandidateInput,
    GetCandidateInput,
    PaginationMeta,
    SearchCandidatesInput,
    SearchCandidatesOutput,
    UpdateCandidateInput,
)
from services.clean_provider import (
    CandidateNotFoundError,
    CleanProvider,
    DuplicateEmailError,
    UserNotFoundError,
)
from services.pagination import build_pagination_links


@require_scopes(Perm.CANDIDATE_READ.value)
async def greenhouse_candidates_search(params: SearchCandidatesInput) -> SearchCandidatesOutput:
    """Search and filter candidates with various criteria."""
    provider = CleanProvider()
    filters = {
        "name": params.name,
        "email": params.email,
        "job_id": params.job_id,
        "tag": params.tag,
        "created_before": params.created_before,
        "created_after": params.created_after,
        "updated_before": params.updated_before,
        "updated_after": params.updated_after,
        "candidate_ids": params.candidate_ids,
    }

    candidates_data = await provider.search_candidates(
        page=params.page,
        per_page=params.per_page,
        **filters,
    )

    total = None
    if not params.skip_count:
        total = await provider.count_candidates(**filters)

    links = build_pagination_links("/candidates", params.page, params.per_page, total)
    meta = PaginationMeta(
        per_page=params.per_page,
        page=params.page,
        total=total,
        links=links,
    )

    # Convert dict results to Pydantic models
    candidates = [CandidateSearchResultOutput(**c) for c in candidates_data]

    return SearchCandidatesOutput(candidates=candidates, meta=meta)


@require_scopes(Perm.CANDIDATE_READ.value)
async def greenhouse_candidates_get(params: GetCandidateInput) -> CandidateOutput:
    """Retrieve complete candidate profile with all related data."""
    provider = CleanProvider()
    try:
        return await provider.get_candidate(params.candidate_id)
    except CandidateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@require_scopes(Perm.CANDIDATE_CREATE.value)
async def greenhouse_candidates_create(params: CreateCandidateInput) -> CandidateOutput:
    """Create a new candidate in Greenhouse."""
    provider = CleanProvider()

    # Convert Pydantic models to dicts for the provider
    email_addresses = [{"value": e.value, "type": e.type} for e in params.email_addresses]

    phone_numbers = None
    if params.phone_numbers:
        phone_numbers = [{"value": p.value, "type": p.type} for p in params.phone_numbers]

    addresses = None
    if params.addresses:
        addresses = [{"value": a.value, "type": a.type} for a in params.addresses]

    website_addresses = None
    if params.website_addresses:
        website_addresses = [{"value": w.value, "type": w.type} for w in params.website_addresses]

    social_media_addresses = None
    if params.social_media_addresses:
        social_media_addresses = [{"value": s.value} for s in params.social_media_addresses]

    educations = None
    if params.educations:
        educations = [
            {
                "school_name": e.school_name,
                "degree": e.degree,
                "discipline": e.discipline,
                "start_date": e.start_date,
                "end_date": e.end_date,
            }
            for e in params.educations
        ]

    employments = None
    if params.employments:
        employments = [
            {
                "company_name": e.company_name,
                "title": e.title,
                "start_date": e.start_date,
                "end_date": e.end_date,
            }
            for e in params.employments
        ]

    try:
        return await provider.create_candidate(
            first_name=params.first_name,
            last_name=params.last_name,
            email_addresses=email_addresses,
            company=params.company,
            title=params.title,
            is_private=params.is_private,
            phone_numbers=phone_numbers,
            addresses=addresses,
            website_addresses=website_addresses,
            social_media_addresses=social_media_addresses,
            tags=params.tags,
            educations=educations,
            employments=employments,
            recruiter_id=params.recruiter_id,
            coordinator_id=params.coordinator_id,
            user_id=params.user_id,
        )
    except (UserNotFoundError, DuplicateEmailError) as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.CANDIDATE_ADD_NOTE.value)
async def greenhouse_candidates_add_note(
    params: AddCandidateNoteInput,
) -> CandidateNoteOutput:
    """Add a note to a candidate."""
    provider = CleanProvider()
    user = get_current_user()
    # Get persona from roles if available (first role is typically the persona)
    roles = user.get("roles", []) if user else []
    persona = roles[0] if roles else None

    try:
        note = await provider.add_candidate_note(
            candidate_id=params.candidate_id,
            data={"body": params.body, "visibility": params.visibility},
            user_id=params.user_id,
            persona=persona,
        )
    except CandidateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except UserNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    return CandidateNoteOutput(**note)


@require_scopes(Perm.CANDIDATE_UPDATE.value)
async def greenhouse_candidates_update(
    params: UpdateCandidateInput,
) -> CandidateOutput:
    """Update an existing candidate's top-level fields."""
    provider = CleanProvider()
    user = get_current_user()
    # Get persona from roles if available (first role is typically the persona)
    roles = user.get("roles", []) if user else []
    persona = roles[0] if roles else None

    update_payload = params.model_dump(exclude_none=True)
    candidate_id = update_payload.pop("candidate_id", None)

    try:
        return await provider.update_candidate(
            candidate_id=candidate_id,
            data=update_payload,
            persona=persona,
            user_id=None,
        )
    except CandidateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except (UserNotFoundError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@require_scopes(Perm.CANDIDATE_ADD_TAG.value)
async def greenhouse_candidates_add_tag(params: AddCandidateTagInput) -> AddCandidateTagOutput:
    """Add a tag to a candidate's profile."""
    provider = CleanProvider()
    user = get_current_user()
    # Get persona from roles if available (first role is typically the persona)
    roles = user.get("roles", []) if user else []
    persona = roles[0] if roles else None

    try:
        tag_info = await provider.add_candidate_tag(
            candidate_id=params.candidate_id,
            tag_name=params.tag,
            persona=persona,
            user_id=None,
        )
        from schemas import TagOutput

        return AddCandidateTagOutput(
            candidate_id=params.candidate_id,
            tag=TagOutput(id=tag_info["tag_id"], name=tag_info["tag_name"]),
        )
    except CandidateNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def register_candidate_tools(mcp: FastMCP) -> None:
    """Register the candidate tools with the MCP server."""
    mcp.tool()(greenhouse_candidates_get)
    mcp.tool()(greenhouse_candidates_create)
    mcp.tool()(greenhouse_candidates_add_note)
    mcp.tool()(greenhouse_candidates_update)
    mcp.tool()(greenhouse_candidates_search)
    mcp.tool()(greenhouse_candidates_add_tag)
