Perform open-ended aspect-based opinion extraction for the product review provided as input.

Identify every valid observation, experience, effect, or evaluation that the reviewer expresses about the reviewed product or the experience of using it. Return each independently assessable statement as one opinion unit.

Each opinion unit has exactly these five fields:

1. `raw_aspect`: the product attribute, component, behavior, consequence, or usage situation being evaluated;
2. `raw_status`: the state, condition, manifestation, or value of that aspect, or `null` only when no grounded state can be named;
3. `excerpt`: an exact contiguous substring copied from the review;
4. `opinion`: a concise statement preserving the reviewer's complete observation or evaluation;
5. `sentiment`: the reviewer's evaluation direction for this specific aspect and status.

Create `raw_aspect` and `raw_status` dynamically. Do not restrict them to a predefined taxonomy.

## Aspect and status

`raw_aspect` identifies what is evaluated. `raw_status` identifies the state it is in.

Use concise, reusable noun phrases. Keep state, polarity, degree, comparison, time, cause, and final judgment out of `raw_aspect`.

Examples:

- use `raw_aspect: 화면 밝기`, `raw_status: 높음`, not `raw_aspect: 너무 밝은 화면`;
- use `raw_aspect: 빛샘`, `raw_status: 적음`, not `raw_aspect: 적은 빛샘`;
- use `raw_aspect: 잔상`, `raw_status: 발생`, not `raw_aspect: 잦은 잔상`;
- use `raw_aspect: 장시간 시청`, `raw_status: 편안함`, not `raw_aspect: 편안한 장시간 시청`.
- use `raw_aspect: 화면 크기`, `raw_status: 27인치`, not `raw_aspect: 27인치 화면 크기`.

Prefer direct labels such as `화면 밝기`, `빛샘`, `잔상`, `화면 선명도`, and `장시간 시청`. Avoid unnecessarily broad labels such as `품질`, `성능`, `특성`, or `사용 경험` when the review supports a concrete target.

Use `전반적 상품 경험` only for an explicit whole-product evaluation that cannot be assigned to a specific attribute. It is not a fallback for a difficult-to-name aspect.

`raw_status` should capture the core state without absorbing details that belong in `opinion`. Normally keep degree, comparison target, usage context, time, cause, and consequences in `opinion`.

Keep the core state compact: use `선명함`, not `매우 선명함`; preserve `매우` in `opinion`. Preserve an explicitly reported value such as `27인치` in `raw_status`, and its suitability for dual-monitor use in `opinion`.

For example, from `이전 모니터보다 무게가 조금 무겁지만 저는 묵직해서 마음에 듭니다.`, use:

- `raw_aspect`: `무게`;
- `raw_status`: `무거움`;
- `opinion`: `이전 모니터보다 무게가 조금 무겁지만 묵직한 느낌을 선호함`.

Do not replace a concrete status with `긍정`, `부정`, `좋음`, or `나쁨`. A state is not itself a sentiment: `무거움`, `가벼움`, `발생`, and `없음` may be liked, disliked, or neutrally observed depending on the review.

An evaluation alone is not a product state. If the review says only `화질에 매우 만족합니다.`, use `raw_aspect: 화질`, `raw_status: null`, `opinion: 화질에 매우 만족함`, and `sentiment: positive`. Do not invent a state such as `선명함`, and do not use `만족`, `불만족`, `최고`, or `훌륭함` as a substitute for a missing product state. The same applies to `영상 보기에 좋아요`: if no distinct viewing state is expressed, its status is `null`, not `좋음`.

Apply this to every aspect, including value for money. `가성비가 마음에 들어요` gives a positive evaluation but no specific state: use `raw_aspect: 가성비`, `raw_status: null`. Do not infer `높음` from praise alone. A wish for a better stand shape should retain the wish in `opinion`; do not present the desired shape as an observed current state.

Preserve uncertainty in the status when removing it would be misleading. For example, use `적합 가능성`, `불확실`, or `판단 보류` when those meanings are expressed.

## Sentiment

`sentiment` must be exactly one of these five labels and must describe the reviewer's direction toward this opinion unit, not a generic prior about the state:

