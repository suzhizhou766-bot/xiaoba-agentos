# Studio Display 闪屏日志监看

针对本机 Studio Display 与 ACASIS TB501Pro 的定时、只读诊断工具。不是小八监看中心的常驻组件，不开机自启。

固定使用小八 Python 3.14.7，无第三方依赖。连续读取 macOS 统一日志，每分钟读取一次经过字段白名单过滤的显示、雷雳和 USB 连接清单。只记录相关硬件事件，不读取磁盘文件内容、不抓屏、不测速，不修改显示或固件设置。运行期间使用 `caffeinate -i` 临时防止系统自动睡眠，结束时释放；不阻止用户主动睡眠或合盖。

## 运行

```sh
/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14 /Users/suzhizhou/Documents/私人助理/工具/StudioDisplay闪屏监看/monitor.py --seconds 3600 --out <新的绝对输出目录>
```

输出目录必须是新目录；已有 `status.json` 时拒绝覆盖。`status.json` 每五秒更新；`events.jsonl` 是经筛选的硬件事件；`topology.jsonl` 记录连接清单的变化；`report.md` 是运行中及最终报告。

中途停止时，先核对 `status.json` 内 PID 与 `script` 路径，再给该进程发送 SIGTERM。它会结束自己的日志子进程和临时唤醒断言，并将本次标记为 `stopped`。

## 计数与边界

- 只对基线识别到的 Studio Display 显示 ID 统计 hotplug out/in；同一次 raw/processed 两类记录合并为一个候选事件。
- 候选事件不自动归因为故障；人工拔线、断电、睡眠和唤醒也要核对。
- 连接状态间隔不是肉眼黑屏时长。没有捕捉到日志，不等于证明没有可见闪屏。
- 若日志进程退出、采集不可见、采样失败或主循环出现长时间暂停，报告会保留告警，不声称完整观测成功。
- 每次以实际 USB／雷雳模式为基线。2026-10-22 原故障前 TB501Pro 为雷雳 80Gb/s；用户重新插回后读取为 USB 10Gb/s，不能当作完全同条件复现。

## 本次验证

2026-10-22：真实故障日志格式的解析、去重、计时及排除其它显示器 ID 检查通过；12 秒真实日志采集收到 36 条 JSON 日志并自动退出，子进程结束。该验证不是一小时正式观察结果。

进行中记录：`工作区/StudioDisplay闪屏监看-20261022/`。不将本轮技术观测自动发布到第二大脑或长期经验。
