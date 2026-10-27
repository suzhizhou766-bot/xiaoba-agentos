---
id: "experience-daf56068-1dc8-49e1-8b3c-579b2d2e5508"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "daf56068-1dc8-49e1-8b3c-579b2d2e5508"
last_verified: "2026-10-11"
---

# DeepSeek Harness 自定义 provider 的输出上限要显式写 maxTokens

适用场景：在 DeepSeek Harness 里通过自定义 provider（例如小八并发 Key 池）使用模型，出现“已达到输出 token 上限”反复截断，或上游报 max_tokens 参数非法。

做法：
1. DSH 对自定义 provider 中未声明 `maxTokens` 的模型按默认 32768 发送输出上限；思考内容也计入输出。需要长输出时，在 `~/.dsh/profiles/desktop/cordis.patch.yml` 对应模型条目下写 `maxTokens`。
2. 该值不能超过上游站点的上限。tierflow.cn 于 2026-10-10 实测上限为 131072（131072 通过，131073 返回 400）。
3. 判断是否为这个原因：看 Key 池用量记录 `私人助理/工具/小八监看/run/keypool-usage/usage-日期.jsonl`，被截断的请求 `out` 正好等于上限值。

证据：2026-10-11 00:20、00:27 两次请求 out=32768；修复为 131072。未验证平民之家节点对 131072 的接受情况。