- `positive`: the reviewer clearly prefers or is satisfied with the aspect or state;
- `negative`: the reviewer is clearly dissatisfied with the aspect or state;
- `mixed`: advantages and disadvantages of the same aspect are explicitly weighed together in one inseparable evaluation;
- `neutral`: the reviewer states a fact or experience without evaluating it;
- `unknown`: the evidence is insufficient or too uncertain to determine an evaluation direction.

Important distinctions:

- `무게가 무겁지만 묵직해서 마음에 든다` is `positive`, not automatically `negative`.
- `무게가 무거워서 옮기기 불편하다` is `negative`.
- `무게는 직접 재보니 4kg이다` is `neutral` when no preference is expressed.
- `받침대 내구성은 아직 잘 모르겠다` is `unknown`, not `neutral`.
- Use `mixed` only when the review explicitly presents a trade-off for the same aspect and a single combined evaluation best preserves that trade-off.
- If positive and negative statements concern different aspects, create separate units and label each one independently rather than using `mixed`.
- A factual observation does not inherit praise for an adjacent aspect. In `다양한 기능이 있는데 가격이 저렴해서 만족합니다.`, feature variety is `neutral` unless separately praised; price is `positive`.
- Tentative wording can still have a clear direction. For example, `게임용으로 괜찮을 것 같다` is generally `positive` while preserving the uncertainty in `raw_status` and `opinion`.

## Attribution and grounding

Extract only the reviewer's own experience, direct observation, or explicit evaluation of the reviewed product.

Do not treat another person's opinion, a seller or manufacturer claim, marketing text, a category stereotype, or an evaluation applying only to a comparison product as the reviewer's observation.

When the reviewed product is compared with another product, describe only the reviewed product and preserve the comparison target in `opinion`.

The product name and product category are context only. Use them to understand terminology and references, but never treat information appearing only in metadata as review evidence.

Do not infer unmentioned technical causes, features, states, or evaluations.

## Opinion-unit boundaries

Create separate units for different aspects. A sentence may produce several units, and those units may share the same excerpt when contrast, comparison, negation, causality, usage conditions, or time must be preserved.

For the same aspect:

- create separate units when independently useful states, usage contexts, or time periods are evaluated separately;
- combine them only when the reviewer weighs them as one inseparable trade-off, using `mixed`;
- do not duplicate repeated statements expressing the same observation;
- use consistent aspect and status labels for the same concepts within a review.

An experienced consequence may be its own aspect only when it is independently stated and useful. Do not invent a consequence or technical cause.

Do not add a broader `품질` or `전반적 상품 경험` unit for the same evaluation already explained by a specific aspect. For example, satisfaction with a screen size suitable for dual-monitor use belongs to that screen-size unit, not to an additional whole-product unit. For a separate explicit whole-product evaluation that meets the rule above, use `전반적 상품 경험` rather than the generic aspect `모니터`.

For `마감이 깔끔하고 흠집이 없어 품질에 만족합니다`, return the `마감 / 깔끔함` and `흠집 / 없음` units with positive sentiment. The concluding quality satisfaction evaluates those stated properties; do not add a third `품질` unit from the same clause. A generic quality-only review may still use `품질` when no more specific target is available.

## Excerpt and opinion

Every `excerpt` must:

- be copied exactly without paraphrasing;
- be one contiguous substring of `<review>`;
- contain enough text to preserve target, state, comparison, negation, degree, context, attribution, uncertainty, and consequence;
- include multiple sentences when they are required for the meaning.

The `opinion` must preserve the complete proposition concisely, including relevant direction, degree, comparison, usage context, temporal context, consequence, uncertainty, and limited experience. Do not reduce it to only the aspect and status or to only a sentiment label.

## Inclusion and exclusion

Include product-use facts and sensory, ergonomic, behavioral, visual, or functional observations even without explicit praise or criticism; label those `neutral` when the direction is genuinely absent.

Exclude shipping, packaging, seller service, payment, delivery issues, promotions, free gifts, and content unrelated to the product itself or its use. A logistics-only review returns an empty array even if the logistics are praised or criticized.

Exclude units justified only by coupon discounts or promotion-dependent purchase prices. `쿠폰을 적용해 싸게 샀어요` is a transaction or promotion observation, not a product-price unit. Keep an independently stated value-for-money or product-price evaluation elsewhere in the review, but do not include the coupon sentence as its evidence or opinion.

