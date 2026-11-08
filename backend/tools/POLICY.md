# 离线策略工具

所有建议仅供观察，工具不会派发任务或替换 LLM 规划。

1. 生成逐任务标注模板：
   `python -m backend.tools.policy_trace run.json labels.json`
2. 人工填写候选 Agent 列表与 0–1 完成度评分，然后导出：
   `python -m backend.tools.policy_trace run.json samples.json --annotations labels.json`
3. 训练并保存模型：
   `python -m backend.tools.policy_train samples.json --save-model model.json --report report.json`
4. 查询建议：
   `python -m backend.tools.policy_suggest model.json agent_a agent_b`
5. 使用独立运行的标注样本评估：
   `python -m backend.tools.policy_eval model.json heldout.json --training samples.json`

批量导出增加 `--batch`：输入是运行记录数组，标注是以 run_id 为键的对象。
批量转换按运行开始时间排序；运行内部保留任务记录顺序，并非精确决策时间。
不要混入尚未结束的运行；原始 trace 不含候选集与逐任务评分，必须显式补充。

来源隔离检查以 run_id 为单位，不会验证指定训练文件是否就是模型的真实训练来源。
模型与样本 SHA-256 用于内容核对，不是签名。固定模型评估不会学习评估集奖励。
一致率及观测奖励都是描述性指标；历史上未选择的 Agent 没有反事实奖励，不能据此
证明策略优于 LLM。默认上下文缺失值也不应被当作真实埋点。

输出文件必须不存在。报告和模型分别写入，后一个失败时前一个可能已经生成。
真实检索标注基线与策略效果验证仍待完成。

## 运行时只记录模式

在后端/worker 环境中设置 `AGENT_POLICY_OBSERVE=1` 和
`AGENT_POLICY_MODEL`（已训练模型 JSON 的路径）以开启。默认关闭。
规划节点在任务规范化和代理筛选后生成观察，审批和任务执行仍按原规划进行。
模型文件每次规划加载一次，建议记录在 checkpoint 的 `_policy_observation`，
并随现有运行历史保存到 `policy_observation`；恢复运行时也会从状态归档。
读取/评分失败记录 `status=skipped`，不会阻止执行；不产生额外 SSE 事件。

记录包括任务 ID、实际/建议 Agent、各候选分数、依赖特征、预算比例和模型指纹，
不记录 prompt。候选集为已注册 worker，尚未按任务能力过滤，因此建议不能直接执行。
依赖特征包含显式依赖与技能隐式依赖；预算比例只表示剩余步骤比例。
当前仅观察初始规划，不包含补救重规划，也不在线更新模型或自动分配奖励。
上述开关需要部署端显式设置；功能测试使用本地模型与 SQLite，未验证真实生产流量。

## 使用已归档的运行时观察

对运行历史 JSON 数组生成报告：
`python -m backend.tools.policy_observations runs.json --output comparison.json`
报告按模型指纹分组，将没有观察或观察失败的运行单独计数，不把它们计入一致率。
这只衡量初始计划建议与原规划的一致性，不衡量实际成功率或策略提升。

为单个已结束运行填写 `scores.json`，内容如 `{"research:1": 0.8}`，然后执行：
`python -m backend.tools.policy_observations run.json --scores scores.json --output samples.json`
任务 ID 必须同时出现在初始观察与执行结果中，实际 Agent 必须匹配，执行状态必须是
done 或 failed。未执行、补救新增或存在重复结果的任务不能据此导出。
评分始终由人工显式提供，不使用 run 级完成度代替任务分数。
候选集、依赖深度和预算比例沿用观察记录；输出按初始观察顺序排列，不能视为精确执行时间顺序。
候选集依然未经能力筛选，训练前应审查其适用性。当前观察版本为 1，同时兼容之前未写版本号的记录。
导出结果可交给 policy_train，已有输出文件不会被覆盖；工具不会自动训练或发布模型。
