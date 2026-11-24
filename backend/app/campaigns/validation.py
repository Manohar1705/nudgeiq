"""Campaign validation: pure checks that run before a campaign is saved or activated.

Nothing here touches storage. The functions take a config (and optionally a template
library) and return a report of issues instead of raising on the first problem, so a
dashboard can show everything that is wrong in one go.

Severity decides what the caller does:
  ERROR    blocks activation (a draft may still be saved with errors)
  WARNING  allows activation but is worth showing to the user

The content rules (known template variables, SMS opt-out) reuse the same rules the
message agent enforces at send time, so the two can never disagree.
"""

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.eligibility import PREDICATES
from app.agents.message import KNOWN_VARIABLES, has_sms_opt_out
from app.models import (
    Channel,
    MessageTemplate,
    MessageVariant,
    OutcomeType,
    TemplateLibrary,
    UseCaseConfig,
    VariantStatus,
    parse_timing,
)

_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# Sample values used only to estimate SMS length. Real values come from build_values().
_SAMPLE_VALUES = {
    "name": "Alexandra",
    "topItem": "Wireless headphones",
    "aov": "120",
    "link": "https://example.com/cart/c1234567",
    "bonusPts": "600",
    "units": "3",
    "store": "Downtown",
}
_SMS_SEGMENT_GSM = 160
_SMS_SEGMENT_UNICODE = 70


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ValidationIssue(BaseModel):
    """One problem. `code` is stable for programs, `message` is for people."""

    model_config = ConfigDict(frozen=True)

    severity: Severity
    code: str
    path: str  # where, e.g. "action_space.channels" or "templates[t_sms].body"
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    @property
    def is_valid(self) -> bool:
        """No errors. Warnings do not count."""
        return not self.errors

    @property
    def can_activate(self) -> bool:
        return self.is_valid

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}


