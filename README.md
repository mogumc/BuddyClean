# BuddyClean
AI写的小工具 可能有人用得上吧

## WorkBuddy 上下文缓存清理脚本

WorkBuddy的对话即使删除了依然会留在用户目录里面，如果很多长上下文对话就会有很多碎片数据。

提供了完全删除和保留使用量删除两种删除方式，无论哪种删除都会先移动到回收站需要二次清理回收站以确保没问题。

## 用法

```
    python wbctx.py report                 # 只读报告现状，一个字节都不动
    python wbctx.py plan                   # 预演清理：范围 + 能省多少，也不动
    python wbctx.py clean                  # 折叠已删会话（交互确认后才动手）
    python wbctx.py clean --action slim    # 仅瘦身，保留对话可读
    python wbctx.py clean --action purge   # 彻底删除（先进回收站）
    python wbctx.py verify                 # 对账：token 统计有没有断
    python wbctx.py trash --list           # 看回收站占了多少
    python wbctx.py trash --empty          # 清空回收站
```

## 免责声明 

一键生成的 不保证没问题

建议Python版本 >= 3.13

保留使用量删除只保证 [BuddySwtich](https://github.com/OShinTeam/BuddySwitch) 可以读取到用量
