"""
ACE Criterion Verifier - LLM Prompts
Ported from ACE harness autograder.py
"""

# =============================================================================
# STAGE 1: RESPONSE TEXT CHECK PROMPT
# =============================================================================

RESPONSE_CHECK_PROMPT = """You are evaluating a response against a specific criterion.

Criterion to evaluate: {criterion_description}

Response Text:
{response_text}

**CANONICAL RECOMMENDATION LIST (use EXACTLY these) if applicable:**
{recommendation_names}

CRITICAL EVALUATION RULES:
1. **ZERO BACKGROUND KNOWLEDGE**: You must IGNORE any knowledge you have about these recommendations/products and only grade the response text based on the criterion. Pretend you have NEVER heard of them before.
2. **ONLY STATEMENTS ABOUT THE Recommendations/Products/Services/etc. COUNT**: A criterion is met for a recommendation/product ONLY if the response text DIRECTLY states the required information for that specific recommendation/product or if it is obviously inferrable.
3. **USE THE CANONICAL LIST**: You MUST evaluate EXACTLY the recommendations listed above if applicable.
   - Do NOT add recommendations that aren't in the list
   - Do NOT remove recommendations from the list
   - Do NOT rename or modify the recommendation names
   - If the list is empty, the response provided NO recommendations

   **WHAT COUNTS AS A RECOMMENDATION:**
   - A recommendation is a specific item the response suggests the user should consider/buy/use
   - Recommendations mentioned only as "examples of what NOT to buy" or "for comparison" are NOT recommendations
   - Recommendations mentioned only to explain why they don't work are NOT recommendations
   - A "meal plan" or "weekly plan" is ONE recommendation, not multiple separate meals
   - A "recipe" is ONE recommendation, not individual ingredients
   - A "DIY project" step by step guide is ONE recommendation, not individual steps or materials as different recommendations.

   If you believe the canonical list is WRONG (e.g., includes non-recommendations or misses actual recommendations),
   still evaluate the canonical list as given, but note your concern in the reasoning.

4. **ZERO BACKGROUND KNOWLEDGE**: You must COMPLETELY IGNORE any knowledge you have about these recommendations/products. Pretend you have NEVER heard of them before.

5. **ONLY EXPLICIT STATEMENTS COUNT**: A criterion is met for a recommendation ONLY if the response text DIRECTLY states the required information for that specific recommendation or if it is obviously inferrable.
Example:
- If the criterion says "supports 2+ players" → The response MUST explicitly say "multiplayer", "2 players", "co-op", "local multiplayer" or similar
- If the criterion says "compatible with X" → The response MUST explicitly mention compatibility with X
- If something is IMPLIED, HINTED AT, or requires INFERENCE → It does NOT count

When checking recommendation features, it might meet additional features. This is fine. For example:
Criterion: "Recommends only mixed martial arts (MMA) gloves"
Recommendation: "Gloves for MMA and Boxing"
Evaluation: meets_criterion = TRUE -- because the recommended gloves are advertised for MMA. Multi-purpose is OK.

6. **STRICT PER-PRODUCT CHECKING**: For each recommendation, look at ONLY the text that describes that specific recommendation/product. Do NOT use:
- Context from other recommendations
- General statements about the overall response
- Your knowledge about what these items typically are
- Assumptions based on names

7. **DETERMINE QUANTIFIER**:
- "at least one RECOMMENDATION/PRODUCT" or "one RECOMMENDATION/PRODUCT" → required_pass_count: 1, evaluation_type: per_product_any
- "at least 2 RECOMMENDATION/PRODUCTS" or "at least 3 RECOMMENDATION/PRODUCTS" → required_pass_count: 2 or 3, evaluation_type: per_product_any
- "ALL" or "only" or "each" → required_pass_count: -1, evaluation_type: per_product_all
- "Recommends [TYPE]" (e.g., "Recommends N64 games", "Recommends red recommendations/products", "Recommends shoes with...") → required_pass_count: -1, evaluation_type: per_product_all (implicit ALL)
- "Recommends a [FEATURE]" or "Recommends a [TYPE] with [FEATURE]" (e.g., "Recommends a product under $50", "Recommends a laptop with 16GB RAM") → required_pass_count: -1, evaluation_type: per_product_all (implicit ALL - every recommendation must have that feature)
- "Provides X items" or "recommends Y recommendations/products" (quantity check) → evaluation_type: holistic

**IMPORTANT:** "one or more of the following FEATURES" refers to features PER RECOMMENDATION/PRODUCT, not number of recommendations/products. This is still per_product_all.

**IMPORTANT:** "Recommends a [product/feature description]" means ALL recommendations must match that description. The "a" is singular grammar, NOT a quantifier. For example, "Recommends a budget laptop" means ALL recommended laptops must be budget laptops.

**DEFAULT:** If NO explicit quantifier like "at least" or "one of" is present, assume ALL must pass → required_pass_count: -1

SPECIAL INSTRUCTION FOR CONDITIONAL CRITERIA:
If this criterion starts with "If" (conditional), treat "available in X", "comes in X", "offered in X" as EQUIVALENT to "recommends X". Stock caveats like "confirm availability" or "check stock" do NOT make it fail.

SCORING:
- Criteria with "at least N": Pass if N or more recommendations/products meet it (e.g., if "at least 2" then Pass if 2+ recommendations/products meet)
- Criteria with "only" or "all": Pass only if EVERY recommendation/product meets it
- Criteria about quantity/count: Evaluate the overall response, not individual products

**CRITICAL - NO VACUOUS TRUTH**:
If the response contains NO recommendations/products, or fails to provide a recommendation then:
- Criteria that CHECK features of recommendations (e.g., "Recommends only X with feature Y") → FAIL (pass=false)
- Criteria that REQUIRE providing something (e.g., "Provides links", "States price") → FAIL (pass=false)
- The user asked for recommendations - providing NONE means criteria cannot be met
- Do NOT pass criteria vacuously just because "there are no violations if there are no products"
- Exception: Criteria explicitly about quantity (e.g., "Recommends at least one") are evaluated separately as holistic
- BE REASONABLE AND LOGICAL. If it should pass, pass, if it should fail, fail. Be Reasonable and logical.

EXAMPLE OF STRICT EVALUATION:
Criterion: "Recommends only games that support 2 or more players locally"
Recommendation/Product: "Mario Kart - A fun racing game where you compete for first place"
Evaluation: meets_criterion = FALSE (no mention of multiplayer, 2+ players, or local play)

Recommendation/Product: "Super Smash Bros - Battle with up to 4 players locally"
Evaluation: meets_criterion = TRUE (explicitly states "up to 4 players locally")

KEY EXAMPLES:

Feature Lists: "Recommends shoes with one or more of: non-compressible sole, raised heel, straps"
- Evaluation: per_product_all (ALL shoes must have at least ONE of the listed features)
- "Shoe 1: firm sole" → TRUE (has non-compressible sole)
- "Shoe 2: elevated heel" → TRUE (has raised heel)
- "Shoe 3: flat, soft sole" → FALSE (has none of the features)
- Result: FAIL (not all shoes have at least one feature)

Conditional: "If recommending item A, must have feature B"
- "Item A - available in feature B" → TRUE
- "Item A - while available in feature B, check stock" → TRUE (caveat ignored)
- "Item A" (no mention of B) → FALSE
- "Item C" (not item A) → TRUE (condition not met)

Only: "Recommends only MMA gloves"
- "MMA/Boxing gloves" → TRUE (is MMA, multi-purpose OK)
- "Boxing gloves" (no MMA) → FALSE

Strict: "Games with 4+ players"
- "Fun party game" → FALSE (no player count mentioned)

Return ONLY valid JSON:
{{
"pass": true/false,
"reasoning": "Clear explanation. For each recommendation, state whether the criterion is EXPLICITLY stated in its description.",
"evaluation_type": "holistic|per_product_all|per_product_any",
"required_pass_count": <number or -1>,
"recommendations_checked": [{{"recommendation_name": "EXACT name from canonical list", "meets_criterion": true/false}}],
"violation_found": true/false
}}

**IMPORTANT**: The "recommendations_checked" array MUST contain EXACTLY the items from the CANONICAL RECOMMENDATION LIST above.
Use the EXACT names provided. If the canonical list has 3 items, your array must have 3 items with those exact names.

SET required_pass_count based on the criterion wording:
- -1 = ALL recommendation/products must pass (for "only", "all", "each", OR when no specific number given)
- 1 = At least 1 recommendation/product must pass (for "one of", "at least one")
- 2 = At least 2 recommendation/products must pass (for "at least 2")
- 3 = At least 3 recommendation/products must pass (for "at least 3")
- etc.

Examples:
- "Recommends only red items" → required_pass_count: -1 (ALL)
- "At least 2 games under $50" → required_pass_count: 2
- "One of the options includes X" → required_pass_count: 1"""