def _err(code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(severity=Severity.ERROR, code=code, path=path, message=message)


def _warn(code: str, path: str, message: str) -> ValidationIssue:
    return ValidationIssue(severity=Severity.WARNING, code=code, path=path, message=message)


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for v in values:
        if v in seen and v not in dupes:
            dupes.append(v)
        seen.add(v)
    return dupes


# ---- raw data -> issues ---------------------------------------------------------------


def _loc_to_path(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return path or "(root)"


def _schema_issues(prefix: str, exc: ValidationError) -> list[ValidationIssue]:
    issues = []
    for e in exc.errors():
        path = _loc_to_path(tuple(e["loc"]))
        full = f"{prefix}.{path}" if prefix and path != "(root)" else (prefix or path)
        issues.append(_err("schema", full, e["msg"]))
    return issues


# ---- config checks --------------------------------------------------------------------


def validate_config(config: UseCaseConfig) -> list[ValidationIssue]:
    """Checks on the campaign settings alone. No templates needed."""
    issues: list[ValidationIssue] = []
    space = config.action_space

    # identity
    if not _ID.match(config.use_case_id):
        issues.append(
            _err(
                "bad_id",
                "use_case_id",
                "use_case_id must start with a lowercase letter and use only "
                "lowercase letters, digits and underscores",
            )
        )
    if not config.name.strip():
        issues.append(_err("empty_name", "name", "name cannot be blank"))

    # cohorts
    if not config.cohorts:
        issues.append(_err("no_cohorts", "cohorts", "add at least one cohort"))
    for dup in _duplicates(c.cohort_id for c in config.cohorts):
        issues.append(_err("duplicate", "cohorts", f"cohort_id '{dup}' is used more than once"))
    for i, cohort in enumerate(config.cohorts):
        if cohort.size == 0:
            issues.append(
                _warn("empty_cohort", f"cohorts[{i}]", f"cohort '{cohort.cohort_id}' is empty")
            )

    # context signals
    for dup in _duplicates(s.signal_id for s in config.context_signals):
        issues.append(
            _err("duplicate", "context_signals", f"signal_id '{dup}' is used more than once")
        )
    if not config.active_signals:
        issues.append(
            _warn(
                "no_signals",
                "context_signals",
                "every context signal is off, so the agent decides without customer context",
            )
        )

    # action space: each list needs at least one option and no repeats
    menus: dict[str, list[str]] = {
        "levers": list(space.levers),
        "channels": [c.value for c in space.channels],
        "offers": list(space.offers),
        "timings": list(space.timings),
    }
    for name, values in menus.items():
        path = f"action_space.{name}"
        if not values:
            issues.append(_err("empty_menu", path, f"add at least one entry to {name}"))
        for dup in _duplicates(values):
            issues.append(_err("duplicate", path, f"'{dup}' is listed more than once"))
        if any(not v.strip() for v in values):
            issues.append(_err("blank_option", path, f"{name} cannot contain a blank entry"))

    # eligibility: rule names must exist, and keys must point at a real option
    options = set(menus["levers"]) | set(menus["channels"]) | set(menus["offers"])
    for option, rule in sorted(space.eligibility.items()):
        path = f"action_space.eligibility.{option}"
        if rule not in PREDICATES:
            issues.append(
                _err(
                    "unknown_rule",
                    path,
                    f"unknown eligibility rule '{rule}' (known: {', '.join(sorted(PREDICATES))})",
                )
            )
        if option not in options:
            issues.append(
                _warn(
                    "orphan_rule",
                    path,
                    f"'{option}' is not a lever, channel or offer here, so the rule never applies",
                )
            )

    # guardrails
    g = config.guardrails
    if g.max_contacts_per_window == 0:
        issues.append(
            _err(
                "no_contacts_allowed",
                "guardrails.max_contacts_per_window",
                "a limit of 0 blocks every message",
            )
        )
    if not g.allowed_weekdays:
        issues.append(
            _err("no_weekdays", "guardrails.allowed_weekdays", "allow at least one weekday")
        )
    elif any(d < 0 or d > 6 for d in g.allowed_weekdays):
        issues.append(
            _err(
                "bad_weekday",
                "guardrails.allowed_weekdays",
                "weekdays must be 0 (Monday) to 6 (Sunday)",
            )
        )
    elif len(set(g.allowed_weekdays)) != len(g.allowed_weekdays):
        issues.append(
            _warn("duplicate", "guardrails.allowed_weekdays", "a weekday is listed twice")
        )
    if g.cooldown_hours > g.window_days * 24:
        issues.append(
            _warn(
                "cooldown_exceeds_window",
                "guardrails.cooldown_hours",
                "the cool-down is longer than the contact window, so only one message fits",
            )
        )
    # a timing the journey can never reach is dead weight in the menu
    for i, timing in enumerate(space.timings):
        try:
            offset = parse_timing(timing)
        except ValueError:
            continue  # already reported by the schema check
        if offset.total_seconds() >= g.exit_after_days * 86400:
            issues.append(
                _warn(
                    "unreachable_timing",
                    f"action_space.timings[{i}]",
                    f"{timing} is after the {g.exit_after_days}-day journey window, never sent",
                )
            )

    # rewards
    if config.rewards.reward_for(OutcomeType.ORDER_PLACED) <= 0:
        issues.append(
            _warn(
                "no_purchase_reward",
                "rewards.points.order_placed",
                "a purchase should earn a positive reward, or the agent has nothing to aim for",
            )
        )
    if config.learning.holdout_fraction == 0:
        issues.append(
            _warn(
                "no_holdout",
                "learning.holdout_fraction",
                "without a control group, uplift cannot be measured",
            )
        )
    return issues


# ---- content checks (templates and variants) -------------------------------------------


def _sms_segments(text: str) -> int:
    limit = _SMS_SEGMENT_GSM if text.isascii() else _SMS_SEGMENT_UNICODE
    return max(1, -(-len(text) // limit))  # ceiling division


def _check_template(
    t: MessageTemplate, channels: set[Channel], offers: set[str]
) -> list[ValidationIssue]:
    path = f"templates[{t.template_id}]"
    issues: list[ValidationIssue] = []
    if not t.body.strip():
        issues.append(_err("empty_body", f"{path}.body", "template body is blank"))
    unknown = sorted(set(t.variables) - KNOWN_VARIABLES)
    if unknown:
        issues.append(_err("unknown_variable", f"{path}.body", f"uses unknown variables {unknown}"))
    if t.channel == Channel.SMS and not has_sms_opt_out(t.body):
        issues.append(
            _err("sms_missing_stop", f"{path}.body", "an SMS must tell customers to reply STOP")
        )
    if t.channel == Channel.EMAIL and not t.body.lstrip().startswith("Subject:"):
        issues.append(
            _warn("email_no_subject", f"{path}.body", "an email should start with 'Subject:'")
        )
    if t.channel not in channels:
        issues.append(
            _warn(
                "unused_channel",
                f"{path}.channel",
                f"this campaign does not use {t.channel.value}, so the template is never sent",
            )
        )
    for offer in t.offers:
        if offer not in offers:
            issues.append(
                _err(
                    "unknown_offer",
                    f"{path}.offers",
                    f"offer '{offer}' is not in this campaign's offers",
                )
            )
    return issues


def _check_variant(v: MessageVariant, template: MessageTemplate) -> list[ValidationIssue]:
    """Rejected variants are never sent, so they are not checked.

    A problem in an APPROVED variant is an error (it could reach a customer). In a
    PENDING one it is a warning, because a reviewer still has to approve it first.
    """
    if v.status == VariantStatus.REJECTED:
        return []
    severity = Severity.ERROR if v.status == VariantStatus.APPROVED else Severity.WARNING
    path = f"variants[{v.variant_id}].text"

    def issue(code: str, message: str) -> ValidationIssue:
        return ValidationIssue(severity=severity, code=code, path=path, message=message)

    issues: list[ValidationIssue] = []
    if not v.text.strip():
        issues.append(issue("empty_body", "variant text is blank"))
    unknown = sorted(set(v.variables) - KNOWN_VARIABLES)
    if unknown:
        issues.append(issue("unknown_variable", f"uses unknown variables {unknown}"))
    if template.channel == Channel.SMS:
        if not has_sms_opt_out(v.text):
            issues.append(issue("sms_missing_stop", "an SMS must tell customers to reply STOP"))
        if not unknown:
            rendered = _VAR.sub(lambda m: _SAMPLE_VALUES.get(m.group(1), "sample"), v.text)
            segments = _sms_segments(rendered)
            if segments > 1:
                issues.append(
                    _warn(
                        "sms_too_long",
                        path,
                        f"about {len(rendered)} characters, sent as {segments} text messages",
                    )
                )
    return issues


def variant_approval_issues(
    variant: MessageVariant, template: MessageTemplate
) -> list[ValidationIssue]:
    """What the checks would report if `variant` were approved, whatever its status is now.

    ERROR issues mean approving it would put an unsendable message in the library. WARNING
    issues are advice the reviewer should see. This is the same check validation runs on an
    approved variant, so approving and activating can never disagree.
    """
    return _check_variant(variant.model_copy(update={"status": VariantStatus.APPROVED}), template)


def validate_library(config: UseCaseConfig, library: TemplateLibrary) -> list[ValidationIssue]:
    """Checks on templates and variants against the campaign they belong to."""
    issues: list[ValidationIssue] = []
    channels = set(config.action_space.channels)
    offers = set(config.action_space.offers)
    by_id = {t.template_id: t for t in library.templates}

    for t in library.templates:
        issues.extend(_check_template(t, channels, offers))
    for v in library.variants:
        issues.extend(_check_variant(v, by_id[v.template_id]))

    # readiness: can the campaign actually send anything?
    approved = [v for v in library.variants if v.status == VariantStatus.APPROVED]
    if not approved:
        issues.append(
            _err(
                "no_approved_variants",
                "variants",
                "no variant is approved, so nothing can be sent",
            )
        )
        return issues

    ready_channels = {by_id[v.template_id].channel for v in approved}
    for channel in config.action_space.channels:
        if channel not in ready_channels:
            issues.append(
                _warn(
                    "channel_not_ready",
                    f"action_space.channels.{channel.value}",
                    f"no approved variant for {channel.value}, so that channel will not be used",
                )
            )
    for offer in config.action_space.offers:
        carriers = [
            v
            for v in approved
            if not by_id[v.template_id].offers or offer in by_id[v.template_id].offers
        ]
        if not carriers:
            issues.append(
                _warn(
                    "offer_not_ready",
                    f"action_space.offers.{offer}",
                    f"no approved variant can carry the '{offer}' offer",
                )
            )
    return issues


# ---- public entry points ----------------------------------------------------------------


def validate_campaign(
    config: UseCaseConfig, library: TemplateLibrary | None = None
) -> ValidationReport:
    """Validate a parsed config, and its templates if a library is given.

    Without a library the content checks are skipped and the report says nothing about
    whether the campaign can send. Pass the library when deciding on activation.
    """
    issues = validate_config(config)
    if library is not None:
        issues.extend(validate_library(config, library))
    return ValidationReport(issues=issues)


def validate_draft(
    config_data: Mapping[str, Any], library_data: Mapping[str, Any] | None = None
) -> ValidationReport:
    """Validate raw dicts, as they arrive from the dashboard.

    A schema failure is reported as issues with field paths, and the deeper checks are
    skipped for the part that did not parse.
    """
    try:
        config = UseCaseConfig.model_validate(config_data)
    except ValidationError as exc:
        issues = _schema_issues("config", exc)
        if library_data is not None:
            try:
                TemplateLibrary.model_validate(library_data)
            except ValidationError as lib_exc:
                issues.extend(_schema_issues("library", lib_exc))
        return ValidationReport(issues=issues)

    library: TemplateLibrary | None = None
    schema_issues: list[ValidationIssue] = []
    if library_data is not None:
        try:
            library = TemplateLibrary.model_validate(library_data)
        except ValidationError as exc:
            schema_issues = _schema_issues("library", exc)
    report = validate_campaign(config, library)
    return ValidationReport(issues=[*schema_issues, *report.issues])
