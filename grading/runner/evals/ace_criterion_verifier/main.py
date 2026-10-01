"""
ACE Criterion Verifier
Evaluates criteria against:
1. Stage 1: Response text (does the response claim to meet the criterion?)
2. Stage 2: Grounded sources (is the claim verified in scraped content?)
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from firecrawl import FirecrawlApp
from litellm.types.utils import Choices
from loguru import logger
from pydantic import BaseModel

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm
from runner.utils.settings import get_settings

from .prompts import (
    HOLISTIC_SOURCE_CHECK_PROMPT,
    LINK_EXTRACTION_PROMPT,
    NON_GROUNDED_CHECK_PROMPT,
    RESPONSE_CHECK_PROMPT,
    SOURCE_CHECK_PROMPT,
)

LLM_JUDGE_TIMEOUT = 600


MAX_JSON_RETRIES = 3


class ResponseCheckResult(BaseModel):
    """Result from Stage 1 response text check."""

    all_pass: bool
    reasoning: str
    evaluation_type: str  # holistic, per_product_all, per_product_any
    required_pass_count: int  # -1 = all, 1+ = at least N
    recommendations_checked: list[dict[str, Any]]
    violation_found: bool


class SourceCheckResult(BaseModel):
    """Result from Stage 2 source verification."""

    all_pass: bool
    reasoning: str
    product_results: list[dict[str, Any]]
    unverifiable: bool = (
        False  # True when source content was unavailable (not a hallucination)
    )


def _extract_json_from_response(text: str) -> dict[str, Any] | None:
    """Extract JSON from LLM response, handling markdown code blocks."""
    if not text:
        return None

    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    if "```json" in text:
        match = re.search(r"```json\s*([\s\S]*?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

    if "```" in text:
        match = re.search(r"```\s*([\s\S]*?)\s*```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


async def _call_llm_for_json(
    model: str,
    messages: list[dict[str, Any]],
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> dict[str, Any]:
    """Call LLM and extract JSON response with retries."""
    for attempt in range(MAX_JSON_RETRIES):
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            logger.warning(
                f"[ACE] task={task_id} | empty response on attempt {attempt + 1}"
            )
            continue

        content = choices[0].message.content
        if not content:
            continue

        parsed = _extract_json_from_response(content)
        if parsed:
            return parsed

        logger.warning(
            f"[ACE] task={task_id} | JSON parse failed on attempt {attempt + 1}"
        )

    raise ValueError(f"Failed to get valid JSON after {MAX_JSON_RETRIES} attempts")


def _get_domain_and_mode(ace_grounding: dict[str, Any]) -> tuple[str, str]:
    """Best-effort extraction of domain and shop/product mode."""
    domain = (
        ace_grounding.get("domain")
        or ace_grounding.get("metadata", {}).get("domain")
        or "Shopping"
    )
    shop_vs_product = (
        ace_grounding.get("shop_vs_product")
        or ace_grounding.get("metadata", {}).get("shop_vs_product")
        or "Product"
    )
    return domain, shop_vs_product


async def _scrape_with_firecrawl(
    url: str, api_key: str, timeout: int = 60
) -> dict[str, Any]:
    """Scrape URL using Firecrawl (to-thread to avoid blocking the event loop)."""
    if FirecrawlApp is None:
        return {
            "success": False,
            "markdown": "",
            "title": "",
            "url": url,
            "error": "Firecrawl not installed",
        }

    try:
        app = FirecrawlApp(api_key=api_key)
        result = await asyncio.wait_for(
            asyncio.to_thread(
                app.scrape,
                url,
                formats=["markdown"],
                proxy="auto",
                only_main_content=False,
            ),
            timeout=timeout,
        )

        markdown = result.markdown if hasattr(result, "markdown") else ""
        title = ""
        final_url = url

        if hasattr(result, "metadata") and result.metadata is not None:
            if hasattr(result.metadata, "title") and result.metadata.title is not None:
                title = result.metadata.title
            if hasattr(result.metadata, "url") and result.metadata.url is not None:
                final_url = result.metadata.url

        if not markdown or len(markdown.strip()) < 50:
            return {
                "success": False,
                "markdown": markdown,
                "title": title,
                "url": final_url,
                "error": "Empty or minimal content returned",
            }

        return {
            "success": True,
            "markdown": markdown,
            "title": title,
            "url": final_url,
            "error": None,
        }
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, "response"):
            try:
                response = getattr(e, "response", None)
                if response is not None and hasattr(response, "text"):
                    error_msg = f"{e}: {response.text[:200]}"
            except Exception:
                pass
        return {
            "success": False,
            "markdown": "",
            "title": "",
            "url": url,
            "error": error_msg,
        }


async def extract_links_for_product(
    product_name: str,
    response_text: str,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> list[str]:
    """Extract URLs from response text for a specific product using LLM."""
    prompt = LINK_EXTRACTION_PROMPT.format(
        product_name=product_name, response_text=response_text
    )
    messages = build_messages(system_prompt="", user_prompt=prompt)

    try:
        data = await _call_llm_for_json(model, messages, extra_args, task_id)
        urls = data.get("urls", [])
        return urls if isinstance(urls, list) else []
    except Exception as e:
        logger.warning(
            f"[ACE] task={task_id} | Failed to extract links for {product_name}: {e}"
        )
        return []


async def _verify_shopping_link(
    product_name: str,
    url: str,
    page_markdown: str,
    verification_mode: str,
    criterion_description: str,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> dict[str, Any]:
    """LLM-based classification of shopping link content (product vs shop page)."""
    mode_rules = (
        f"""VALID product purchasing page:
- Direct product page for {product_name}
- Has "Add to Cart", "Buy Now", or similar purchase button
- User can directly purchase THIS specific product

INVALID pages:
- Product list or search results (even if they link to purchase pages)
- Review/comparison pages (without direct purchase)
- General shop homepage
- Category pages with multiple products"""
        if verification_mode == "Product"
        else f"""VALID store/vendor page:
- Direct link to the vendor's store or product category page
- Shows products from {product_name} (the vendor/brand)
- User can browse and potentially purchase from this vendor
- Includes vendor branding, product listings, or shop page

INVALID pages:
- Unrelated vendor or store
- Generic marketplace search results (unless {product_name} is the seller)
- Review/comparison pages without vendor link
- Dead links or error pages"""
    )

    prompt = f"""You are classifying a webpage to determine if it's a valid {"product purchasing" if verification_mode == "Product" else "store/vendor"} page.

Product/Vendor name: {product_name}
Criterion (context): {criterion_description}

Page content:
{page_markdown}

Classification guidelines:
{mode_rules}

Return ONLY valid JSON:
{{
  "is_purchase_page": true/false,
  "page_type": "product_page|vendor_page|list|search_results|review|general",
  "reason": "Brief explanation why valid or invalid"
}}"""

    messages = build_messages(system_prompt="", user_prompt=prompt)
    data = await _call_llm_for_json(model, messages, extra_args, task_id)

    return {
        "is_valid": data.get("is_purchase_page", False),
        "reason": data.get("reason", ""),
        "page_type": data.get("page_type", "unknown"),
    }


async def _verify_gaming_link(
    recommendation_name: str,
    url: str,
    page_markdown: str,
    criterion_description: str,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> dict[str, Any]:
    """LLM-based verification for gaming link requirements."""
    prompt = f"""You are verifying if a webpage meets a link requirement for a Gaming criterion.

Game/Item: {recommendation_name}
URL: {url}
Criterion: {criterion_description}

Page content:
{page_markdown}

Verify both:
1. URL domain matches the requirement
2. Page content is relevant to the game/item