# =============================================================================
# STAGE 2: SOURCE VERIFICATION PROMPT
# =============================================================================

SOURCE_CHECK_PROMPT = """Your job is to check that our grading of a model's response is correct, based on the source material.

We gave the model a prompt asking for recommendations. It then gave a response with suitable recommendations.
We graded whether the response meets the following criterion, and found that it passed: {criterion_description}
The recommendation, product, or shop that passed is called: {product_name}

We now want you to check whether the recommendation actually passes the criterion given the source material.
For instance, if the criterion is "The response recommends a veterinarian" and the recommendation, product, or shop in the response is "St Barnard's vets", you need to check the source material to confirm that "St Barnard's vets" actually is a veterinarian.
This is important for making sure that the model has not hallucinated in its response. We want to make sure that our grading of the model's responses is correct and the model is not making anything up.

Another example:
Criterion: "Recommends food themed games" and the recommendation, product, or shop returned in the response is "Overcooked! All you can eat", you need to check the source material to confirm that the "Overcooked! All you can eat" actually is a food themed game.

Another example:
Criterion: "Recommends badminton rackets under $25" and the recommendation, product, or shop returned in the response is "Puma Xr1234 Pro badminton racket", you need to check the source material to confirm that the Puma Xr1234 Pro badminton racket actually is a badminton racket and is under $25.

Another example:
Criterion: "Provides prices for each item" and the recommendation, product, or shop returned in the response is "XBox 360 controller", you need to check the source material to confirm that the XBox 360 controller actually has a price.

Source content:
{source_text}

The model response returned: {product_name}. The response passed this criterion: {criterion_description}. Based on ONLY the source content, should it have passed?

The recommendation, product, or shop PASSES if the criterion is true in AT LEAST ONE source (it does NOT need to be supported by all sources).

Return ONLY valid JSON:
{{
"pass": true/false,
"reason": "Brief explanation citing which source(s) confirmed or denied the criterion"
}}"""


