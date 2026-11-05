import pytest

from weekend_saver.models import Action, Decision, PowerState, ResourceStatus
from weekend_saver.planning import decide

SKIP_TAGS = {"Weekend-Saver": "SKIP"}


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PowerState.RUNNING, Decision.ACT),
        (PowerState.STOPPED, Decision.SKIP),
        (PowerState.TRANSITIONING, Decision.SKIP),
        (PowerState.UNKNOWN, Decision.ERROR),
    ],
)
def test_disable_acts_on_running_resources_and_flags_unreadable_states(state, expected):
    decision, _ = decide(Action.DISABLE, ResourceStatus(state), recorded=False)

    assert decision is expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PowerState.STOPPED, Decision.ACT),
        (PowerState.RUNNING, Decision.SKIP),
        (PowerState.TRANSITIONING, Decision.ERROR),
        (PowerState.UNKNOWN, Decision.ERROR),
    ],
)
def test_enable_restores_recorded_resources_and_flags_unconfirmable_states(state, expected):
    decision, _ = decide(Action.ENABLE, ResourceStatus(state), recorded=True)

    assert decision is expected


def test_enable_leaves_resources_we_did_not_disable_alone():
    decision, reason = decide(Action.ENABLE, ResourceStatus(PowerState.STOPPED), recorded=False)

    assert decision is Decision.SKIP
    assert "not disabled by weekend-saver" in reason


def test_enable_with_ignore_prior_state_starts_unrecorded_stopped_resources():
    decision, _ = decide(
        Action.ENABLE,
        ResourceStatus(PowerState.STOPPED),
        recorded=False,
        ignore_prior_state=True,
    )

    assert decision is Decision.ACT


def test_skip_tag_stops_disable_regardless_of_case():
    status = ResourceStatus(PowerState.RUNNING, SKIP_TAGS)

    decision, reason = decide(Action.DISABLE, status, recorded=False)

    assert decision is Decision.SKIP
    assert "weekend-saver=skip" in reason


def test_skip_tag_does_not_strand_a_resource_we_disabled():
    status = ResourceStatus(PowerState.STOPPED, SKIP_TAGS)

    decision, _ = decide(Action.ENABLE, status, recorded=True)

    assert decision is Decision.ACT


def test_skip_tag_protects_unrecorded_resources_from_ignore_prior_state():
    status = ResourceStatus(PowerState.STOPPED, SKIP_TAGS)

    decision, _ = decide(Action.ENABLE, status, recorded=False, ignore_prior_state=True)

    assert decision is Decision.SKIP
