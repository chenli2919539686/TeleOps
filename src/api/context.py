"""API 层共享运行时上下文。

用来打破 server.py <-> routers/ 的循环依赖：

- server.py 在启动、完成全部全局单例 / helper / 模型定义后，把需要跨模块共享的
  对象挂到本模块的 `ctx` 实例上；
- routers/ 各子模块只 `from src.api.context import ctx as s` 读取，**绝不 import server**，
  因此不会在 server 尚未初始化完毕时触发回入，循环被切断。

路由处理函数内部统一用 `s.<名字>` 访问（与之前 `server` 的写法一致），对请求期
（此时 server 早已初始化、ctx 已填充）透明无差异。
"""
class _Ctx:
    pass


ctx = _Ctx()