# =============================================================================
# HOLISTIC SOURCE CHECK (No Products)
# =============================================================================

HOLISTIC_SOURCE_CHECK_PROMPT = """You are verifying if a criterion is supported by grounding sources.

Criterion to verify: {criterion_description}

Grounding Source Content:
{source_text}

IMPORTANT: Based ONLY on the source content above, is the criterion's claim supported or verifiable?
- If the sources contain information that supports or validates the criterion: pass = true
- If the sources do not mention or support the criterion: pass = false

Return ONLY valid JSON:
{{
"pass": true/false,
"reason": "Brief explanation citing which source(s) support or contradict the criterion"
}}"""


# =============================================================================
# NON-GROUNDED CRITERION PROMPT (Single Stage)
# =============================================================================

NON_GROUNDED_CHECK_PROMPT = """You are evaluating a response against a specific criterion.

Criterion to evaluate: {criterion_description}

Response Text:
{response_text}

SPECIAL INSTRUCTION FOR CONDITIONAL CRITERIA:
If this criterion starts with "If" (conditional), treat "available in X", "comes in X", "offered in X" as EQUIVALENT to "recommends X". Stock caveats like "confirm availability" or "check stock" do NOT make it fail.

CRITICAL EVALUATION RULES:

1. **ZERO BACKGROUND KNOWLEDGE**: You must COMPLETELY IGNORE any knowledge you have about these recommendation/products. Pretend you have NEVER heard of them before.

2. **ONLY STATEMENTS ABOUT THE RECOMMENDATION/PRODUCT COUNT**: A criterion is met for a recommendation/product ONLY if the response text DIRECTLY states the required information for that specific recommendation/product or if it is obviously inferrable.
Example:
- If the criterion says "supports 2+ players" → The response MUST explicitly say "multiplayer", "2 players", "co-op", "local multiplayer" or similar
- If the criterion says "compatible with X" → The response MUST explicitly mention compatibility with X
- If something is IMPLIED, HINTED AT, or requires INFERENCE → It does NOT count

Multi-purpose recommendation/products are fine:
Criterion: "Recommends only mixed martial arts (MMA) gloves"
Recommendation/Product: "Gloves for MMA and Boxing"
Evaluation: meets_criterion = TRUE (advertised for MMA, multi-sport OK)

3. **DETERMINE SCOPE**:
- HOLISTIC: Applies to the overall response (e.g., "recommends exactly 3 items")
- PER_RECOMMENDATION: Applies to each individual recommendation (e.g., "all items must be X")

SCORING:
- Criteria with "only" or "all": Pass only if EVERY recommendation answer or product meets it
- Criteria about quantity/count: Evaluate the overall response

KEY EXAMPLES:

Conditional: "If recommending item A, must have feature B"
- "Item A - available in feature B" → TRUE
- "Item A - while available in feature B, check stock" → TRUE
- "Item A" (no mention of B) → FALSE
- "Item C" (not item A) → TRUE (condition not met)

Only: "Recommends only MMA gloves"
- "MMA/Boxing gloves" → TRUE
- "Boxing gloves" (no MMA) → FALSE

Overall be intuitive and logical, if it should pass, pass, if it should fail, fail. Be Reasonable and logical.

Return ONLY valid JSON:
{{
"pass": true/false,
"reasoning": "Clear explanation of why pass or fail.",
"evaluation_type": "holistic|per_recommendation",
"violation_found": true/false
}}"""


# =============================================================================
# LINK EXTRACTION PROMPT
# =============================================================================

LINK_EXTRACTION_PROMPT = """Extract ALL URLs mentioned for product: {product_name}

Response text:
{response_text}

Find URLs (http://, https://) mentioned in context of {product_name} specifically.

Return ONLY valid JSON:
{{
"urls": ["url1", "url2", ...]
}}"""
