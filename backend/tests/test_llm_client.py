"""The OpenAI-compatible client (Groq, Gemini, OpenRouter), the fallback chain and the env switch. No network and no key are used."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.campaigns import CampaignService, VariantGenerator
from app.llm import (
    FallbackClient,
    OpenAICompatClient,
    DraftBrief,
    LLMConfigError,
    LLMError,
    LLMReplyError,
    LLMRequest,
    MockLLMClient,
    llm_from_env,
)
from app.models import Channel, VariantSource, VariantStatus

KEY = "sk-test-SECRET-123"
PUSH = "t_push_still_there"
GOOD = "Hey {{name}}, your {{topItem}} is still here: {{link}}"
REQUEST = LLMRequest(
    system="be brief",
    prompt="write two variants",
    brief=DraftBrief("t", Channel.PUSH, "Hi {{name}}", 2),
)


URL = "https://example.test/v1"


def reply(text: str, **extra) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], **extra}


def client_for(handler, **kw) -> OpenAICompatClient:
    kw.setdefault("transport", httpx.MockTransport(handler))
    kw.setdefault("base_url", URL)
    return OpenAICompatClient(KEY, "test-model", **kw)


def drafts(*texts) -> str:
    return json.dumps([{"text": t, "principle": "urgency", "tone": "brisk"} for t in texts])


# ---- the request --------------------------------------------------------------------------
def test_request_shape_and_headers():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["method"] = str(request.url), request.method
        seen["headers"], seen["body"] = request.headers, json.loads(request.content)
        return httpx.Response(200, json=reply("hello"))

    assert client_for(handler).complete(REQUEST) == "hello"
    assert seen["url"] == f"{URL}/chat/completions" and seen["method"] == "POST"
    assert seen["headers"]["authorization"] == f"Bearer {KEY}"
    body = seen["body"]
    assert body["model"] == "test-model" and body["max_tokens"] == 2000
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "write two variants"},
    ]
    assert KEY not in json.dumps(body)


def test_base_url_and_limits_can_be_changed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["max"] = str(request.url), json.loads(request.content)["max_tokens"]
        return httpx.Response(200, json=reply("ok"))

    client_for(handler, base_url="https://proxy.example/", max_tokens=500).complete(REQUEST)
    assert seen == {"url": "https://proxy.example/chat/completions", "max": 500}


def test_name_is_the_model_and_the_key_is_hidden():
    c = OpenAICompatClient(KEY, "some-model", URL)
    assert c.name == "some-model" and KEY not in repr(c) and KEY not in str(c)
    with pytest.raises(ValueError, match="API key"):
        OpenAICompatClient("   ", "m", URL)
    with pytest.raises(ValueError, match="model"):
        OpenAICompatClient(KEY, " ", URL)


# ---- the answer ---------------------------------------------------------------------------
def test_the_first_choice_text_is_returned():
    c = client_for(lambda r: httpx.Response(200, json=reply("[1, 2]")))
    assert c.complete(REQUEST) == "[1, 2]"


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": "nope"},
        {},
        [],
        {"choices": [{"message": {"content": "[1"}, "finish_reason": "length"}]},
    ],
)
def test_an_answer_without_usable_text_is_a_reply_error(payload):
    with pytest.raises(LLMReplyError):
        client_for(lambda r: httpx.Response(200, json=payload)).complete(REQUEST)


# ---- failures -----------------------------------------------------------------------------
def test_http_errors_become_llm_errors_with_the_apis_message():
    body = {"error": {"message": "invalid key"}}
    with pytest.raises(LLMError, match="401.*invalid key") as err:
        client_for(lambda r: httpx.Response(401, json=body)).complete(REQUEST)
    assert KEY not in str(err.value)


@pytest.mark.parametrize("status", [400, 429, 500, 503])
def test_every_non_200_is_an_llm_error(status):
    with pytest.raises(LLMError, match=str(status)):
        client_for(lambda r: httpx.Response(status, text="busy")).complete(REQUEST)


def test_a_long_or_odd_error_body_is_shortened():
    with pytest.raises(LLMError) as err:
        client_for(lambda r: httpx.Response(500, text="x" * 5000)).complete(REQUEST)
    assert len(str(err.value)) < 400
    with pytest.raises(LLMError, match="no details"):
        client_for(lambda r: httpx.Response(502, text="")).complete(REQUEST)


def test_non_json_success_is_an_llm_error():
    with pytest.raises(LLMError, match="not JSON"):
        client_for(lambda r: httpx.Response(200, text="<html>")).complete(REQUEST)


def test_timeouts_and_connection_failures_become_llm_errors():
    def slow(request):
        raise httpx.ReadTimeout("took too long", request=request)

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError, match="did not answer within 5 seconds"):
        client_for(slow, timeout=5).complete(REQUEST)
    with pytest.raises(LLMError, match="could not reach the model"):
        client_for(down).complete(REQUEST)


# ---- with the generator and the API -------------------------------------------------------
@pytest.fixture
def draft(repo, config, library):
    svc = CampaignService(repo)
    svc.create_draft(config)
    for t in library.templates:
        svc.save_template(config.use_case_id, t)
    for v in library.variants:
        svc.save_variant(config.use_case_id, v)
    return svc, config.use_case_id


def test_the_generator_works_through_the_real_client(draft):
    svc, cid = draft
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=reply(f"```json\n{drafts(GOOD, 'Hi {{nickname}}')}\n```"))

    result = VariantGenerator(svc, client_for(handler)).generate(cid, PUSH, count=2)
    assert [v.text for v in result.created] == [GOOD]
    assert [s.code for s in result.skipped] == ["unknown_variable"]
    v = result.created[0]
    assert v.status == VariantStatus.PENDING and v.source == VariantSource.LLM
    assert v.generated_by == "test-model" and result.model == "test-model"
    assert "exactly 2" in seen["body"]["messages"][0]["content"]
    assert "JSON" in seen["body"]["system"]


def test_an_upstream_failure_stores_nothing(draft):
    svc, cid = draft
    before = len(svc.repo.list_variants(cid))
    c = client_for(lambda r: httpx.Response(503, json={"error": {"message": "overloaded"}}))
    with pytest.raises(LLMError, match="overloaded"):
        VariantGenerator(svc, c).generate(cid, PUSH)
    assert len(svc.repo.list_variants(cid)) == before


def api_with(repo, config, library, llm):
    api = TestClient(create_app(repo, llm=llm))
    data = config.model_dump(mode="json")
    data["use_case_id"] = "spring"
    api.post("/campaigns", json=data)
    for t in library.templates:
        api.put(f"/campaigns/spring/templates/{t.template_id}", json=t.model_dump(mode="json"))
    return api


def test_generate_endpoint_with_the_real_client(repo, config, library):
    c = client_for(lambda r: httpx.Response(200, json=reply(drafts(GOOD))))
    api = api_with(repo, config, library, c)
    response = api.post("/campaigns/spring/variants/generate", json={"template_id": PUSH})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "test-model" and body["created"][0]["generated_by"] == "test-model"


def test_generate_endpoint_reports_upstream_failures_as_502_without_the_key(repo, config, library):
    c = client_for(lambda r: httpx.Response(401, json={"error": {"message": "invalid key"}}))
    api = api_with(repo, config, library, c)
    response = api.post("/campaigns/spring/variants/generate", json={"template_id": PUSH})
    assert response.status_code == 502
    assert "invalid key" in response.json()["detail"] and KEY not in response.text
    assert api.get("/campaigns/spring/variants").json() == []


def test_a_cut_off_reply_is_502(repo, config, library):
    cut = {"choices": [{"message": {"content": "[{"}, "finish_reason": "length"}]}
    api = api_with(repo, config, library, client_for(lambda r: httpx.Response(200, json=cut)))
    response = api.post("/campaigns/spring/variants/generate", json={"template_id": PUSH})
    assert response.status_code == 502 and "cut off" in response.json()["detail"]


# ---- the fallback chain -------------------------------------------------------------------
class Stub:
    def __init__(self, name, text=None):
        self.name, self.text, self.calls = name, text, 0

    def complete(self, request):
        self.calls += 1
        if self.text is None:
            raise LLMError("down")
        return self.text


def test_fallback_uses_the_first_that_answers_and_reports_its_name():
    a, b, c = Stub("a"), Stub("b", "hi"), Stub("c", "never")
    chain = FallbackClient([a, b, c])
    assert chain.complete(REQUEST) == "hi"
    assert chain.name == "b" and (a.calls, b.calls, c.calls) == (1, 1, 0)


def test_fallback_raises_when_everything_fails_and_needs_a_client():
    with pytest.raises(LLMError, match="every model failed.*a: down.*b: down"):
        FallbackClient([Stub("a"), Stub("b")]).complete(REQUEST)
    with pytest.raises(ValueError):
        FallbackClient([])


def test_fallback_ends_in_the_mock():
    chain = llm_from_env({"NUDGEIQ_LLM": "chain", "GROQ_API_KEY": KEY, "NUDGEIQ_GROQ_MODEL": "g1"})
    assert isinstance(chain, FallbackClient)
    assert chain._clients[-1].__class__ is MockLLMClient


# ---- the environment switch ---------------------------------------------------------------
GROQ = {"NUDGEIQ_LLM": "groq", "GROQ_API_KEY": KEY, "NUDGEIQ_GROQ_MODEL": "g-model"}


def test_default_is_the_mock():
    assert isinstance(llm_from_env({}), MockLLMClient)
    assert isinstance(llm_from_env({"NUDGEIQ_LLM": "mock"}), MockLLMClient)
    assert isinstance(llm_from_env({"NUDGEIQ_LLM": " MOCK "}), MockLLMClient)


def test_a_key_alone_does_not_switch_to_the_real_model():
    assert isinstance(llm_from_env({"GROQ_API_KEY": KEY}), MockLLMClient)


def test_a_provider_needs_a_key_and_a_model():
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        llm_from_env({"NUDGEIQ_LLM": "groq", "NUDGEIQ_GROQ_MODEL": "m"})
    with pytest.raises(LLMConfigError, match="NUDGEIQ_GEMINI_MODEL"):
        llm_from_env({"NUDGEIQ_LLM": "gemini", "GEMINI_API_KEY": KEY})
    with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
        llm_from_env({"NUDGEIQ_LLM": "openrouter", "OPENROUTER_API_KEY": " "})


def test_each_provider_gets_its_own_url():
    c = llm_from_env(GROQ)
    assert isinstance(c, OpenAICompatClient) and c.name == "g-model"
    assert c.base_url == "https://api.groq.com/openai/v1" and c.timeout == 60.0
    g = llm_from_env({"NUDGEIQ_LLM": "gemini", "GEMINI_API_KEY": KEY, "NUDGEIQ_GEMINI_MODEL": "m"})
    assert "generativelanguage.googleapis.com" in g.base_url
    o = llm_from_env(
        {"NUDGEIQ_LLM": "openrouter", "OPENROUTER_API_KEY": KEY, "NUDGEIQ_OPENROUTER_MODEL": "m:free"}
    )
    assert o.base_url == "https://openrouter.ai/api/v1"


def test_chain_order_is_groq_groq2_gemini_then_mock():
    env = {
        **GROQ,
        "NUDGEIQ_LLM": "chain",
        "NUDGEIQ_GROQ_MODEL_2": "g-small",
        "GEMINI_API_KEY": KEY,
        "NUDGEIQ_GEMINI_MODEL": "gem",
    }
    chain = llm_from_env(env)
    assert [getattr(c, "name", "") for c in chain._clients][:3] == ["g-model", "g-small", "gem"]
    assert isinstance(chain._clients[-1], MockLLMClient)


def test_chain_with_nothing_set_up_is_refused():
    with pytest.raises(LLMConfigError, match="at least one provider"):
        llm_from_env({"NUDGEIQ_LLM": "chain"})


def test_chain_survives_a_blocked_network_and_falls_to_the_mock(draft):
    svc, cid = draft
    down = client_for(lambda r: httpx.Response(429, text="rate limited"))
    chain = FallbackClient([down, MockLLMClient()])
    result = VariantGenerator(svc, chain).generate(cid, PUSH, count=1)
    assert result.model == "mock" or result.model == chain.name


def test_timeout_override_and_bad_timeouts():
    assert llm_from_env({**GROQ, "NUDGEIQ_LLM_TIMEOUT": "12.5"}).timeout == 12.5
    for bad in ("fast", "0", "-3"):
        with pytest.raises(LLMConfigError, match="TIMEOUT"):
            llm_from_env({**GROQ, "NUDGEIQ_LLM_TIMEOUT": bad})


def test_unknown_provider_is_refused():
    with pytest.raises(LLMConfigError, match="openai.*mock.*groq"):
        llm_from_env({"NUDGEIQ_LLM": "openai"})


def test_create_app_reads_the_environment(monkeypatch, repo):
    monkeypatch.delenv("NUDGEIQ_LLM", raising=False)
    assert isinstance(create_app(repo).state.llm, MockLLMClient)
    for k, v in {**GROQ, "NUDGEIQ_GROQ_MODEL": "env-model"}.items():
        monkeypatch.setenv(k, v)
    app = create_app(repo)
    assert isinstance(app.state.llm, OpenAICompatClient) and app.state.llm.name == "env-model"


def test_create_app_fails_at_start_up_on_bad_settings_before_touching_storage(monkeypatch, repo):
    monkeypatch.setenv("NUDGEIQ_LLM", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        create_app(repo)
    assert repo.list_campaigns() == []  # nothing was seeded
    assert isinstance(create_app(repo, llm=MockLLMClient()).state.llm, MockLLMClient)
