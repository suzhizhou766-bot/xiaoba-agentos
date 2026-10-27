---
id: "experience-a2bf51d7-97c8-41c6-8dac-85f5e83eb311"
type: "experience"
parent: "experiences"
status: "active"
authority: false
source_task_id: "a2bf51d7-97c8-41c6-8dac-85f5e83eb311"
last_verified: "2026-10-12"
---

# 发布审查的否决先按真实触发点复现，停启验收要覆盖正式Runner链路

2026-10-12第2步甲验收：一份外部审查认为sqlite3.OperationalError会绕过apply的异常处理。源码实际使用except BaseException。主力在publish_index之后、start_worker之前的queue调用注入该异常，获得结构化失败、完整退回及文件权限还原。以探针源码与实际输出补充材料，两路重新独立审查均通过，原否决和超时记录保留。

候选或审查者的断言都不是事实本身。碰到具体否决先定位调用栈、异常继承与触发位置，用控制组验证；不能仅为迎合否决增加无效代码，也不能直接忽略否决。补充证据要绑定新审查题面，不把旧PASS复用为新材料的结论。

复制现场最初使用直接Python Worker，随后改用线上同一个签名Runner及复制的正式启动脚本、显式隔离配置。真实launchctl停启和11阶段故障退回全部通过，最后完整切换的旧PID退出、新PID与新清单实际读回。只读入口验证、隔离测试、真实复制现场、线上正式切换是不同证据。

证据位于工作区/小八AgentOS-架构第2步甲/reports/甲-双审与现场演练验收.md、甲-SQLite故障独立探针.json、甲-复制现场演练.json。正式线上当时未切换；本候选不要求用户接受创意/业务结论，不包含原始审查输出，不替代两阶段经验审批。
