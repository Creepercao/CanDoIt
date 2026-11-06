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
真实检索标注基线、运行时策略建议记录与效果验证仍待完成。
