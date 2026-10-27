import unittest

from xiaoba_agentos.routing import AGENTOS_MULTI, ASK_USER, DIRECT, REVIEW, decide_route


class RoutingPolicyTest(unittest.TestCase):
    def test_unrelated_aside_stays_in_session_model(self):
        decision = decide_route("顺便问一句，月亮为什么会有圆缺？", relation="unrelated")
        self.assertEqual(DIRECT, decision.route)
        self.assertEqual("session_model_direct", decision.answer_provenance)

    def test_high_impact_request_cannot_be_forced_to_direct(self):
        with self.assertRaises(ValueError):
            decide_route("删除并发布正式版本", risk="high", force_route=DIRECT)

    def test_low_impact_request_cannot_be_forced_to_direct(self):
        with self.assertRaisesRegex(ValueError, "direct route cannot be forced"):
            decide_route("你好", force_route=DIRECT)

    def test_unflagged_code_edits_and_purchases_enter_agentos(self):
        prompts = (
            "把 src/app.py 里的 false 改成 true",
            "edit src/app.py and run the tests",
            "请购买这个商品",
            "purchase this item",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(AGENTOS_MULTI, decide_route(prompt).route)

    def test_action_language_overrides_unrelated_aside_signal(self):
        self.assertEqual(
            AGENTOS_MULTI,
            decide_route("edit src/app.py", relation="unrelated").route,
        )

    def test_english_action_words_do_not_match_inside_other_words(self):
        self.assertEqual(DIRECT, decide_route("What is a credit score?").route)

    def test_implementation_request_enters_agentos(self):
        decision = decide_route("请升级小八AgentOS并完成回归验证")
        self.assertEqual(AGENTOS_MULTI, decision.route)

    def test_explicit_multi_agent_request_has_precedence(self):
        decision = decide_route("请走小八双Agent复核这个答案")
        self.assertEqual(AGENTOS_MULTI, decision.route)
        self.assertEqual(1.0, decision.confidence)

    def test_explicit_multi_agent_review_of_review_flow_is_not_misrouted(self):
        decision = decide_route(
            "复核交付初审与下班终审实现",
            explicit_agentos=True,
        )
        self.assertEqual(AGENTOS_MULTI, decision.route)

    def test_shutdown_enters_review_boundary(self):
        self.assertEqual(REVIEW, decide_route("下班了小八助理").route)

    def test_ambiguous_active_task_followup_asks_user(self):
        decision = decide_route("这个怎么办", active_task_id="task-1")
        self.assertEqual(ASK_USER, decision.route)


if __name__ == "__main__":
    unittest.main()
