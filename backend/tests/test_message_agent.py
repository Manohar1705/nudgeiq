import pytest

from app.agents.message import (
    MessageAgent,
    MessageError,
    NoApprovedVariantError,
    build_values,
)
from app.models import Action, Channel, MessageTemplate, MessageVariant, TemplateLibrary


def push(offer="free_shipping"):
    return Action(channel=Channel.PUSH, timing="T+1h", offer=offer)


def test_only_approved_variants_are_used(library, make_state):
    agent = MessageAgent(library, seed=1)
    approved = {v.variant_id for v in agent.approved_variants()}
    assert "v_push_authority" not in approved and "v_push_rejected" not in approved
    used = {agent.render(push(), make_state()).variant_id for _ in range(100)}
    assert used <= {"v_push_warm", "v_push_social"}
    assert len(used) == 2  # both approved variants get used


def test_variant_matches_channel(library, make_state):
    agent = MessageAgent(library, seed=1)
    sms = Action(channel=Channel.SMS, timing="T+1h", offer="free_shipping")
    assert agent.render(sms, make_state()).variant_id == "v_sms_stock"


def test_variant_matches_offer(library):
    agent = MessageAgent(library, seed=1)
    bonus_ids = {v.variant_id for v in agent.approved_variants(Channel.EMAIL, "bonus_points")}
    percent_ids = {v.variant_id for v in agent.approved_variants(Channel.EMAIL, "percent_off")}
    assert bonus_ids == {"v_email_warm", "v_email_direct"}
    assert percent_ids == {"v_email_cart_ready"}
    assert agent.can_send(Channel.EMAIL, "percent_off")


def test_merge_values_are_filled(library, make_state):
    agent = MessageAgent(library, seed=1)
    text = agent.render(push(), make_state(), {"first_name": "Asha", "top_item": "lamp"}).text
    assert "Asha" in text and "lamp" in text and "{{" not in text


def test_defaults_when_profile_missing(make_state):
    values = build_values(make_state())
    assert values["name"] == "there" and values["topItem"] == "items"


def test_wait_action_cannot_render(library, make_state):
    with pytest.raises(MessageError):
        MessageAgent(library).render(Action.wait(), make_state())


def test_no_approved_variant_raises(library, make_state):
    library = library.model_copy(
        update={"variants": [v for v in library.variants if "push" not in v.variant_id]}
    )
    agent = MessageAgent(library)
    assert Channel.PUSH not in agent.deliverable_channels()
    with pytest.raises(NoApprovedVariantError):
        agent.render(push(), make_state())


def _library(text, channel=Channel.PUSH, status="approved"):
    return TemplateLibrary(
        templates=[MessageTemplate(template_id="t", name="n", channel=channel, body="x")],
        variants=[
            MessageVariant(
                variant_id="v", template_id="t", text=text, principle="p", tone="t", status=status
            )
        ],
    )


def test_unknown_variable_in_approved_variant_fails_early():
    with pytest.raises(MessageError, match="unknown variables"):
        MessageAgent(_library("Hi {{nickname}}"))


def test_unknown_variable_ok_while_pending():
    MessageAgent(_library("Hi {{nickname}}", status="pending"))


def test_sms_needs_opt_out():
    with pytest.raises(MessageError, match="STOP"):
        MessageAgent(_library("Buy now {{link}}", channel=Channel.SMS))
    MessageAgent(_library("Buy now {{link}} Reply STOP to opt out", channel=Channel.SMS))


def test_library_rejects_orphan_variant():
    with pytest.raises(ValueError):
        TemplateLibrary(
            templates=[],
            variants=[
                MessageVariant(
                    variant_id="v", template_id="missing", text="x", principle="p", tone="t"
                )
            ],
        )
