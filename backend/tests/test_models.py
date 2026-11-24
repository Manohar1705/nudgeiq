import pytest
from pydantic import ValidationError

from app.models import (
    Action,
    Channel,
    CustomerState,
    Decision,
    MessageTemplate,
    MessageVariant,
    Outcome,
    OutcomeType,
    RewardConfig,
    VariantStatus,
)


def make_state(**kw):
    base = dict(customer_id="c1", cohort_id="cart_high_aov", cart_value=80, abandoned_minutes=45)
    base.update(kw)
    return CustomerState(**base)


def test_state_valid_and_rejects_negative_cart():
    assert make_state().contactable is True
    with pytest.raises(ValidationError):
        make_state(cart_value=-1)


def test_state_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        make_state(surprise="x")


def test_action_key_and_wait():
    a = Action(channel=Channel.PUSH, timing="T+1h", offer="free_shipping")
    assert a.key == "push|T+1h|free_shipping|None"
    assert Action.wait().is_wait
    assert Action.wait().key == "wait"
    assert a == Action(channel=Channel.PUSH, timing="T+1h", offer="free_shipping")


def test_reward_sign_validation():
    with pytest.raises(ValidationError):
        RewardConfig(points={OutcomeType.UNSUBSCRIBED: 5})
    with pytest.raises(ValidationError):
        RewardConfig(points={OutcomeType.ORDER_PLACED: -5})
    assert RewardConfig(points={OutcomeType.ORDER_PLACED: 40}).reward_for(OutcomeType.IGNORED) == 0


def test_template_variables_and_render():
    t = MessageTemplate(
        template_id="t1", name="x", channel=Channel.PUSH, body="Hi {{name}}, {{topItem}} {{ name }}"
    )
    assert t.variables == ["name", "topItem"]
    v = MessageVariant(
        variant_id="v1",
        template_id="t1",
        text="Hi {{name}}, your {{topItem}}",
        principle="social_proof",
        tone="warm",
    )
    assert v.status == VariantStatus.PENDING
    assert v.render({"name": "Asha", "topItem": "lamp"}) == "Hi Asha, your lamp"
    with pytest.raises(KeyError):
        v.render({"name": "Asha"})


def test_outcome_and_decision_trace():
    o = Outcome(decision_id="d1", customer_id="c1", outcome_type=OutcomeType.ORDER_PLACED)
    assert o.occurred_at.tzinfo is not None
    d = Decision(decision_id="d1", customer_id="c1", state=make_state())
    d.log("guardrail", "passed")
    assert d.chosen.is_wait
    assert d.steps[0].agent == "guardrail"
