"""U-SM: every v2 edge is allowed and every other pair is refused
(W-G2-149 section 3). Legacy v1 states are readable but have no edges."""

import itertools
import unittest

from xb_member_gateway.models import JobState
from xb_member_gateway.state_machine import (
    ALLOWED_TRANSITIONS, CLAIMABLE_STATES, LEGACY_STATES, InvalidTransition, next_state, validate_transition,
)


S = JobState
EXPECTED_EDGES = {
    (S.RECEIVED, S.VALIDATED), (S.RECEIVED, S.REJECTED_VALIDATION),
    (S.VALIDATED, S.QUEUED), (S.VALIDATED, S.REJECTED_VALIDATION),
    (S.QUEUED, S.LEASED), (S.RETRY_WAIT, S.LEASED),
    (S.LEASED, S.CREATED_VERIFIED), (S.LEASED, S.LINKED_EXISTING), (S.LEASED, S.REJECTED_VALIDATION),
    (S.LEASED, S.MANUAL_REVIEW), (S.LEASED, S.RETRY_WAIT),
    (S.MANUAL_REVIEW, S.RESOLVED), (S.MANUAL_REVIEW, S.QUEUED),
}


class StateMachineV2Tests(unittest.TestCase):
    def test_exactly_the_contract_edges_are_allowed(self):
        actual = {(source, target) for source, targets in ALLOWED_TRANSITIONS.items() for target in targets}
        self.assertEqual(actual, EXPECTED_EDGES)

    def test_every_pair_of_states_is_decided(self):
        for source, target in itertools.product(JobState, JobState):
            with self.subTest(source=source.value, target=target.value):
                if (source, target) in EXPECTED_EDGES:
                    self.assertEqual(next_state(source, target), target)
                else:
                    with self.assertRaises(InvalidTransition):
                        validate_transition(source, target)

    def test_terminal_and_legacy_states_have_no_exit(self):
        for state in {S.CREATED_VERIFIED, S.LINKED_EXISTING, S.REJECTED_VALIDATION, S.RESOLVED} | LEGACY_STATES:
            with self.subTest(state=state.value):
                self.assertEqual(ALLOWED_TRANSITIONS[state], frozenset())
        for legacy in LEGACY_STATES:
            self.assertFalse(any(legacy in targets for targets in ALLOWED_TRANSITIONS.values()), legacy)

    def test_only_queued_and_retry_wait_are_claimable(self):
        self.assertEqual(CLAIMABLE_STATES, {S.QUEUED, S.RETRY_WAIT})

    def test_unknown_state_fails_closed(self):
        with self.assertRaises(InvalidTransition):
            validate_transition("NOT_A_STATE", "QUEUED")
        with self.assertRaises(InvalidTransition):
            validate_transition("MANUAL_REVIEW", "CREATED_VERIFIED")


if __name__ == "__main__":
    unittest.main()