Return ONLY valid JSON:
{{
  "is_valid": true/false,
  "page_type": "official|store|forum|video|wiki|third_party|other",
  "reason": "Brief explanation why it does or doesn't meet the criterion requirement. Mention the domain and content type."
}}"""

    messages = build_messages(system_prompt="", user_prompt=prompt)
    data = await _call_llm_for_json(model, messages, extra_args, task_id)

    return {
        "is_valid": data.get("is_valid", False),
        "reason": data.get("reason", ""),
        "page_type": data.get("page_type", "unknown"),
    }


async def verify_link_page(
    product_name: str,
    url: str,
    criterion_description: str,
    domain: str,
    shop_vs_product: str,
    ace_firecrawl_api_key: str,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> dict[str, Any]:
    """Scrape a URL then verify it against the link criterion."""
    scraped = await _scrape_with_firecrawl(url, ace_firecrawl_api_key)
    if not scraped.get("success"):
        return {
            "is_valid": False,
            "page_type": "error",
            "reason": scraped.get("error") or "Page failed to load or has no content",
        }

    markdown = scraped.get("markdown", "")

    if domain.lower() == "gaming":
        return await _verify_gaming_link(
            recommendation_name=product_name,
            url=url,
            page_markdown=markdown,
            criterion_description=criterion_description,
            model=model,
            extra_args=extra_args,
            task_id=task_id,
        )

    return await _verify_shopping_link(
        product_name=product_name,
        url=url,
        page_markdown=markdown,
        verification_mode=shop_vs_product,
        criterion_description=criterion_description,
        model=model,
        extra_args=extra_args,
        task_id=task_id,
    )


async def grade_link_criterion(
    criterion: dict[str, Any],
    response_text: str,
    product_map: list[dict[str, Any]],
    domain: str,
    shop_vs_product: str,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> dict[str, Any]:
    """
    Grade "Provides link(s)" criterion with link verification.

    Stage 1: Check if links are provided in response
    Stage 2: Verify links meet requirement
    """
    logger.info(f"[ACE] task={task_id} | Link criterion ({domain} / {shop_vs_product})")

    settings = get_settings()
    ace_firecrawl_api_key = getattr(settings, "ACE_FIRECRAWL_API_KEY", None) or ""
    if not ace_firecrawl_api_key:
        reason = "Missing ACE_FIRECRAWL_API_KEY - cannot verify links"
        logger.error(f"[ACE] task={task_id} | {reason}")
        return {
            "score": 0,
            "stage_reached": "response_text",
            "stage_1_result": {
                "all_pass": False,
                "reasoning": reason,
                "product_results": [],
            },
            "stage_2_result": None,
            "reason": reason,
        }

    if not product_map:
        logger.error(
            f"[ACE] task={task_id} | No recommendations identified - cannot evaluate link criterion"
        )
        return {
            "score": 0,
            "stage_reached": "response_text",
            "stage_1_result": {
                "all_pass": False,
                "reasoning": "No recommendations identified in response - cannot evaluate links",
                "product_results": [],
            },
            "stage_2_result": None,
            "reason": "Failed: No recommendations identified to check for links",
        }

    product_link_results = []
    all_have_links = True

    for product in product_map:
        product_name = product.get("product_name", "Unknown")
        links = await extract_links_for_product(
            product_name=product_name,
            response_text=response_text,
            model=model,
            extra_args=extra_args,
            task_id=task_id,
        )

        product_link_results.append(
            {
                "product_name": product_name,
                "links_found": links,
                "has_links": len(links) > 0,
            }
        )

        if len(links) == 0:
            all_have_links = False
            logger.warning(f"[ACE] task={task_id} | No links found for {product_name}")
        else:
            logger.info(
                f"[ACE] task={task_id} | Found {len(links)} link(s) for {product_name}"
            )

    if not all_have_links:
        return {
            "score": 0,
            "stage_reached": "response_text",
            "stage_1_result": {
                "all_pass": False,
                "reasoning": "One or more products missing purchase links",
                "product_results": product_link_results,
            },
            "stage_2_result": None,
            "reason": "Failed: Not all products have purchase links in response",
        }

    logger.info(f"[ACE] task={task_id} | Stage 2: Verifying links via Firecrawl")
    link_verification_results = []
    all_products_pass = True

    for product_data in product_link_results:
        product_name = product_data["product_name"]
        links = product_data["links_found"]

        product_passes = False
        link_checks = []

        for url in links:
            logger.info(f"[ACE] task={task_id} | Verifying {url} for {product_name}")
            verification = await verify_link_page(
                product_name=product_name,
                url=url,
                criterion_description=criterion.get("description", ""),
                domain=domain,
                shop_vs_product=shop_vs_product,
                ace_firecrawl_api_key=ace_firecrawl_api_key,
                model=model,
                extra_args=extra_args,
                task_id=task_id,
            )

            link_checks.append(
                {
                    "url": url,
                    "is_valid": verification.get("is_valid", False),
                    "page_type": verification.get("page_type", "unknown"),
                    "reason": verification.get("reason", ""),
                }
            )

            if verification.get("is_valid"):
                product_passes = True
                break  # one valid link is enough

        link_verification_results.append(
            {
                "product_name": product_name,
                "pass": product_passes,
                "links_checked": link_checks,
            }
        )

        if not product_passes:
            all_products_pass = False

    # Per the ACE paper, failing to provide a valid link is a response failure (0),
    # not a hallucination (-1). -1 is reserved for claims that are contradicted by sources.
    score = 1 if all_products_pass else 0

    return {
        "score": score,
        "stage_reached": "link_verification",
        "stage_1_result": {
            "all_pass": True,
            "reasoning": "All products have links",
            "product_results": product_link_results,
        },
        "stage_2_result": {
            "all_pass": all_products_pass,
            "reasoning": f"Link verification: {'All valid' if all_products_pass else 'One or more invalid'}",
            "product_results": link_verification_results,
        },
        "reason": f"Links provided and {'all verified' if all_products_pass else 'verification failed'}",
    }


async def check_response_text(
    criterion_description: str,
    response_text: str,
    products: list[dict[str, Any]],
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> ResponseCheckResult:
    """
    Stage 1: Check if products meet criterion based on response text.

    Returns ResponseCheckResult with pass/fail and per-product results.
    """
    logger.info(f"[ACE] task={task_id} | Stage 1: Checking response text")

    recommendation_names = [p.get("product_name", "") for p in products]
    names_str = (
        ", ".join(recommendation_names)
        if recommendation_names
        else "(No recommendations identified)"
    )

    prompt = RESPONSE_CHECK_PROMPT.format(
        criterion_description=criterion_description,
        response_text=response_text,
        recommendation_names=names_str,
    )

    messages = build_messages(system_prompt="", user_prompt=prompt)

    try:
        data = await _call_llm_for_json(model, messages, extra_args, task_id)

        result = ResponseCheckResult(
            all_pass=data.get("pass", False),
            reasoning=data.get("reasoning", ""),
            evaluation_type=data.get("evaluation_type", "per_product_all"),
            required_pass_count=data.get("required_pass_count", -1),
            recommendations_checked=data.get("recommendations_checked", []),
            violation_found=data.get("violation_found", False),
        )

        status = "PASS" if result.all_pass else "FAIL"
        logger.info(f"[ACE] task={task_id} | Stage 1: {status}")

        return result

    except Exception as e:
        logger.error(f"[ACE] task={task_id} | Stage 1 error: {e}")
        return ResponseCheckResult(
            all_pass=False,
            reasoning=f"Error during check: {e}",
            evaluation_type="error",
            required_pass_count=-1,
            recommendations_checked=[],
            violation_found=False,
        )


async def check_grounded_sources(
    criterion_description: str,
    product_map: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    evaluation_type: str,
    required_count: int,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> SourceCheckResult:
    """
    Stage 2: Verify claims against scraped source content.

    A product PASSES if criterion is true in AT LEAST ONE of its sources.
    """
    logger.info(f"[ACE] task={task_id} | Stage 2: Checking grounded sources")

    product_results: list[dict[str, Any]] = []

    if not product_map:
        logger.info(f"[ACE] task={task_id} | No product map - holistic grounding check")

        source_contents = []
        for source in sources:
            content = source.get("webpage_content", {})
            text = content.get("text") or content.get("markdown", "")
            if text:
                source_contents.append(
                    f"Source: {source.get('source_title', 'Unknown')}\n{text}"
                )

        if not source_contents:
            # No source content means we cannot verify or refute the claim.
            # Per the ACE paper, -1 requires a source that contradicts the claim.
            # Signal unverifiable so the verifier returns score=0, not -1.
            return SourceCheckResult(
                all_pass=False,
                unverifiable=True,
                reasoning="No grounding sources available - cannot verify, returning 0 (not hallucination)",
                product_results=[],
            )

        source_text = "\n---\n".join(source_contents)

        prompt = HOLISTIC_SOURCE_CHECK_PROMPT.format(
            criterion_description=criterion_description,
            source_text=source_text[:100000],  # Limit size
        )

        messages = build_messages(system_prompt="", user_prompt=prompt)

        try:
            data = await _call_llm_for_json(model, messages, extra_args, task_id)
            all_pass = data.get("pass", False)
            reasoning = data.get("reason", "")

            status = "PASS" if all_pass else "FAIL"
            logger.info(f"[ACE] task={task_id} | Stage 2 holistic: {status}")

            return SourceCheckResult(
                all_pass=all_pass,
                reasoning=f"Holistic grounding verification: {reasoning}",
                product_results=[],
            )
        except Exception as e:
            logger.error(f"[ACE] task={task_id} | Stage 2 holistic error: {e}")
            return SourceCheckResult(
                all_pass=False,
                unverifiable=True,
                reasoning=f"Error during holistic grounding check: {e}",
                product_results=[],
            )

    for product in product_map:
        product_name = product.get("product_name", "Unknown")
        source_indices = product.get("source_indices", [])

        logger.debug(
            f"[ACE] task={task_id} | Checking {product_name} (sources: {source_indices})"
        )

        source_contents = []
        for idx in source_indices:
            source = next(
                (s for s in sources if s.get("source_number") == idx + 1),
                None,
            )
            if source:
                content = source.get("webpage_content", {})
                text = content.get("text") or content.get("markdown", "")
                if text:
                    source_contents.append(
                        {
                            "title": source.get("source_title", "Unknown"),
                            "content": text,
                        }
                    )

        if not source_contents:
            # No source content means we cannot verify or refute the claim for this product.
            # Skip it from the aggregation — only verified products determine pass/fail.
            logger.debug(
                f"[ACE] task={task_id} | {product_name}: no source content, skipping from aggregation"
            )
            continue

        source_text = "\n---\n".join(
            [f"Source: {s['title']}\n{s['content']}" for s in source_contents]
        )

        prompt = SOURCE_CHECK_PROMPT.format(
            criterion_description=criterion_description,
            product_name=product_name,
            source_text=source_text[:100000],  # Limit size
        )

        messages = build_messages(system_prompt="", user_prompt=prompt)

        try:
            data = await _call_llm_for_json(model, messages, extra_args, task_id)

            product_results.append(
                {
                    "product_name": product_name,
                    "pass": data.get("pass", False),
                    "reason": data.get("reason", ""),
                    "sources_checked": [s["title"] for s in source_contents],
                }
            )

            status = "PASS" if data.get("pass") else "FAIL"
            logger.debug(f"[ACE] task={task_id} | {product_name}: {status}")

        except Exception as e:
            logger.error(f"[ACE] task={task_id} | Error checking {product_name}: {e}")
            logger.debug(
                f"[ACE] task={task_id} | {product_name}: LLM error, skipping from aggregation"
            )
            continue

    # If all products were skipped (no source content for any), signal unverifiable
    # so the verifier returns score=0 instead of -1.
    if not product_results:
        return SourceCheckResult(
            all_pass=False,
            unverifiable=True,
            reasoning="No scraped content available for any product - cannot verify, returning 0 (not hallucination)",
            product_results=[],
        )

    pass_count = sum(1 for p in product_results if p["pass"])
    total_count = len(product_results)
    skipped_count = len(product_map) - total_count

    if evaluation_type == "per_product_any":
        effective_required = total_count if required_count == -1 else required_count
        all_pass = pass_count >= effective_required
        # If threshold not met but skipped products (no source content) could have closed
        # the gap, the outcome is indeterminate — not a hallucination.
        if (
            not all_pass
            and skipped_count > 0
            and (pass_count + skipped_count) >= effective_required
        ):
            reasoning = (
                f"Source verification inconclusive ({pass_count}/{total_count} verified passed, "
                f"{skipped_count} unverifiable — cannot confirm threshold of {effective_required})"
            )
            logger.info(f"[ACE] task={task_id} | Stage 2: UNVERIFIABLE - {reasoning}")
            return SourceCheckResult(
                all_pass=False,
                unverifiable=True,
                reasoning=reasoning,
                product_results=product_results,
            )
        reasoning = f"Source verification ({pass_count}/{total_count} passed)"
    elif evaluation_type == "holistic":
        all_pass = all(p["pass"] for p in product_results) if product_results else True
        reasoning = f"Source verification (holistic {pass_count}/{total_count} passed)"
    else:  # per_product_all or default
        all_pass = pass_count == total_count if product_results else False
        reasoning = f"Source verification ({pass_count}/{total_count} passed)"

    status = "PASS" if all_pass else "FAIL"
    logger.info(f"[ACE] task={task_id} | Stage 2: {status} - {reasoning}")

    return SourceCheckResult(
        all_pass=all_pass,
        reasoning=reasoning,
        product_results=product_results,
    )


async def check_non_grounded_criterion(
    criterion_description: str,
    response_text: str,
    model: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
) -> tuple[bool, str]:
    """
    Single-stage check for non-grounded criteria.
    Returns (pass, reasoning).
    """
    logger.info(f"[ACE] task={task_id} | Non-grounded criterion check")

    prompt = NON_GROUNDED_CHECK_PROMPT.format(
        criterion_description=criterion_description,
        response_text=response_text,
    )

    messages = build_messages(system_prompt="", user_prompt=prompt)

    try:
        data = await _call_llm_for_json(model, messages, extra_args, task_id)

        all_pass = data.get("pass", False)
        reasoning = data.get("reasoning", "")

        status = "PASS" if all_pass else "FAIL"
        logger.info(f"[ACE] task={task_id} | Non-grounded: {status}")

        return all_pass, reasoning

    except Exception as e:
        logger.error(f"[ACE] task={task_id} | Non-grounded error: {e}")
        return False, f"Error during check: {e}"


async def ace_criterion_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """
    ACE Criterion Verifier - Two-stage verification for grounded recommendations.

    Input (verifier_values):
        - description: Criterion description to evaluate
        - type: Criterion type (feature, pricing, link, etc.)
        - grounded_status: "Grounded" or "Not Grounded"
        - hurdle_tag: "Hurdle" or "Not"

    The ACE grounding data is expected in trajectory.output["ace_grounding"]:
        - response_text: LLM response text
        - product_source_map: List of products with source mappings
        - sources: Scraped source content

    Returns:
        VerifierResult with score:
        - 1: Stage 1 pass + Stage 2 pass (verified)
        - 0: Stage 1 fail (response doesn't meet criterion)
        - -1: Stage 1 pass + Stage 2 fail (hallucination)
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    criterion_description = verifier_values.get("description", "")
    criterion_type = verifier_values.get("type", "standard")
    grounded_status = verifier_values.get("grounded_status", "Grounded")
    hurdle_tag = verifier_values.get("hurdle_tag", "Not")

    if not criterion_description:
        raise ValueError("Missing required field: description")

    logger.info(
        f"[ACE] task={task_id} | Evaluating: {criterion_description[:80]}... "
        f"(type={criterion_type}, grounded={grounded_status})"
    )

    ace_grounding = {}
    if input.trajectory and input.trajectory.output:
        ace_grounding = input.trajectory.output.get("ace_grounding", {})

    if not ace_grounding:
        logger.warning(
            f"[ACE] task={task_id} | No ace_grounding data found in trajectory"
        )
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "score": 0,
                "stage_reached": "none",
                "reason": "No ACE grounding data found in trajectory output",
            },
            status=VerifierResultStatus.ERROR,
            message="Missing ace_grounding data",
        )

    response_text = ace_grounding.get("response_text", "")
    product_map = ace_grounding.get("product_source_map", [])
    sources = ace_grounding.get("sources", [])

    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args

    try:
        domain, shop_vs_product = _get_domain_and_mode(ace_grounding)
        criterion_id = verifier_values.get("criterion_id") or verifier_values.get("id")

        if criterion_type == "Provides link(s)":
            link_result = await grade_link_criterion(
                criterion=verifier_values,
                response_text=response_text,
                product_map=product_map,
                domain=domain,
                shop_vs_product=shop_vs_product,
                model=model,
                extra_args=extra_args,
                task_id=task_id,
            )

            score = int(link_result["score"])

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=float(score),
                verifier_result_values={
                    "score": score,
                    "criterion_id": criterion_id,
                    "description": criterion_description,
                    "stage_reached": link_result.get("stage_reached"),
                    "criterion_type": criterion_type,
                    "hurdle_tag": hurdle_tag,
                    "stage_1_result": link_result.get("stage_1_result"),
                    "stage_2_result": link_result.get("stage_2_result"),
                    "reason": link_result.get("reason"),
                },
            )

        if grounded_status == "Not Grounded":
            all_pass, reasoning = await check_non_grounded_criterion(
                criterion_description=criterion_description,
                response_text=response_text,
                model=model,
                extra_args=extra_args,
                task_id=task_id,
            )

            score = 1 if all_pass else 0

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=float(score),
                verifier_result_values={
                    "score": score,
                    "criterion_id": criterion_id,
                    "description": criterion_description,
                    "stage_reached": "response_text_only",
                    "criterion_type": criterion_type,
                    "hurdle_tag": hurdle_tag,
                    "stage_1_result": {
                        "all_pass": all_pass,
                        "reasoning": reasoning,
                    },
                    "stage_2_result": None,
                    "reason": reasoning,
                },
            )

        # Two-stage evaluation for grounded criteria
        stage_1_result = await check_response_text(
            criterion_description=criterion_description,
            response_text=response_text,
            products=product_map,
            model=model,
            extra_args=extra_args,
            task_id=task_id,
        )

        if not stage_1_result.all_pass:
            logger.info(f"[ACE] task={task_id} | FAILED at Stage 1 (Score: 0)")

            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "score": 0,
                    "criterion_id": criterion_id,
                    "description": criterion_description,
                    "stage_reached": "response_text",
                    "criterion_type": criterion_type,
                    "hurdle_tag": hurdle_tag,
                    "stage_1_result": stage_1_result.model_dump(),
                    "stage_2_result": None,
                    "reason": f"Failed response text check: {stage_1_result.reasoning}",
                },
            )

        stage_2_result = await check_grounded_sources(
            criterion_description=criterion_description,
            product_map=product_map,
            sources=sources,
            evaluation_type=stage_1_result.evaluation_type,
            required_count=stage_1_result.required_pass_count,
            model=model,
            extra_args=extra_args,
            task_id=task_id,
        )

        if stage_2_result.all_pass:
            score = 1
            logger.info(f"[ACE] task={task_id} | PASSED both stages (Score: 1)")
        elif stage_2_result.unverifiable:
            score = 0
            logger.info(
                f"[ACE] task={task_id} | FAILED at Stage 2 - unverifiable (Score: 0)"
            )
        else:
            score = -1
            logger.info(
                f"[ACE] task={task_id} | FAILED at Stage 2 - hallucination (Score: -1)"
            )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=float(score),
            verifier_result_values={
                "score": score,
                "criterion_id": criterion_id,
                "description": criterion_description,
                "stage_reached": "grounded_sources",
                "criterion_type": criterion_type,
                "hurdle_tag": hurdle_tag,
                "stage_1_result": stage_1_result.model_dump(),
                "stage_2_result": stage_2_result.model_dump(),
                "reason": f"Stage 1: {stage_1_result.reasoning} | Stage 2: {stage_2_result.reasoning}",
            },
        )

    except Exception as e:
        error_msg = f"ACE criterion verification failed: {e}"
        logger.error(f"[ACE] task={task_id} | error: {error_msg}")
        raise ValueError(error_msg) from e
