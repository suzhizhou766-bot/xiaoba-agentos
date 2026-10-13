"""模块约定。

新增一个模块 = 新建一个文件，继承 Module，实现 routes()（声明地址与门禁级别），
需要后台工作的再实现 start()/stop()/health()，最后在 xiaoba_monitor.py 的 MODULES 里加一行。
"""


class Module:
    id = ""       # 英文标识，也是 /api/modules 里的键
    title = ""    # 给人看的名字
    kind = "view"  # view：有界面的功能；service：纯后台

    def __init__(self, ctx):
        self.ctx = ctx

    def routes(self):
        return []

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        """返回 {"status": ok|starting|warn|failed, "detail": 说明}。"""
        return {"status": "ok"}