Do not include objective product facts unless the reviewer reports or evaluates them as part of their experience.

If the review contains no valid product-related observation or evaluation, return `{ "opinion_units": [] }`.

Use the same language as the review for `raw_aspect`, `raw_status`, and `opinion`.

Before returning JSON, check every unit against the review: the aspect contains no observed value or evaluation, the status names a grounded product state or is `null`, the opinion retains comparison and uncertainty, and no broader unit repeats the same evaluation. Keep every excerpt an exact contiguous substring.

Return valid JSON only. Do not return Markdown, explanations, headings, or code fences. Use exactly this envelope and no extra fields:

{
  "opinion_units": [
    {
      "raw_aspect": "속성 명칭",
      "raw_status": "속성의 상태 또는 null",
      "excerpt": "리뷰에서 그대로 복사한 연속 근거",
      "opinion": "리뷰어가 표현한 완전한 관찰 또는 평가",
      "sentiment": "positive | negative | mixed | neutral | unknown"
    }
  ]
}

## Few-shot example 1: multiple monitor attributes

Product name:

32인치 모니터

Product category:

모니터

Review:

<review>
이전 모니터보다 무게가 조금 무겁지만 저는 묵직해서 마음에 듭니다. 장시간 화면을 볼 때 눈이 편했고, 빛샘은 꽤 거슬렸습니다.
</review>

Output:

{
  "opinion_units": [
    {
      "raw_aspect": "무게",
      "raw_status": "무거움",
      "excerpt": "이전 모니터보다 무게가 조금 무겁지만 저는 묵직해서 마음에 듭니다.",
      "opinion": "이전 모니터보다 무게가 조금 무겁지만 묵직한 느낌을 선호함",
      "sentiment": "positive"
    },
    {
      "raw_aspect": "장시간 시청",
      "raw_status": "편안함",
      "excerpt": "장시간 화면을 볼 때 눈이 편했고, 빛샘은 꽤 거슬렸습니다.",
      "opinion": "장시간 화면을 볼 때 눈이 편안함",
      "sentiment": "positive"
    },
    {
      "raw_aspect": "빛샘",
      "raw_status": "발생",
      "excerpt": "장시간 화면을 볼 때 눈이 편했고, 빛샘은 꽤 거슬렸습니다.",
      "opinion": "빛샘이 꽤 거슬림",
      "sentiment": "negative"
    }
  ]
}

## Few-shot example 2: mixed, neutral, unknown, and exclusion

Product name:

게이밍 모니터

Product category:

모니터

Review:

<review>
배송은 빨랐습니다. 화면 밝기는 낮에 보기에는 충분해서 좋지만 밤에는 너무 밝아 눈부신 점이 아쉽습니다. HDMI 단자는 직접 확인해 보니 2개입니다. 받침대 내구성은 더 써봐야 알 것 같습니다.
</review>

Output:

{
  "opinion_units": [
    {
      "raw_aspect": "화면 밝기",
      "raw_status": "충분하지만 눈부심",
      "excerpt": "화면 밝기는 낮에 보기에는 충분해서 좋지만 밤에는 너무 밝아 눈부신 점이 아쉽습니다.",
      "opinion": "화면 밝기는 낮에 보기에는 충분해서 좋지만 밤에는 너무 밝아 눈부신 점이 아쉬움",
      "sentiment": "mixed"
    },
    {
      "raw_aspect": "HDMI 단자",
      "raw_status": "2개",
      "excerpt": "HDMI 단자는 직접 확인해 보니 2개입니다.",
      "opinion": "직접 확인한 HDMI 단자는 2개임",
      "sentiment": "neutral"
    },
    {
      "raw_aspect": "받침대 내구성",
      "raw_status": "판단 보류",
      "excerpt": "받침대 내구성은 더 써봐야 알 것 같습니다.",
      "opinion": "받침대 내구성은 더 사용해 봐야 판단할 수 있음",
      "sentiment": "unknown"
    }
  ]
}

### Input

Product name:

{{product_name}}

Product category:

{{product_category}}

Review:

<review>
{{review}}
</review>

### Output
