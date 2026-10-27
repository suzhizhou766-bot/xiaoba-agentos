import unittest

from xiaoba_agentos.circuit_breaker import CircuitBreaker


class CircuitBreakerTest(unittest.TestCase):
    def test_two_consecutive_misses_trip(self):
        breaker = CircuitBreaker(threshold=2)
        s1 = breaker.record_step("t", "s", tool_calls=0, tool_results=0)
        self.assertFalse(s1.tripped)
        s2 = breaker.record_step("t", "s", tool_calls=0, tool_results=0)
        self.assertTrue(s2.tripped)
        self.assertEqual("missing_real_tool_execution", s2.reason)

    def test_call_without_result_counts_as_miss(self):
        breaker = CircuitBreaker(threshold=2)
        breaker.record_step("t", "s", tool_calls=1, tool_results=0)
        breaker.record_step("t", "s", tool_calls=1, tool_results=0)
        self.assertTrue(breaker.is_tripped("t", "s"))

    def test_real_execution_resets_counter(self):
        breaker = CircuitBreaker(threshold=2)
        breaker.record_step("t", "s", tool_calls=0, tool_results=0)
        breaker.record_step("t", "s", tool_calls=1, tool_results=1)
        self.assertFalse(breaker.is_tripped("t", "s"))

    def test_same_error_trips(self):
        b = CircuitBreaker()
        b.record_step('t', '9.4', 1, 1, exit_code=1, error_signature='exact-match')
        s = b.record_step('t', '9.4', 1, 1, exit_code=1, error_signature='exact-match')
        self.assertEqual('repeated_tool_error', s.reason)
        self.assertTrue(s.tripped)

    def test_different_errors_same_step_trip(self):
        b = CircuitBreaker()
        b.record_step('t', 's', 1, 1, exit_code=1, error_signature='a')
        self.assertEqual('repeated_step_failure', b.record_step('t', 's', 1, 1, exit_code=2, error_signature='b').reason)

    def test_tripped_requires_explicit_reset(self):
        b = CircuitBreaker()
        for _ in range(2): b.record_step('t', 's', 0, 0)
        self.assertTrue(b.record_step('t', 's', 1, 1).tripped)
        b.reset('t', 's')
        self.assertFalse(b.record_step('t', 's', 1, 1).tripped)

    def test_success_breaks_failure_sequence(self):
        b = CircuitBreaker()
        b.record_step('t', 's', 1, 1, exit_code=1)
        b.record_step('t', 's', 1, 1)
        self.assertFalse(b.record_step('t', 's', 1, 1, exit_code=1).tripped)

    def test_failure_then_planning_trips(self):
        b = CircuitBreaker()
        b.record_step('t', 's', 1, 1, exit_code=1)
        b.record_step('t', 's', 0, 0)
        self.assertTrue(b.record_step('t', 's', 0, 0).tripped)

    def test_missing_results_and_step_isolation(self):
        b = CircuitBreaker()
        b.record_step('t', 's', 2, 1)
        self.assertFalse(b.record_step('t', 'other', 0, 0).tripped)
        self.assertTrue(b.record_step('t', 's', 2, 1).tripped)
