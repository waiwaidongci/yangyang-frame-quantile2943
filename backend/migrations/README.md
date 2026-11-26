# 数据库初始化

`bootstrap.py` 按 SQLAlchemy 模型初始化 v1 表结构。后端启动时执行，worker 在后端健康后启动。演示台账与账号使用自然键补齐，后续更新数据时通过导入建立独立快照。

表清单：`accounts`、`datasets`、`analysis_runs`、`reviews`、`audit_events`，以及中途交班重估使用的 `work_events`（只增不改的工时记录，按接收时间戳冻结）与 `handoffs`（持久化的时点重估结果）。
